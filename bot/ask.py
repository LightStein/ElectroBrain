#!/usr/bin/env python3
"""ask.py — answering engine CLI for George's standards assistant.

Invoked by the bridge once per turn:
    python ask.py -p "<message>" [--fresh]

Stdout = the reply (bridge BRIDGE_OUTPUT=plain). Progress lines go to
./.progress (the bridge tails it and forwards lines to Telegram).

Pipeline:
  A. lexical routing: pick candidate documents and bilingual search terms
     from meta.json keywords - no model, no latency
  B. retrieval: score heading-delimited chunks of those documents' full.md
     by term hits (tf * idf-lite), take the top chunks
  C. ONE Claude API call: the retrieved chunks plus the question, no tools

Stage C used to be a local Ollama model with the Claude Code CLI behind it as
an escalation. Both are gone, and the measurements are in the comments by
API_MODEL and answer_claude(): the local model answered under half the
questions, needed 42-112s because 7B does not fit 4GB of VRAM, and once
inverted a safety-critical wire colour; the CLI escalation re-derived
retrieval with grep at 195k-1.1M tokens per question for work stage B had
already done. One stateless call with the chunks costs ~20k tokens, which is
why the strongest model is now cheaper than the weakest setup we began with.

Every answer passes four guards before George sees it (check_answer): a
citation must be present, relevant to the question, quoting text that exists
in the retrieved chunks, and citing a clause number that exists there too.

Configuration via environment (all optional):
  STANDARDS_ROOT       root folder (default: parent of this script's directory)
  ANTHROPIC_API_KEY    required - the engine cannot answer without it
  ASK_API_MODEL        default claude-opus-5-5
  ASK_API_EFFORT       low|medium|high|xhigh|max, default high
  ASK_API_MAX_TOKENS   default 8000
  ASK_API_TIMEOUT      seconds, default 600
  ASK_HISTORY_FILE     default <root>/state/ask-history.json
  ASK_ANSWER_LOG       default <root>/state/answers.jsonl
  ASK_ANSWER_PROMPT    default <script dir>/answer-prompt.md
  ASK_MAX_CHUNK_CHARS  total retrieval budget, default 60000
  ASK_MAX_CHUNKS       default 40
  ASK_MAX_CHUNKS_PER_DOC  default 6
"""

import argparse
import base64
import datetime
import json
import math
import os
import re
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("STANDARDS_ROOT", os.path.dirname(SCRIPT_DIR))
# The engine is the Claude API, called ONCE per question with the chunks
# retrieval already selected. It used to be a local Ollama model with the
# Claude Code CLI as an escalation, and that arrangement was measured and
# abandoned: the local model answered 3-4 of 8 questions, took 42-112s
# because a 7B model does not fit 4GB of VRAM, and once told a revisor that
# the protective-earth conductor is blue (it is yellow-green; blue is the
# neutral) citing a clause that does not exist. The CLI escalation that
# rescued those answers re-did retrieval itself with grep, costing
# 195k-1.1M tokens per question for work ask.py had already done.
#
# Sending the retrieved chunks instead costs ~20k tokens, which makes the
# most capable model cheaper than the weakest arrangement we started with.
API_MODEL = os.environ.get("ASK_API_MODEL", "claude-opus-5-5")
# Thinking is always on for this model and cannot be disabled; effort is the
# only depth control and its default is "medium". George's stated priority is
# precision over speed, so this is set explicitly rather than left to default.
API_EFFORT = os.environ.get("ASK_API_EFFORT", "high")
API_MAX_TOKENS = int(os.environ.get("ASK_API_MAX_TOKENS", "8000"))
API_TIMEOUT = float(os.environ.get("ASK_API_TIMEOUT", "600"))
# Only needed for a key that is not scoped to a workspace (sk-ant-usr-...).
API_WORKSPACE_ID = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
ANSWER_PROMPT_FILE = os.environ.get(
    "ASK_ANSWER_PROMPT", os.path.join(SCRIPT_DIR, "answer-prompt.md"))
# Every answer is appended here with its token usage. Nobody could previously
# see what George had been told - the one person able to check an answer had
# no way to read any, which is the gap that let a wrong answer go unnoticed.
ANSWER_LOG = os.environ.get("ASK_ANSWER_LOG",
                            os.path.join(ROOT, "state", "answers.jsonl"))
# $/MTok (input, cache-read, output), for the cost estimate in the log only.
PRICES = {
    "claude-opus-5-5":   (4.0, 0.20, 20.0),
    "claude-opus-5":     (5.0, 0.25, 25.0),
    "claude-sonnet-5-5": (2.0, 0.20, 10.0),
    "claude-haiku-4-5":  (1.0, 0.10, 5.0),
}

HISTORY_FILE = os.environ.get("ASK_HISTORY_FILE", os.path.join(ROOT, "state", "ask-history.json"))
# The old 6000-char ceiling was a measured property of a 4B model, not of the
# task: at 14000 it scored WORSE, because a small model dilutes rather than
# finds. That ceiling does not apply to the model answering now, and the
# system was losing to recall - every answer was drawn from 0.04% of a 15.2MB
# corpus. ~60k chars is ~20k tokens, roughly $0.10 a question on Opus 5.5.
MAX_CHUNK_CHARS = int(os.environ.get("ASK_MAX_CHUNK_CHARS", "60000"))
# Per-chunk size. At 2500 the budget fit only TWO chunks, so a single
# document could take both slots and crowd out the one that actually held the
# answer - observed with a switch-height question where СП 256 was correctly
# shortlisted but never made it into the context.
CHUNK_CHARS = int(os.environ.get("ASK_CHUNK_CHARS", "1200"))
MAX_CHUNKS_PER_DOC = int(os.environ.get("ASK_MAX_CHUNKS_PER_DOC", "6"))
# The question's own words matter far more than keywords expanded from
# meta.json; weighting them equally is what let reference lists outrank real
# clauses.
QUESTION_TERM_WEIGHT = 3.0
EXPANDED_TERM_WEIGHT = 1.0
# An acronym or standard designation in the question is close to a filter:
# if the user says TN-C or IP44, chunks containing it are almost certainly
# the right ones.
DESIGNATION_WEIGHT = 8.0
EXPANDED_QUERY_WEIGHT = 2.0
MAX_CHUNKS = int(os.environ.get("ASK_MAX_CHUNKS", "40"))
# A chunk that is mostly "ГОСТ Р 55842-2013 (ИСО 30061:2007) ..." is a
# normative-references list. It matches many terms and answers nothing.
REFLIST_RE = re.compile(r"(ГОСТ|МЭК|ИСО|IEC|ISO|СП|СНиП|EN)\s*[Р\s]*[\d.\-]{3,}", re.I)

# A source line: the 📄 marker, or an explicit clause reference.
CITATION_RE = re.compile(r"📄|\bп\.\s*\d|\bпункт\s*\d", re.I)
# The verbatim clause the answer claims to be quoting.
QUOTE_RE = re.compile(r"«([^»]{8,})»")

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


def route_lexical(question, metas, max_docs=12, expanded=None):
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
    # Model-supplied synonyms sit between the question's own words and the
    # catalogue keywords: more trustworthy than a keyword that merely tagged
    # the document, less than what the user actually typed.
    for t in (expanded or []):
        for w in TOKEN_RE.findall(t):
            if is_designation(w):
                terms.setdefault(w, DESIGNATION_WEIGHT)
            elif len(w) >= 4 and w.lower() not in STOPWORDS:
                terms.setdefault(w.lower(), EXPANDED_QUERY_WEIGHT)
    # expansion also widens the document shortlist
    qt = qt | {norm_token(w) for t in (expanded or [])
               for w in TOKEN_RE.findall(t) if len(w) >= 4}

    for doc_id, meta in metas.items():
        ru = meta.get("keywords_ru") or []
        en = meta.get("keywords_en") or []
        topics = meta.get("topics") or []
        title = meta.get("title") or ""

        score, hits = 0.0, []
        # Corpus-derived vocabulary: what the document actually talks about,
        # as opposed to what its opening pages announce. No aligned twin, so
        # it is matched on its own.
        #
        # This block used to sit ABOVE the initialisation on the line before.
        # On the first document that was an UnboundLocalError - a hard crash
        # of the whole request, surfacing to George as "Внутренняя ошибка
        # помощника" - and on every later document it scored the PREVIOUS
        # document instead, then discarded it, while hits.append() polluted
        # that document's search terms after they had already been recorded.
        # So the feature never once contributed to routing.
        for kw in (meta.get("keywords_idf") or []):
            if any(t.startswith(kw[:5]) or kw.startswith(t[:5]) for t in qt if len(t) > 4):
                score += 2.0
                hits.append(kw)
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
        if len(out) >= MAX_CHUNKS:
            break
    return out


# --------------------------------------------------------- stage C: answer

def build_context(ctx_chunks, titles_by_id):
    parts = []
    for did, ch in ctx_chunks:
        title = titles_by_id.get(did, did)
        parts.append("===== %s (id: %s) =====\n%s" % (title, did, ch))
    return "\n\n".join(parts)


def _image_blocks(question):
    """Telegram photos arrive as a '[Image attached: <path>]' marker."""
    blocks = []
    for path in re.findall(r"\[Image attached:\s*([^\]]+?)\]", question):
        path = path.strip()
        ext = os.path.splitext(path)[1].lower()
        media = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".gif": "image/gif", ".webp": "image/webp"}.get(ext)
        if not media or not os.path.isfile(path):
            log("image skipped (unsupported or missing): %s" % path)
            continue
        try:
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode("ascii")
        except OSError as e:
            log("image unreadable: %s" % e)
            continue
        blocks.append({"type": "image",
                       "source": {"type": "base64", "media_type": media, "data": data}})
    return blocks


def answer_claude(question, ctx_chunks, history, titles_by_id):
    """One stateless call: the chunks we already found, and the question.

    The predecessor to this function told the model to find the documents
    itself with grep, which cost 195k-1.1M input tokens per question - for
    retrieval stage B had already done and then discarded. Passing the chunks
    instead is ~20k tokens, so the strongest model now costs less than the
    weakest arrangement did.

    No tools are given deliberately. Without them there is no agentic loop,
    so there is no turn-count variance, nothing to cap with --max-turns (which
    returned an empty answer on 1 of 2 test questions), and no path by which
    text typed into a Telegram chat can reach a shell.
    """
    import anthropic

    try:
        with open(ANSWER_PROMPT_FILE, encoding="utf-8") as f:
            sys_prompt = f.read().strip()
    except OSError:
        sys_prompt = ("Отвечай по-русски, только по приведённым фрагментам "
                      "стандартов, всегда указывай документ, пункт и точную "
                      "цитату. Если ответа во фрагментах нет - скажи прямо.")

    content = []
    content.extend(_image_blocks(question))
    clean_q = re.sub(r"\[(?:Image|File) attached:[^\]]*\]", "", question).strip()
    content.append({"type": "text",
                    "text": "ФРАГМЕНТЫ ДОКУМЕНТОВ:\n%s\n\nВОПРОС: %s"
                            % (build_context(ctx_chunks, titles_by_id), clean_q)})

    msgs = []
    for h in history[-3:]:
        msgs.append({"role": "user", "content": h["q"]})
        msgs.append({"role": "assistant", "content": h["a"][:800]})
    msgs.append({"role": "user", "content": content})

    # A user-scoped key (sk-ant-usr-...) belongs to no single workspace, so the
    # API requires the workspace to be named per request; a workspace-scoped
    # key carries it already and needs nothing here.
    headers = {}
    if API_WORKSPACE_ID:
        headers["anthropic-workspace-id"] = API_WORKSPACE_ID
    client = anthropic.Anthropic(timeout=API_TIMEOUT,
                                 default_headers=headers or None)
    kwargs = dict(model=API_MODEL, max_tokens=API_MAX_TOKENS, system=sys_prompt,
                  thinking={"type": "adaptive"},
                  output_config={"effort": API_EFFORT}, messages=msgs)
    try:
        # Server-side fallback: if a safety classifier declines, the API reruns
        # the request on another model within the same call instead of handing
        # George an error. Unlikely on published electrical standards, but the
        # failure it prevents is silent.
        resp = client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    except anthropic.BadRequestError as e:
        # If the beta is ever withdrawn, answering still matters more than the
        # fallback does.
        log("fallback beta rejected (%s); retrying without it" % str(e)[:120])
        resp = client.messages.create(**kwargs)

    if resp.stop_reason == "refusal":
        cat = getattr(resp.stop_details, "category", None) if resp.stop_details else None
        log("refused by safety classifier: %s" % cat)
        return ("Модель отказалась отвечать на этот вопрос. "
                "Переформулируй или напиши Анри."), resp.usage
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if resp.stop_reason == "max_tokens":
        log("answer truncated at max_tokens")
    return text, resp.usage


def usage_cost(usage):
    """Estimated $ for one call. For the log - not a billing record."""
    rates = PRICES.get(API_MODEL)
    if not rates or usage is None:
        return None
    inp, cached, out = rates
    g = lambda n: getattr(usage, n, 0) or 0
    return round((g("input_tokens") * inp
                  + g("cache_read_input_tokens") * cached
                  + g("cache_creation_input_tokens") * inp * 1.25
                  + g("output_tokens") * out) / 1e6, 5)


def log_answer(question, reply, usage, doc_ids, verdict):
    """Append-only record so a wrong answer can be found after the fact."""
    g = lambda n: (getattr(usage, n, 0) or 0) if usage else 0
    row = {
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
        "model": API_MODEL, "effort": API_EFFORT,
        "question": question[:500], "answer": reply[:4000],
        "docs": list(doc_ids)[:12], "verdict": verdict,
        "in_tokens": g("input_tokens") + g("cache_read_input_tokens")
                     + g("cache_creation_input_tokens"),
        "out_tokens": g("output_tokens"), "est_usd": usage_cost(usage),
    }
    try:
        os.makedirs(os.path.dirname(ANSWER_LOG), exist_ok=True)
        with open(ANSWER_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        log("answer log failed: %s" % e)



# -------------------------------------------------------- citation relevance

def cited_quotes(reply):
    """The clause texts an answer claims to be quoting."""
    quotes = QUOTE_RE.findall(reply)
    if quotes:
        return quotes
    # Some answers cite without quote marks. Fall back to whatever follows the
    # document reference on a citation line.
    out = []
    for ln in reply.splitlines():
        if CITATION_RE.search(ln) and ":" in ln:
            tail = ln.split(":", 1)[1].strip()
            if len(tail) >= 8:
                out.append(tail)
    return out


def citation_is_relevant(question, reply):
    """Does the quoted clause actually talk about what was asked?

    Checking the shape of a citation only proves the model typed a clause
    number. In testing one answer cited ПУЭ 1.7.8 for an unrelated question -
    correct format, wrong clause - and passed. That is the failure most likely
    to mislead a revisor, because it reads as sourced and gives him a specific
    reference to act on.

    Deliberately lenient: one shared content word is enough. Standards phrase
    things in their own vocabulary (a clause about УДТ answers a question about
    УЗО), and the two errors are not symmetric - escalating a correct answer
    costs one Claude call, while showing a confidently mis-sourced one costs
    George's trust in the whole tool. When in doubt, let it through and let the
    quote speak for itself; he is told to verify against the original anyway.
    """
    quotes = cited_quotes(reply)
    if not quotes:
        return True                      # nothing quoted; the shape check stands
    want = question_tokens(question)
    if not want:
        return True                      # nothing to match against
    got = set()
    for q in quotes:
        got |= question_tokens(q)
    return bool(want & got)


# --------------------------------------------------------- quote grounding

# Verified against the text actually retrieved, in 5-word shingles, most of
# which must be present. Exact matching is too brittle - OCR leaves stray
# spacing and models normalise punctuation - while a whole-quote similarity
# score would let a real opening clause carry a fabricated ending. Requiring
# most short runs to exist means an invented phrase fails wherever it was
# invented.
QUOTE_SHINGLE = 5
QUOTE_MATCH_MIN = 0.6


def _norm_words(text):
    return re.findall(r"[a-zа-я0-9]+", text.lower().replace("ё", "е"))


def quotes_are_grounded(reply, chunks):
    """Does every «quote» in the answer actually occur in what we retrieved?

    The failure this exists for: asked what colour a protective earth
    conductor is, the local model answered "синего цвета" - blue is the
    NEUTRAL - and attributed it to a clause number that does not exist,
    quoting a sentence that appears nowhere in the corpus. It passed the
    citation-shape check and the vocabulary-relevance check, because the
    fabricated quote naturally contained the question's own words.

    Shape and topicality cannot catch invention. Only the source can. This is
    a string search over text we already hold, so it is deterministic and
    free - no model is asked to grade another model's honesty.
    """
    quotes = QUOTE_RE.findall(reply)
    if not quotes:
        return True, ""
    hay_words = []
    for _, ch in chunks:
        hay_words.extend(_norm_words(ch))
    hay = " ".join(hay_words)
    if not hay:
        return True, ""
    for q in quotes:
        qw = _norm_words(q)
        if not qw:
            continue
        if len(qw) < QUOTE_SHINGLE:
            if " ".join(qw) not in hay:
                return False, q[:90]
            continue
        sh = [" ".join(qw[i:i + QUOTE_SHINGLE])
              for i in range(len(qw) - QUOTE_SHINGLE + 1)]
        hits = sum(1 for s in sh if s in hay)
        if hits / float(len(sh)) < QUOTE_MATCH_MIN:
            return False, q[:90]
    return True, ""


# ------------------------------------------------------- clause verification

# Any dotted clause-like number occurring in the text we retrieved.
CLAUSE_PRESENT_RE = re.compile(r"\b\d+(?:\.\d+){1,3}\b")
# A clause the answer claims to cite. Only dotted forms are checked: a bare
# "п. 7" cannot be told apart from an ordinary number in the source text, and
# a guard that fires on ambiguity gets switched off.
CLAUSE_CITED_RE = re.compile(r"(?:п\.|пункт|§)\s*(\d+(?:\.\d+){1,3})", re.I)


def cited_clauses_exist(reply, chunks):
    """Does every clause number the answer cites appear in the source?

    Quote grounding checks the TEXT of a citation. It cannot catch a real
    quote from one clause labelled with another clause's number - shape,
    vocabulary and grounding all pass, and the result is a verbatim quote
    under a number that does not say it. For an inspector copying a reference
    into a certification document that is worse than an obviously absurd
    answer, because nothing about it looks wrong.

    The number that started this work, 12.1.030, was never a clause at all:
    it was "ГОСТ 12.1.030" broken across lines by the PDF and promoted to a
    heading by the pipeline. The pipeline no longer does that, and this
    refuses to cite such a number even if one reappears.
    """
    present = set()
    for _, ch in chunks:
        present.update(CLAUSE_PRESENT_RE.findall(ch))
    for num in CLAUSE_CITED_RE.findall(reply):
        if num not in present:
            return False, num
    return True, ""



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


def check_answer(question, reply, chunks):
    """All four guards, in one place. Returns (reason, message_for_george).

    These used to run only on the local model's answers and not at all on the
    escalated ones - which, at 5-6 escalations per 9 questions, meant most of
    what George read passed through no check whatsoever. The guards protected
    the path that was distrusted and skipped the path that was relied on.
    There is one path now, and everything on it is checked.
    """
    if "NOT_FOUND" in reply:
        return ("model found nothing usable in the retrieved fragments",
                "В найденных фрагментах прямого ответа нет. Попробуй "
                "переформулировать — или, если документа в базе нет, добавь его.")
    if not CITATION_RE.search(reply):
        return ("no citation in the answer",
                "Нашёл похожий текст, но не смог указать точный пункт "
                "документа — не показываю такой ответ.")
    if not citation_is_relevant(question, reply):
        return ("cited clause shares no vocabulary with the question",
                "Нашёл ссылку на пункт, но он не про то, о чём вопрос — "
                "не показываю такой ответ.")
    grounded, bad_quote = quotes_are_grounded(reply, chunks)
    if not grounded:
        return ("quoted text is not in the retrieved documents: %r" % bad_quote,
                "Ответ ссылался на цитату, которой нет в документах — "
                "не показываю такой ответ.")
    ok, bad_num = cited_clauses_exist(reply, chunks)
    if not ok:
        return ("cited clause %s does not appear in the retrieved documents" % bad_num,
                "Ответ ссылался на пункт %s, которого нет в найденных "
                "документах — не показываю такой ответ." % bad_num)
    return None, None


def found_sources(chunks, titles):
    """What we actually retrieved, so a refusal still points somewhere."""
    seen, out = set(), []
    for did, ch in chunks:
        if did in seen:
            continue
        seen.add(did)
        m = re.search(r"^###\s*(\d+(?:\.\d+){1,3})", ch, re.M)
        where = (", п. " + m.group(1)) if m else ""
        out.append("• %s%s" % (titles.get(did, did)[:70], where))
        if len(out) >= 5:
            break
    return "\n".join(out)


DOC_EXTS = {".pdf", ".docx", ".doc", ".odt", ".xodt"}
ATTACH_RE = re.compile(r"\[(?:File|Image) attached:\s*([^\]]+?)(?:\s*\(([^)]*)\))?\]")


def attached_documents(question):
    """Standards documents George sent to the chat, as (path, display name).

    The bot downloads an upload into its container and passes the HOST path in
    the prompt text, so the bridge - which runs on the host - can open it
    directly. With staging on, several files plus a caption arrive as one turn,
    which is what makes "here are 3 PDFs" a single ingestion.
    """
    out = []
    for path, name in ATTACH_RE.findall(question):
        path = path.strip()
        if os.path.splitext(path)[1].lower() in DOC_EXTS and os.path.isfile(path):
            out.append((path, (name or os.path.basename(path)).strip()))
    return out


def ingest_documents(docs):
    """Copy into the corpus and reindex. Replaces George's Update-Standards.bat.

    Adding a document used to mean putting it in a folder on his laptop and
    double-clicking a .bat. Sending it to the chat he already asks questions in
    is one less thing to explain, and it is the same path the questions take.
    """
    import shutil
    import subprocess

    raw = os.environ.get("STANDARDS_RAW", os.path.join(ROOT, "corpus"))
    os.makedirs(raw, exist_ok=True)
    added = []
    for path, name in docs:
        safe = re.sub(r"[\\/:*?\"<>|]", "_", name) or os.path.basename(path)
        dest = os.path.join(raw, safe)
        try:
            # EACCES on a file that exists means the Bot API server's 0640
            # root:root download mode; the bot chmods 0644 on download, so this
            # should not happen - but say which file if it does.
            shutil.copyfile(path, dest)
            added.append(safe)
        except OSError as e:
            log("ingest copy failed for %s: %s" % (name, e))
            return ("Не смог прочитать файл «%s»: %s\n"
                    "Перешли его ещё раз или напиши Анри." % (name, e))
    if not added:
        return "Не нашёл, что добавить."

    progress("📥 Добавляю %d док. в базу — это займёт минуту…" % len(added))
    env = dict(os.environ)
    env.update({"STANDARDS_ROOT": ROOT, "STANDARDS_RAW": raw,
                "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    r = subprocess.run([sys.executable,
                        os.path.join(ROOT, "pipeline", "update.py")],
                       capture_output=True, text=True, encoding="utf-8",
                       cwd=ROOT, env=env, timeout=3600)
    if r.returncode != 0:
        log("pipeline failed: %s" % (r.stderr or "")[-600:])
        return ("Файл сохранил, но не смог построить индекс. "
                "Напиши Анри — он посмотрит.\n\n" + ", ".join(added))
    total = len(os.listdir(DOCS_DIR)) if os.path.isdir(DOCS_DIR) else 0
    return ("Готово. Добавил:\n• " + "\n• ".join(added)
            + "\n\nВсего документов в базе: %d. Можно спрашивать." % total)


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

    # "/pro" used to switch engines. There is only one engine now, so the
    # prefix is accepted and ignored rather than failing in George's face.
    question = re.sub(r"^(\[From:[^\]]*\]\s*)?(\[Replying[^\]]*\]\s*)*PRO:\s*",
                      lambda m: m.group(0).replace("PRO:", "").rstrip(),
                      question, count=1)

    # A document sent to the chat is an index request, not a question. Handled
    # before routing because there is nothing to retrieve from yet, and the
    # bridge serialises turns, so this cannot race a question mid-reindex.
    docs = attached_documents(question)
    if docs:
        reply = ingest_documents(docs)
        print(reply)
        log_answer(question, reply, None, [], "ingest")
        history.append({"q": "[добавление документов]", "a": reply})
        save_history(history)
        return

    catalog_text, titles = load_catalog()
    if not catalog_text:
        # Adding a document is now "send it to this chat", not a .bat on a
        # laptop, so the empty-index message has to say that.
        print("База документов пока пустая. Пришли сюда файлы стандартов "
              "(PDF, DOC, DOCX) — я их добавлю, и можно будет спрашивать.")
        return

    terms, doc_ids = route_lexical(question, load_meta_index())
    # The router works from model-written keywords and can name an id that no
    # longer exists; an empty list makes retrieve() scan everything, which is
    # the safe fallback.
    doc_ids = [d for d in doc_ids if d in titles]
    log(f"route {time.time()-t0:.1f}s: terms={terms} docs={doc_ids}")
    if doc_ids:
        names = ", ".join(titles.get(d, d) for d in doc_ids[:5])
        more = f" (+{len(doc_ids)-5})" if len(doc_ids) > 5 else ""
        progress(f"📚 Смотрю: {names}{more}")

    chunks = retrieve(doc_ids, terms)
    log(f"retrieve {time.time()-t0:.1f}s: {len(chunks)} chunks")
    if not chunks:
        reply = ("По этим словам ничего не нашёл в документах. Попробуй "
                 "переформулировать — например «розетка» вместо «штепсель».")
        print(reply)
        log_answer(question, reply, None, doc_ids, "no-chunks")
        history.append({"q": question, "a": reply})
        save_history(history)
        return

    progress("🧠 Читаю найденные пункты…")
    try:
        reply, usage = answer_claude(question, chunks, history, titles)
    except Exception as e:
        # Network down, key missing, service error - all look the same to
        # George, so say the one thing he can act on. There is no local model
        # behind this any more: no connection means no answer, which is the
        # honest outcome rather than a weak guess.
        name = type(e).__name__
        log(f"api call failed: {name}: {e}")
        reply = ("Не удалось связаться с сервером Claude. Проверь, что "
                 "ноутбук в сети, и попробуй ещё раз. Если повторяется — "
                 "напиши Анри.")
        print(reply)
        log_answer(question, reply, None, doc_ids, "api-error:" + name)
        return
    log(f"answer {time.time()-t0:.1f}s")

    reason, message = check_answer(question, reply, chunks)
    if reason:
        log(f"answer rejected: {reason}")
        sources = found_sources(chunks, titles)
        reply = message
        if sources:
            # A rejection that names where to look is still useful; a bare
            # "no" sends him back to the 4-5 hours this is meant to replace.
            reply += "\n\nСмотрел здесь — проверь сам:\n" + sources
    log_answer(question, reply, usage, doc_ids, reason or "ok")

    print(reply)
    history.append({"q": question, "a": reply})
    save_history(history)



if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # the bridge shows stdout; keep failures readable
        print(f"Внутренняя ошибка помощника: {e}")
        raise
