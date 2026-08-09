#!/usr/bin/env python3
"""update.py — the whole document pipeline, resumable, driven by a manifest.

Stages per document (recorded in state/manifest.json, safe to re-run anytime):
  scan       hash files, classify PDFs text/scanned/mixed, detect language
  extract    digital PDF -> pymupdf; docx/odt -> pandoc; legacy .doc ->
             LibreOffice -> pandoc; scanned PDF -> ocrmypdf (tesseract rus+eng)
  markdown   MECHANICAL, no model: strip page furniture and watermarks, join
             hyphenated line breaks, promote clause numbers to headings ->
             index/docs/<id>/full.md, wording left verbatim
  meta       claude -p reads a SAMPLE and writes only meta.json + a catalog
             line (see cleanup-prompt.md)
  (removal)  files deleted from the raw folder -> index entries removed

Why the split: this corpus is 16.3M chars (~5.4M tokens). Having a model
rewrite all of it would take days, exhaust plan limits, and let it silently
"correct" clause numbers and measurements - unacceptable when a revisor cites
them. So the text stays verbatim and the model is used only for judgement:
titles, topics, and aligned RU/EN keywords for cross-language search.

Usage:
  python update.py                 run everything that's pending
  python update.py --scan-only     just scan + write state/inventory_report.md
  python update.py --skip-claude   stop after extraction/OCR (no cleanup)
  python update.py --limit N       process at most N docs through cleanup

Deps: pip install pymupdf ; ocrmypdf + tesseract (rus+eng) ; pandoc ; claude CLI.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import datetime

ROOT = os.environ.get("STANDARDS_ROOT",
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# George keeps his documents in his own folder; point at it rather than
# duplicating them. The pipeline only ever READS here (hash + extract), so
# aiming it at a live working folder is safe, and deletions there correctly
# propagate to index removals.
RAW = os.environ.get("STANDARDS_RAW", os.path.join(ROOT, "raw"))
WORK = os.path.join(ROOT, "work")
EXTRACTED = os.path.join(WORK, "extracted")
OCRED = os.path.join(WORK, "ocred")
INDEX = os.path.join(ROOT, "index")
DOCS = os.path.join(INDEX, "docs")
CATALOG = os.path.join(INDEX, "catalog.md")
STATE = os.path.join(ROOT, "state")
MANIFEST = os.path.join(STATE, "manifest.json")
REPORT = os.path.join(STATE, "inventory_report.md")
CLEANUP_PROMPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cleanup-prompt.md")
# Opening slice handed to Claude for metadata. Standards state their title,
# scope and contents up front, so this is plenty and keeps the pass cheap.
SAMPLE_CHARS = int(os.environ.get("STANDARDS_SAMPLE_CHARS", "14000"))
META_MODEL = os.environ.get("STANDARDS_META_MODEL", "haiku")

# .xodt is not a real format - it is an ODT with a typo'd extension, and
# George's corpus contains one. Identified by magic bytes, handled as odt.
DOC_EXTS = {".pdf", ".docx", ".doc", ".odt", ".xodt"}
PANDOC_FMT = {".docx": "docx", ".odt": "odt", ".xodt": "odt"}
TEXT_CHARS_PER_PAGE = 50   # fewer extractable chars than this = image page


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


CLAUDE_EXE = find_claude()


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def slugify(name):
    base = os.path.splitext(os.path.basename(name))[0]
    base = unicodedata.normalize("NFKD", base)
    # Transliterate nothing — keep it ASCII-safe and short but unique-ish.
    slug = re.sub(r"[^A-Za-z0-9а-яА-Я]+", "-", base).strip("-").lower()
    slug = re.sub(r"-{2,}", "-", slug)[:60]
    return slug or "doc"


def load_manifest():
    try:
        with open(MANIFEST, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"files": {}}


def save_manifest(m):
    os.makedirs(STATE, exist_ok=True)
    tmp = MANIFEST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=1)
    os.replace(tmp, MANIFEST)


def guess_lang(text):
    cyr = len(re.findall(r"[а-яА-Я]", text))
    lat = len(re.findall(r"[a-zA-Z]", text))
    if cyr + lat < 40:
        return "?"
    return "ru" if cyr > lat else "en"


# ------------------------------------------------------------------ scan

def classify_pdf(path):
    import fitz  # pymupdf
    doc = fitz.open(path)
    pages = doc.page_count
    text_pages = 0
    sample = []
    for page in doc:
        t = page.get_text()
        if len(t.strip()) >= TEXT_CHARS_PER_PAGE:
            text_pages += 1
            if len(sample) < 3:
                sample.append(t[:2000])
    doc.close()
    if pages == 0:
        kind = "empty"
    elif text_pages >= pages * 0.9:
        kind = "text"
    elif text_pages <= pages * 0.1:
        kind = "scanned"
    else:
        kind = "mixed"
    return kind, pages, guess_lang("\n".join(sample))


def stage_scan(m):
    os.makedirs(RAW, exist_ok=True)
    seen = set()
    changed = 0
    for name in sorted(os.listdir(RAW)):
        p = os.path.join(RAW, name)
        if not os.path.isfile(p) or os.path.splitext(name)[1].lower() not in DOC_EXTS:
            continue
        seen.add(name)
        digest = sha256(p)
        rec = m["files"].get(name)
        if rec and rec.get("sha256") == digest:
            continue
        # new or changed file
        doc_id = rec["doc_id"] if rec else slugify(name)
        # keep doc ids unique
        taken = {r["doc_id"] for k, r in m["files"].items() if k != name}
        while doc_id in taken:
            doc_id += "-x"
        ext = os.path.splitext(name)[1].lower()
        if ext == ".pdf":
            try:
                kind, pages, lang = classify_pdf(p)
            except Exception as e:
                log(f"scan: cannot read {name}: {e}")
                kind, pages, lang = "error", 0, "?"
        else:
            kind, pages, lang = "office", None, "?"
        m["files"][name] = {
            "sha256": digest, "doc_id": doc_id, "kind": kind,
            "pages": pages, "lang": lang, "status": "scanned",
        }
        changed += 1
        log(f"scan: {name} -> id={doc_id} kind={kind} pages={pages} lang={lang}")

    # removals
    removed = [name for name in list(m["files"]) if name not in seen]
    for name in removed:
        rec = m["files"].pop(name)
        remove_from_index(rec["doc_id"])
        log(f"removed: {name} (id={rec['doc_id']}) — index entry deleted")
    save_manifest(m)
    return changed, removed


def write_report(m):
    lines = ["# Inventory report", f"Generated: {datetime.now():%Y-%m-%d %H:%M}", ""]
    counts = {}
    for name, r in sorted(m["files"].items()):
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
        lines.append(f"- `{name}` — id `{r['doc_id']}`, {r['kind']}, "
                     f"{r.get('pages') or '?'} p., lang {r.get('lang')}, status {r['status']}")
    lines.insert(2, "Totals: " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    os.makedirs(STATE, exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log(f"report -> {REPORT}")


# ------------------------------------------------------- extract / ocr

def stage_extract(m):
    os.makedirs(EXTRACTED, exist_ok=True)
    os.makedirs(OCRED, exist_ok=True)
    for name, r in m["files"].items():
        if r["status"] != "scanned" or r["kind"] in ("error", "empty"):
            continue
        src = os.path.join(RAW, name)
        out = os.path.join(EXTRACTED, r["doc_id"] + ".txt")
        try:
            if r["kind"] == "office":
                text = extract_doc(src, r["doc_id"])
            elif r["kind"] == "text":
                text = extract_pdf_text(src)
            else:  # scanned / mixed
                text = extract_pdf_ocr(src, r["doc_id"])
            with open(out, "w", encoding="utf-8") as f:
                f.write(text)
            if r["lang"] == "?":
                r["lang"] = guess_lang(text)
            r["status"] = "extracted"
            log(f"extract: {name} -> {len(text)} chars ({r['kind']})")
        except Exception as e:
            r["status"] = "extract_failed"
            r["error"] = str(e)[:300]
            log(f"extract FAILED: {name}: {e}")
        save_manifest(m)


def extract_pdf_text(path):
    import fitz
    doc = fitz.open(path)
    parts = []
    for i, page in enumerate(doc):
        parts.append(f"\n\n[[page {i + 1}]]\n" + page.get_text())
    doc.close()
    return "".join(parts)


def extract_pdf_ocr(path, doc_id):
    """OCR via ocrmypdf (tesseract rus+eng), then extract the text layer.
    --redo-ocr handles 'mixed' docs: existing good text is kept."""
    out_pdf = os.path.join(OCRED, doc_id + ".pdf")
    if not os.path.isfile(out_pdf):
        cmd = ["ocrmypdf", "-l", "rus+eng", "--redo-ocr", "--optimize", "0",
               "--output-type", "pdf", path, out_pdf]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if r.returncode not in (0, 10):  # 10 = already has text (with --redo-ocr rare)
            raise RuntimeError(f"ocrmypdf exit {r.returncode}: {(r.stderr or '')[-400:]}")
    return extract_pdf_text(out_pdf)


def find_soffice():
    p = shutil.which("soffice") or shutil.which("soffice.exe")
    if p:
        return p
    for base in (os.environ.get("ProgramFiles", ""),
                 os.environ.get("ProgramFiles(x86)", "")):
        if base:
            cand = os.path.join(base, "LibreOffice", "program", "soffice.exe")
            if os.path.isfile(cand):
                return cand
    return None


def soffice_to_docx(path):
    """Legacy OLE2 .doc -> .docx. Neither pandoc nor python-docx can read the
    old binary Word format; LibreOffice headless is the reliable converter."""
    exe = find_soffice()
    if not exe:
        raise RuntimeError("legacy .doc needs LibreOffice (winget install "
                           "TheDocumentFoundation.LibreOffice)")
    outdir = os.path.join(WORK, "converted")
    os.makedirs(outdir, exist_ok=True)
    subprocess.run([exe, "--headless", "--norestore", "--convert-to", "docx",
                    "--outdir", outdir, path],
                   capture_output=True, timeout=900)
    out = os.path.join(outdir,
                       os.path.splitext(os.path.basename(path))[0] + ".docx")
    if not os.path.isfile(out):
        raise RuntimeError("LibreOffice produced no output for this .doc")
    return out


def extract_doc(path, doc_id):
    """docx/odt via pandoc; legacy .doc via LibreOffice first."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".doc":
        path = soffice_to_docx(path)
        ext = ".docx"
    pandoc = shutil.which("pandoc")
    fmt = PANDOC_FMT.get(ext)
    if pandoc and fmt:
        # -f is explicit: pandoc guesses format from the extension, and
        # .xodt would otherwise be unrecognised.
        r = subprocess.run([pandoc, "-f", fmt, "-t", "gfm", "--wrap=none", path],
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=600)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
    # fallback: python-docx plain paragraphs (docx only)
    from docx import Document  # pip install python-docx
    d = Document(path)
    return "\n\n".join(p.text for p in d.paragraphs)



# ---------------------------------------------- stage: mechanical markdown

PAGE_RE = re.compile(r"\[\[page (\d+)\]\]")
# "1.1.29 Title", "5.2. Title", "10.12 Title" - the clause numbering a revisor
# actually cites. Promoting these to headings is what makes retrieval work at
# all: ask.py splits documents on Markdown headings, so without them a
# 3.4M-char file is one undifferentiated blob.
CLAUSE_RE = re.compile(r"^(\d+(?:\.\d+){0,3})[.)]?\s+(\S.{0,120})$")
SECTION_RE = re.compile(r"^(Приложение|ПРИЛОЖЕНИЕ|Раздел|РАЗДЕЛ|Annex|Section)\b.{0,80}$")
HYPHEN_RE = re.compile(r"(\w)[-­]\n(\w)")


def page_furniture(pages):
    """Lines repeated across many pages: watermarks, running heads, footers.

    One document here carries "Электротехническая библиотека Elec.ru" twice on
    every page. Left in, it pollutes retrieval and wastes context on every
    chunk that quotes it.
    """
    seen = {}
    for pg in pages:
        for line in {l.strip() for l in pg.split("\n") if l.strip()}:
            if len(line) < 90:
                seen[line] = seen.get(line, 0) + 1
    threshold = max(3, int(len(pages) * 0.3))
    return {l for l, n in seen.items() if n >= threshold}


def to_markdown(text, title):
    pages = PAGE_RE.split(text)
    # split() yields [pre, num, body, num, body, ...] - keep the bodies
    bodies = pages[2::2] if len(pages) > 1 else pages
    junk = page_furniture(bodies) if len(bodies) > 2 else set()

    text = "\n".join(bodies)
    text = HYPHEN_RE.sub(r"\1\2", text)          # join hyphenated line breaks

    out, blank = [f"# {title}", ""], False
    for raw in text.split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            if not blank:
                out.append("")
            blank = True
            continue
        if stripped in junk or stripped.isdigit():
            continue                              # watermark / bare page number
        blank = False
        if SECTION_RE.match(stripped):
            out.append(f"\n## {stripped}\n")
        elif CLAUSE_RE.match(stripped):
            out.append(f"\n### {stripped}\n")
        else:
            out.append(line)
    md = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"


def stage_markdown(m):
    """extracted .txt -> index/docs/<id>/full.md, with NO model involved.

    The corpus is 16.3M chars (~5.4M tokens). Rewriting that through Claude
    would take days, exhaust plan limits, and - worse for a revisor - let a
    model silently "correct" clause numbers and measurements. The extracted
    text is already the document's own text layer, so the honest thing is to
    clean it structurally and keep the wording verbatim, which is exactly
    what a citation needs.
    """
    for name, r in m["files"].items():
        if r["status"] != "extracted":
            continue
        src = os.path.join(EXTRACTED, r["doc_id"] + ".txt")
        if not os.path.isfile(src):
            continue
        with open(src, encoding="utf-8") as f:
            text = f.read()
        title = os.path.splitext(name)[0]
        out_dir = os.path.join(DOCS, r["doc_id"])
        os.makedirs(out_dir, exist_ok=True)
        md = to_markdown(text, title)
        with open(os.path.join(out_dir, "full.md"), "w", encoding="utf-8") as f:
            f.write(md)
        r["status"] = "markdown"
        r["md_chars"] = len(md)
        headings = md.count("\n### ") + md.count("\n## ")
        log(f"markdown: {r['doc_id']} -> {len(md)} chars, {headings} headings")
    save_manifest(m)

# ------------------------------------------------------------- cleanup

def stage_meta(m, limit=None):
    """Claude produces ONLY meta.json + the catalog line, from a sample.

    It never sees or rewrites the whole document: full.md is built
    mechanically (see stage_markdown). What a model is genuinely needed for
    here is judgement - the official title, the topics, and RU/EN keyword
    synonyms that let a Russian question find an English document. That is a
    few hundred output tokens per doc instead of rewriting millions.
    """
    if not CLAUDE_EXE:
        log("meta: claude CLI not found - install it and log in, then re-run. "
            "full.md files are already built, so nothing is lost.")
        return
    with open(CLEANUP_PROMPT, encoding="utf-8") as f:
        sys_prompt = f.read()
    todo = [(n, r) for n, r in m["files"].items() if r["status"] == "markdown"]
    if limit:
        todo = todo[:limit]
    log(f"meta: {len(todo)} document(s) to describe")
    for name, r in todo:
        doc_id = r["doc_id"]
        out_dir = os.path.join(DOCS, doc_id)
        md_path = os.path.join(out_dir, "full.md")
        # A sample is enough: standards put the title, scope and contents up
        # front. Reading 3.4M chars of ПУЭ-7 to name it would be absurd.
        try:
            with open(md_path, encoding="utf-8") as f:
                sample = f.read(SAMPLE_CHARS)
        except OSError:
            continue
        sample_file = os.path.join(WORK, "sample.txt")
        with open(sample_file, "w", encoding="utf-8") as f:
            f.write(sample)
        task = (f"Describe one document.\n"
                f"- Original filename: {name}\n"
                f"- doc id: {doc_id}\n"
                f"- Language guess: {r.get('lang')}\n"
                f"- Full text (already built, do NOT rewrite it): {md_path}\n"
                f"- A sample of its opening is at: {sample_file}\n"
                f"- Write ONLY: {out_dir}\\meta.json\n"
                f"- Then append/replace this doc's line in {CATALOG}\n")
        cmd = [CLAUDE_EXE, "--print", "--dangerously-skip-permissions",
               "--model", META_MODEL,
               "--append-system-prompt", sys_prompt, task]
        try:
            res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                                 encoding="utf-8", timeout=900)
            ok = res.returncode == 0 and os.path.isfile(
                os.path.join(out_dir, "meta.json"))
            if ok:
                r["status"] = "done"
                log(f"meta: {doc_id} ok")
            else:
                log(f"meta FAILED: {doc_id} rc={res.returncode} "
                    f"{(res.stderr or '')[-200:]}")
        except subprocess.TimeoutExpired:
            log(f"meta TIMEOUT: {doc_id}")
        save_manifest(m)


def remove_from_index(doc_id):
    shutil.rmtree(os.path.join(DOCS, doc_id), ignore_errors=True)
    try:
        with open(CATALOG, encoding="utf-8") as f:
            lines = f.readlines()
        keep = [l for l in lines if not re.match(rf"^-\s*{re.escape(doc_id)}\s*\|", l)]
        if len(keep) != len(lines):
            with open(CATALOG, "w", encoding="utf-8") as f:
                f.writelines(keep)
    except OSError:
        pass
    for d in (EXTRACTED, OCRED):
        for ext in (".txt", ".pdf"):
            try:
                os.remove(os.path.join(d, doc_id + ext))
            except OSError:
                pass


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan-only", action="store_true")
    ap.add_argument("--skip-claude", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    # User-facing text lives here rather than in Update-Standards.bat: cmd.exe
    # renders Cyrillic in a .bat as mojibake under any codepage, while Python 3
    # writes Unicode straight to the Windows console API.
    print("=" * 50)
    print(" Обновление базы стандартов…")
    print(" Это может занять несколько минут — не закрывай окно.")
    print("=" * 50)

    os.makedirs(INDEX, exist_ok=True)
    os.makedirs(DOCS, exist_ok=True)
    if not os.path.isfile(CATALOG):
        with open(CATALOG, "w", encoding="utf-8") as f:
            f.write("# Каталог стандартов\n\n"
                    "<!-- строки вида: - <id> | <название> | <ru/en> | <темы> -->\n")

    m = load_manifest()
    changed, removed = stage_scan(m)
    write_report(m)
    log(f"scan: {changed} new/changed, {len(removed)} removed, "
        f"{len(m['files'])} total")
    if args.scan_only:
        return

    stage_extract(m)
    stage_markdown(m)
    if not args.skip_claude:
        stage_meta(m, limit=args.limit)

    statuses = {}
    for r in m["files"].values():
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    log("summary: " + ", ".join(f"{k}: {v}" for k, v in sorted(statuses.items())))

    done = statuses.get("done", 0)
    failed = sum(v for k, v in statuses.items() if "fail" in k)
    print()
    print("=" * 50)
    print(f" Готово. Документов в базе: {done}")
    if failed:
        print(f" Не удалось обработать: {failed} — покажи это Анри.")
    print(f" Отчёт: {REPORT}")
    print("=" * 50)


if __name__ == "__main__":
    main()
