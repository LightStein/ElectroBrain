#!/usr/bin/env python3
"""ask.py — answering engine CLI for George's standards assistant.

Invoked by the bridge once per turn:
    python ask.py -p "<message>" [--fresh]

Stdout = the reply (bridge BRIDGE_OUTPUT=plain). Progress lines go to
./.progress (the bridge tails it and forwards lines to Telegram).

Pipeline (local, qwen via Ollama):
  A. one qwen call: question (+ short history) + catalog.md
       -> {"terms_ru": [...], "terms_en": [...], "doc_ids": [...]}
  B. lexical retrieval: score heading-chunks of the selected docs' full.md
     by term hits (tf * idf-lite), take the top chunks
  C. one qwen call: answer in Russian with mandatory citations
     (document title + clause number + verbatim quote)

Escalation to `claude -p` (strong engine, Anri's account):
  - message starts with "PRO:" (bot's /pro command)
  - message contains an [Image attached:...] / [File attached:...] marker
    (qwen is text-only)
  - stage C reports NOT_FOUND and ASK_AUTO_ESCALATE=1

Configuration via environment (all optional):
  STANDARDS_ROOT     root folder (default: parent of this script's directory)
  OLLAMA_URL         default http://127.0.0.1:11434
  ASK_MODEL          default qwen3:4b-instruct (non-thinking)
  ASK_NUM_CTX        default 16384
  ASK_HISTORY_FILE   default <root>/state/ask-history.json
  ASK_MAX_CHUNK_CHARS  total retrieval budget, default 12000
  ASK_THINK_FINAL    "1" = let the model think (default 0; the default
                     model has no thinking mode)
  ASK_AUTO_ESCALATE  "1" = auto-run claude when qwen finds nothing (default 0)
  ASK_CLAUDE_MODEL   default "haiku"
  ASK_PRO_PROMPT     default <script dir>/pro-prompt.md
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("STANDARDS_ROOT", os.path.dirname(SCRIPT_DIR))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
# qwen3:4b thinks unconditionally in Ollama - neither think=false nor
# /no_think suppresses it - and burns ~450 tokens (~30s on a GTX 1650)
# on a one-line question. The -instruct variant does not think at all:
# same question, 30 tokens, 7s. Measured on the target laptop.
MODEL = os.environ.get("ASK_MODEL", "qwen3:4b-instruct")
NUM_CTX = int(os.environ.get("ASK_NUM_CTX", "16384"))
HISTORY_FILE = os.environ.get("ASK_HISTORY_FILE", os.path.join(ROOT, "state", "ask-history.json"))
# At the measured ~114 tok/s prompt processing, 12000 chars of context was
# ~5000 tokens = ~44s of prompt processing before generation even begins.
# 6000 keeps the answer call inside a usable chat latency.
MAX_CHUNK_CHARS = int(os.environ.get("ASK_MAX_CHUNK_CHARS", "6000"))
# Per-chunk size. At 2500 the 6000-char budget fit only TWO chunks, so a
# single document could take both slots and crowd out the one that actually
# held the answer - observed with a switch-height question where СП 256 was
# correctly shortlisted but never made it into the context. Smaller chunks
# mean more documents represented for the same number of tokens.
CHUNK_CHARS = int(os.environ.get("ASK_CHUNK_CHARS", "1200"))
MAX_CHUNKS_PER_DOC = int(os.environ.get("ASK_MAX_CHUNKS_PER_DOC", "2"))
# The question's own words matter far more than keywords expanded from
# meta.json; weighting them equally is what let reference lists outrank real
# clauses.
QUESTION_TERM_WEIGHT = 3.0
EXPANDED_TERM_WEIGHT = 1.0
# An acronym or standard designation in the question is close to a filter:
# if the user says TN-C or IP44, chunks containing it are almost certainly
# the right ones.
DESIGNATION_WEIGHT = 8.0
# A chunk that is mostly "ГОСТ Р 55842-2013 (ИСО 30061:2007) ..." is a
# normative-references list. It matches many terms and answers nothing.
REFLIST_RE = re.compile(r"(ГОСТ|МЭК|ИСО|IEC|ISO|СП|СНиП|EN)\s*[Р\s]*[\d.\-]{3,}", re.I)
# Off by default: the default model has no thinking mode to enable.
THINK_FINAL = os.environ.get("ASK_THINK_FINAL", "0") == "1"
AUTO_ESCALATE = os.environ.get("ASK_AUTO_ESCALATE", "0") == "1"
# "lexical" (default) routes without a model - see stage A. "llm" keeps the
# original catalog-in-prompt router, retained for comparison.
ROUTER = os.environ.get("ASK_ROUTER", "lexical")
CLAUDE_MODEL = os.environ.get("ASK_CLAUDE_MODEL", "haiku")
PRO_PROMPT_FILE = os.environ.get("ASK_PRO_PROMPT", os.path.join(SCRIPT_DIR, "pro-prompt.md"))

CATALOG = os.path.join(ROOT, "index", "catalog.md")
DOCS_DIR = os.path.join(ROOT, "index", "docs")
HISTORY_MAX = 6

# ---------------------------------------------------------------- utilities

def progress(text):
    """Milestone line for Telegram (bridge tails ./.progress in its WORKDIR)."""
    try:
        with open(".progress", "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass


def log(text):
    print(f"[ask] {text}", file=sys.stderr)


def load_history():
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_history(history):
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history[-HISTORY_MAX:], f, ensure_ascii=False)
    except OSError:
        pass


def ollama_chat(messages, think, want_json=False, timeout=300):
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "think": think,
        "options": {"num_ctx": NUM_CTX, "temperature": 0.2},
    }
    if want_json:
        payload["format"] = "json"
    req = urllib.request.Request(
        OLLAMA_URL + "/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except Exception as e:  # older ollama may reject "think" — retry without it
        if "think" in payload:
            payload.pop("think")
            req = urllib.request.Request(
                OLLAMA_URL + "/api/chat",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
        else:
            raise e
    content = (data.get("message") or {}).get("content", "")
    # Strip any inline <think> block qwen may emit when `think` isn't honored.
    content = re.sub(r"<think>[\s\S]*?</think>", "", content).strip()
    return content



# ------------------------------------------------- stage A: lexical routing
#
# Measured on the real corpus: embedding the 56-document catalog in a routing
# prompt costs 8,575 tokens, and this GPU processes prompts at ~114 tok/s -
# 83 SECONDS per question, before the answer call even starts. Worse, if the
# catalog ever exceeds num_ctx, Ollama returns 400 and every question fails
# outright rather than degrading.
#
# So routing is done without a model. meta.json carries ALIGNED RU/EN keyword
# pairs (keywords_ru[i] and keywords_en[i] are the same concept), which is
# exactly the translation table needed: a Russian question token that matches
# a Russian keyword contributes its English twin as a search term. That was
# the only thing the LLM call was really needed for.

STOPWORDS = {
    "какой", "какая", "какое", "какие", "какого", "каком", "чему", "чего",
    "который", "должен", "должна", "должно", "нужно", "надо", "можно",
    "быть", "если", "или", "для", "при", "над", "под", "это", "как",
    "что", "где", "когда", "почему", "сколько", "ставить", "делать",
    "what", "which", "the", "and", "for", "with", "from", "should", "must",
}


def norm_token(w):
    """Crude Russian stemming: drop the inflected tail.

    A full morphological analyser would be better, but this is a keyword
    match, not parsing - "заземления"/"заземление"/"заземлению" all need to
    collide, and cutting the last two characters of a long word does that
    without a dependency.
    """
    w = w.lower().strip("«»\"'(),.;:!?-—")
    if len(w) > 6 and re.search(r"[а-яё]", w):
        return w[:-2]
    return w


# Keeps designations whole: TN-C, TN-C-S, IP44, ГОСТ, 50571.5.52, УЗО.
# The previous pattern split on the hyphen and then dropped both halves for
# being under 4 characters, so "чем отличается TN-C от TN-S" reached
# retrieval carrying only the word "система" - which matches every document
# in an electrical corpus. The most discriminating tokens were the ones
# being thrown away.
TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+(?:[-.][A-Za-zА-Яа-яЁё0-9]+)*")
# All-caps Latin/Cyrillic, or anything with a digit or hyphen: acronyms and
# standard designations. Rare, and therefore worth far more than prose words.
DESIGNATION_RE = re.compile(r"^(?:[A-ZА-ЯЁ]{2,}(?:[-.][A-ZА-ЯЁ0-9]+)*|.*[\d].*|.*-.*)$")


def is_designation(w):
    return bool(DESIGNATION_RE.match(w)) and len(w) >= 2


def question_tokens(question):
    out = set()
    for w in TOKEN_RE.findall(question):
        if is_designation(w):
            out.add(w.lower())
        elif len(w) >= 3 and w.lower() not in STOPWORDS:
            out.add(norm_token(w))
    return out


def load_meta_index():
    """doc_id -> meta dict, for every indexed document."""
    out = {}
    if not os.path.isdir(DOCS_DIR):
        return out
    for doc_id in os.listdir(DOCS_DIR):
        mp = os.path.join(DOCS_DIR, doc_id, "meta.json")
        try:
            with open(mp, encoding="utf-8") as f:
                out[doc_id] = json.load(f)
        except (OSError, ValueError):
            continue
    return out


def route_lexical(question, metas, max_docs=12):
    """Pick candidate documents and build WEIGHTED bilingual search terms.

    The question's own words are the real signal; expanded keywords only
    broaden reach. Weighting them equally let reference-list sections win -
    they are dense with standard names, so they collect keyword hits without
    containing any answer.
    """
    qt = question_tokens(question)
    scored = []
    terms = {}
    for w in TOKEN_RE.findall(question):
        if is_designation(w):
            terms[w] = DESIGNATION_WEIGHT
        elif len(w) >= 4 and w.lower() not in STOPWORDS:
            terms[w.lower()] = QUESTION_TERM_WEIGHT

    for doc_id, meta in metas.items():
        ru = meta.get("keywords_ru") or []
        en = meta.get("keywords_en") or []
        topics = meta.get("topics") or []
        title = meta.get("title") or ""

        score, hits = 0.0, []
        # Keywords are the strongest signal, and the aligned pair gives us the
        # other language for free.
        for i, kw in enumerate(ru):
            if any(t in norm_token(kw) or norm_token(kw) in t for t in qt if len(t) > 3):
                score += 3.0
                hits.append(kw)
                if i < len(en):
                    hits.append(en[i])       # the aligned English twin
        for i, kw in enumerate(en):
            if any(t in kw.lower() or kw.lower() in t for t in qt if len(t) > 3):
                score += 3.0
                hits.append(kw)
                if i < len(ru):
                    hits.append(ru[i])
        for t in topics:
            tl = t.lower()
            if any(q in tl for q in qt if len(q) > 3):
                score += 1.5
                hits.append(t)
        tl = title.lower()
        if any(q in tl for q in qt if len(q) > 3):
            score += 1.0

        if score > 0:
            scored.append((score, doc_id, hits))

    scored.sort(key=lambda x: -x[0])
    top = scored[:max_docs]
    for _, _, hits in top:
        for h in hits:
            if len(h) > 2:
                terms.setdefault(h, EXPANDED_TERM_WEIGHT)
    doc_ids = [d for _, d, _ in top]
    # No keyword hit anywhere: fall back to scanning everything rather than
    # answering "not found" from an empty shortlist.
    return terms, doc_ids

# ------------------------------------------- stage A (legacy): LLM routing

ROUTE_SYSTEM = """Ты — маршрутизатор вопросов к каталогу нормативных документов \
(электрика, пожарные системы, заземление, молниезащита и т.п.). Документы на \
русском и английском. Тебе дан каталог (id | название | язык | темы) и вопрос.

Верни СТРОГО JSON без пояснений:
{"terms_ru": [...], "terms_en": [...], "doc_ids": [...]}

- terms_ru: 3-8 ключевых слов/словосочетаний ПО-РУССКИ для поиска по тексту
  (включая синонимы: например для "цвет провода заземления" — "заземление",
  "защитный проводник", "жёлто-зелёный", "PE", "маркировка").
- terms_en: те же понятия ПО-АНГЛИЙСКИ ("grounding", "protective earth",
  "green-yellow", "conductor colour").
- doc_ids: id ВСЕХ документов из каталога, которые могут содержать ответ
  (обычно 3-15). Если не уверен — включай."""


# Router prompt budget in CHARACTERS. num_ctx counts prompt AND response, so
# reserve room for the system prompt, history, question and the JSON reply,
# then convert with a deliberately pessimistic 2.5 chars/token - Russian
# tokenises worse than English, and over-estimating here only costs some
# keyword detail, while under-estimating silently truncates the prompt.
CATALOG_BUDGET = int(os.environ.get("ASK_CATALOG_BUDGET",
                                    str(int((NUM_CTX - 1500) * 2.5))))


def shrink_catalog(catalog_text):
    """Keep every document, drop detail, when the catalog outgrows the window.

    Truncating the prompt would silently drop whole documents off the end -
    they become unroutable and nobody finds out. Shortening each line instead
    costs some keyword recall but keeps every document reachable.
    """
    if len(catalog_text) <= CATALOG_BUDGET:
        return catalog_text
    out = []
    for line in catalog_text.split("\n"):
        if line.startswith("- "):
            parts = line.split("|")
            if len(parts) >= 4:
                topics = ", ".join(parts[3].split(",")[:3]).strip()
                line = f"{parts[0].strip()} | {parts[1].strip()} | {topics}"
        out.append(line)
    shrunk = "\n".join(out)
    log(f"catalog {len(catalog_text)} chars > budget {CATALOG_BUDGET}, "
        f"shortened to {len(shrunk)}")
    if len(shrunk) > CATALOG_BUDGET:
        log("WARNING: catalog still over budget - the router may not see "
            "every document. Trim topics in meta.json or raise ASK_NUM_CTX.")
    return shrunk


def route(question, catalog_text, history):
    catalog_text = shrink_catalog(catalog_text)
    hist = ""
    if history:
        last = history[-2:]
        hist = "\n\nКонтекст предыдущих вопросов:\n" + "\n".join(
            f"Q: {h['q'][:200]}" for h in last)
    msgs = [
        {"role": "system", "content": ROUTE_SYSTEM},
        {"role": "user", "content": f"КАТАЛОГ:\n{catalog_text}\n{hist}\n\nВОПРОС: {question}"},
    ]
    raw = ollama_chat(msgs, think=False, want_json=True, timeout=180)
    try:
        parsed = json.loads(raw)
        terms = [t for t in (parsed.get("terms_ru") or []) + (parsed.get("terms_en") or [])
                 if isinstance(t, str) and t.strip()]
        doc_ids = [d for d in (parsed.get("doc_ids") or []) if isinstance(d, str)]
        return terms, doc_ids
    except ValueError:
        log(f"route: unparseable JSON: {raw[:200]}")
        return [w for w in re.findall(r"\w{4,}", question)][:8], []


# -------------------------------------------------------- stage B: retrieval

HEADING_RE = re.compile(r"^#{1,4}\s", re.M)


def split_chunks(text, max_chars=CHUNK_CHARS):
    """Split a Markdown doc into heading-delimited chunks; oversized chunks are
    split again on blank lines."""
    positions = [m.start() for m in HEADING_RE.finditer(text)] or [0]
    if positions[0] != 0:
        positions.insert(0, 0)
    positions.append(len(text))
    chunks = []
    for a, b in zip(positions, positions[1:]):
        seg = text[a:b].strip()
        if not seg:
            continue
        if len(seg) <= max_chars:
            chunks.append(seg)
        else:
            buf = ""
            for para in seg.split("\n\n"):
                if len(buf) + len(para) > max_chars and buf:
                    chunks.append(buf.strip())
                    buf = ""
                buf += para + "\n\n"
            if buf.strip():
                chunks.append(buf.strip())
    return chunks


def russian_stem(w):
    """Prefix stem for Russian inflection.

    Cutting only the last two characters is not enough: "розеточной" became
    "розеточн", which cannot match "розеток" - and that is exactly how a
    question about socket circuits missed a clause about sockets. A fixed
    short prefix collides the inflections that matter ("розето" matches both)
    and also catches some compounding ("труб" reaches "трубопровод").
    """
    if not re.search(r"[а-яёА-ЯЁ]", w):
        return w
    if len(w) > 6:
        return w[:6]
    if len(w) >= 5:
        return w[:4]
    return w


def term_regexes(terms):
    """`terms` may be a dict {term: weight} or a plain iterable."""
    """One regex per WORD, not per term.

    Keywords from meta.json are often phrases ("Выключатели и коммутационная
    аппаратура"). Escaped whole they match nothing at all - verified: every
    multi-word term missed on text that plainly contained its words. Splitting
    into words recovers that signal; the words are what appear in the
    documents.
    """
    weights = terms if isinstance(terms, dict) else {t: 1.0 for t in terms}
    out, seen = [], set()
    for term, tw in weights.items():
        for w in re.findall(r"[\wа-яёА-ЯЁ]+", str(term)):
            if len(w) < 4:
                continue
            stem = russian_stem(w)
            key = stem.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append((w, re.compile(re.escape(stem), re.I), tw))
    return out


def retrieve(doc_ids, terms):
    """Score chunks of the selected docs; fall back to all docs on empty."""
    regs = term_regexes(terms)
    if not regs:
        return []

    def doc_paths(ids):
        for did in ids:
            p = os.path.join(DOCS_DIR, did, "full.md")
            if os.path.isfile(p):
                yield did, p

    ids = list(doc_ids)
    if not ids and os.path.isdir(DOCS_DIR):
        ids = sorted(os.listdir(DOCS_DIR))

    # idf-lite: a term hitting every doc tells us little
    per_doc_chunks = {}
    doc_freq = {t: 0 for t, _, _ in regs}
    for did, p in doc_paths(ids):
        try:
            with open(p, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        per_doc_chunks[did] = split_chunks(text)
        for t, rx, _ in regs:
            if rx.search(text):
                doc_freq[t] += 1

    # Proper idf, and squared: the previous linear form spanned only 1.0-2.8,
    # so a generic word like "система" hitting four times beat a rare, highly
    # specific term hitting once. In this corpus almost every document
    # mentions cables and systems; only the rare terms carry information.
    n_docs = max(1, len(per_doc_chunks))
    weights = {t: (math.log((n_docs + 1) / (df + 1)) + 1.0) ** 2
               for t, df in doc_freq.items()}

    scored = []
    for did, chunks in per_doc_chunks.items():
        for ch in chunks:
            s = 0.0
            for t, rx, tw in regs:
                hits = len(rx.findall(ch))
                if hits:
                    # sqrt, not a raw count: repeating a common word should
                    # not outweigh the presence of a discriminating one.
                    s += weights[t] * tw * math.sqrt(hits)
            if s > 0:
                # Normative-reference lists are keyword-dense and answer
                # nothing; discount them rather than dropping them outright,
                # since a reference can occasionally be the answer.
                refs = len(REFLIST_RE.findall(ch))
                if refs >= 3 and refs * 60 > len(ch):
                    s *= 0.25
                scored.append((s, did, ch))
    scored.sort(key=lambda x: -x[0])

    # Cap per document. Without this the highest-scoring document can take
    # every slot, which is exactly how a correctly-shortlisted document ended
    # up contributing nothing to the answer.
    out, used, per_doc = [], 0, {}
    for s, did, ch in scored:
        if per_doc.get(did, 0) >= MAX_CHUNKS_PER_DOC:
            continue
        if used + len(ch) > MAX_CHUNK_CHARS and out:
            # skip this one, not the rest: a single oversized chunk used to
            # end selection early and drop smaller ones that still fitted
            continue
        out.append((did, ch))
        per_doc[did] = per_doc.get(did, 0) + 1
        used += len(ch)
        if len(out) >= 8:
            break
    return out


# --------------------------------------------------------- stage C: answer

ANSWER_SYSTEM = """Ты — помощник ревизора по электротехническим и пожарным \
нормам. Отвечай ТОЛЬКО на основе приведённых фрагментов документов. Правила:

1. Отвечай по-русски, кратко и по делу (это Telegram).
2. ОБЯЗАТЕЛЬНО указывай источник: название документа и номер пункта/раздела,
   плюс короткую дословную цитату. Формат в конце ответа:
   📄 <документ>, п. <пункт>: «<цитата>»
3. Если во фрагментах ответа НЕТ — не выдумывай. Напиши ровно: NOT_FOUND
4. Если фрагменты противоречат друг другу — покажи оба варианта с источниками.
5. Заверши строкой: _Проверь в первоисточнике._"""


def answer(question, ctx_chunks, history, titles_by_id):
    parts = []
    for did, ch in ctx_chunks:
        title = titles_by_id.get(did, did)
        parts.append(f"===== {title} (id: {did}) =====\n{ch}")
    context = "\n\n".join(parts)
    msgs = [{"role": "system", "content": ANSWER_SYSTEM}]
    for h in history[-3:]:
        msgs.append({"role": "user", "content": h["q"]})
        msgs.append({"role": "assistant", "content": h["a"][:800]})
    msgs.append({"role": "user",
                 "content": f"ФРАГМЕНТЫ ДОКУМЕНТОВ:\n{context}\n\nВОПРОС: {question}"})
    return ollama_chat(msgs, think=THINK_FINAL, timeout=420)


# ----------------------------------------------------------- claude escalate

def find_claude():
    """Absolute path to a claude CLI that subprocess can launch cleanly.

    npm lays down three shims next to each other - `claude` (a bash script
    with no extension), `claude.cmd` and `claude.ps1` - plus the real
    binary at node_modules/@anthropic-ai/claude-code/bin/claude.exe.

    Two traps, both hit on this project:
      * a bare which("claude") returns the extensionless bash shim, which
        Windows cannot execute at all (FileNotFoundError, indistinguishable
        from "claude is not installed");
      * the .cmd shim re-parses its arguments through cmd.exe, which mangles
        a multi-line --append-system-prompt into nothing, so claude exits
        with "Input must be provided ... when using --print".

    So prefer the real .exe, which CreateProcess launches with argv intact.
    """
    if os.name == "nt":
        for base in (os.environ.get("APPDATA", ""),
                     os.environ.get("ProgramFiles", "")):
            if not base:
                continue
            cand = os.path.join(base, "npm", "node_modules", "@anthropic-ai",
                                "claude-code", "bin", "claude.exe")
            if os.path.isfile(cand):
                return cand
        # Fall back to a shim, .cmd only - never the bash or .ps1 one.
        for name in ("claude.exe", "claude.cmd"):
            p = shutil.which(name)
            if p and p.lower().endswith((".exe", ".cmd")):
                return p
        return None
    return shutil.which("claude")


def escalate_claude(question, history):
    """Strong engine: claude CLI with grep/read over the same index."""
    progress("⚡ Подключаю сильную модель…")
    try:
        with open(PRO_PROMPT_FILE, encoding="utf-8") as f:
            sys_prompt = f.read().strip()
    except OSError:
        sys_prompt = ("Answer questions about electrical/fire standards using the "
                      "Markdown index under index/. Always cite document + clause. "
                      "Answer in Russian.")
    hist = ""
    if history:
        hist = "Контекст диалога:\n" + "\n".join(
            f"Q: {h['q'][:300]}\nA: {h['a'][:300]}" for h in history[-2:]) + "\n\n"
    exe = find_claude()
    if not exe:
        return "Сильная модель недоступна: claude CLI не найден."
    cmd = [
        exe, "--print", "--dangerously-skip-permissions",
        "--model", CLAUDE_MODEL,
        "--append-system-prompt", sys_prompt,
        "-p", hist + question,
    ]
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", timeout=600,
                           shell=False)
        out = (r.stdout or "").strip()
        if r.returncode != 0 or not out:
            return f"Ошибка сильной модели (код {r.returncode}): {(r.stderr or '')[:300]}"
        return out
    except FileNotFoundError:
        return "Сильная модель недоступна: claude CLI не найден."
    except subprocess.TimeoutExpired:
        return "Сильная модель не ответила за 10 минут — попробуй ещё раз."


# ------------------------------------------------------------------- main

def load_catalog():
    try:
        with open(CATALOG, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return "", {}
    titles = {}
    # catalog line format: "- <id> | <title> | <lang> | <topics>"
    for line in text.splitlines():
        m = re.match(r"^-\s*([\w][\w.-]*)\s*\|\s*([^|]+)", line)
        if m:
            titles[m.group(1).strip()] = m.group(2).strip()
    return text, titles


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--prompt", required=True)
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args()
    question = args.prompt.strip()

    t0 = time.time()
    history = [] if args.fresh else load_history()
    if args.fresh:
        save_history([])

    force_pro = bool(re.match(r"^(\[From:[^\]]*\]\s*)?(\[Replying[^\]]*\]\s*)*PRO:", question))
    has_file = "[Image attached:" in question or "[File attached:" in question

    if force_pro or has_file:
        q = re.sub(r"PRO:\s*", "", question, count=1) if force_pro else question
        reply = escalate_claude(q, history)
        print(reply)
        history.append({"q": q, "a": reply})
        save_history(history)
        return

    catalog_text, titles = load_catalog()
    if not catalog_text:
        print("Индекс ещё не построен (index/catalog.md отсутствует). "
              "Запусти Update-Standards или обратись к Анри.")
        return

    if ROUTER == "llm":
        terms, doc_ids = route(question, catalog_text, history)
    else:
        terms, doc_ids = route_lexical(question, load_meta_index())
    # Small models occasionally hallucinate ids — keep only real ones. An empty
    # list makes retrieve() scan the whole corpus, which is the safe fallback.
    doc_ids = [d for d in doc_ids if d in titles]
    log(f"route {time.time()-t0:.1f}s: terms={terms} docs={doc_ids}")
    if doc_ids:
        names = ", ".join(titles.get(d, d) for d in doc_ids[:5])
        more = f" (+{len(doc_ids)-5})" if len(doc_ids) > 5 else ""
        progress(f"📚 Смотрю: {names}{more}")

    chunks = retrieve(doc_ids, terms)
    log(f"retrieve {time.time()-t0:.1f}s: {len(chunks)} chunks")

    if not chunks:
        if AUTO_ESCALATE:
            reply = escalate_claude(question, history)
        else:
            reply = ("По этим словам ничего не нашёл в документах. "
                     "Попробуй переформулировать или спроси сильную модель: "
                     "/pro " + question[:150])
        print(reply)
        history.append({"q": question, "a": reply})
        save_history(history)
        return

    reply = answer(question, chunks, history, titles)
    log(f"answer {time.time()-t0:.1f}s")

    if "NOT_FOUND" in reply:
        if AUTO_ESCALATE:
            reply = escalate_claude(question, history)
        else:
            reply = ("В найденных фрагментах прямого ответа нет. "
                     "Попробуй переформулировать или спроси сильную модель: "
                     "/pro " + question[:150])

    print(reply)
    history.append({"q": question, "a": reply})
    save_history(history)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # the bridge shows stdout; keep failures readable
        print(f"Внутренняя ошибка помощника: {e}")
        raise
