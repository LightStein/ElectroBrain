#!/usr/bin/env python3
"""update.py — the whole document pipeline, resumable, driven by a manifest.

Stages per document (recorded in state/manifest.json, safe to re-run anytime):
  scan       hash raw/ files, classify PDFs text/scanned/mixed, detect language
  extract    digital PDF -> pymupdf text; DOCX -> pandoc markdown
  ocr        scanned/mixed PDF -> ocrmypdf (tesseract rus+eng) -> text
  cleanup    claude -p turns extracted text into index/docs/<id>/full.md
             + meta.json + a catalog.md line (see cleanup-prompt.md)
  (removal)  files deleted from raw/ -> their index entries are removed

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
RAW = os.path.join(ROOT, "raw")
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

DOC_EXTS = {".pdf", ".docx", ".doc"}
TEXT_CHARS_PER_PAGE = 50   # fewer extractable chars than this = image page


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
            kind, pages, lang = "docx", None, "?"
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
            if r["kind"] == "docx":
                text = extract_docx(src)
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


def extract_docx(path):
    pandoc = shutil.which("pandoc")
    if pandoc:
        r = subprocess.run([pandoc, "-t", "gfm", "--wrap=none", path],
                           capture_output=True, text=True, encoding="utf-8", timeout=600)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
    # fallback: python-docx plain paragraphs
    from docx import Document  # pip install python-docx
    d = Document(path)
    return "\n\n".join(p.text for p in d.paragraphs)


# ------------------------------------------------------------- cleanup

def stage_cleanup(m, limit=None):
    """claude -p per document: extracted text -> clean full.md + meta.json +
    catalog line. Resumable; a failed doc stays 'extracted' for the next run."""
    with open(CLEANUP_PROMPT, encoding="utf-8") as f:
        sys_prompt = f.read()
    todo = [(n, r) for n, r in m["files"].items() if r["status"] == "extracted"]
    if limit:
        todo = todo[:limit]
    for name, r in todo:
        doc_id = r["doc_id"]
        src_txt = os.path.join(EXTRACTED, doc_id + ".txt")
        out_dir = os.path.join(DOCS, doc_id)
        os.makedirs(out_dir, exist_ok=True)
        task = (f"Process one document.\n"
                f"- Original filename: {name}\n"
                f"- doc id: {doc_id}\n"
                f"- Language guess: {r.get('lang')}\n"
                f"- Was OCR used: {'yes' if r['kind'] in ('scanned', 'mixed') else 'no'}\n"
                f"- Extracted text: {src_txt}\n"
                f"- Write to: {out_dir}\\full.md and {out_dir}\\meta.json\n"
                f"- Then append/replace this doc's line in {CATALOG}\n")
        log(f"cleanup: {doc_id} ({name}) ...")
        cmd = ["claude", "--print", "--dangerously-skip-permissions",
               "--append-system-prompt", sys_prompt, "-p", task]
        try:
            res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                                 encoding="utf-8", timeout=1800)
            ok = res.returncode == 0 and os.path.isfile(os.path.join(out_dir, "full.md"))
            if ok:
                r["status"] = "done"
                log(f"cleanup: {doc_id} done")
            else:
                log(f"cleanup FAILED: {doc_id} rc={res.returncode} "
                    f"stderr={(res.stderr or '')[-200:]}")
        except subprocess.TimeoutExpired:
            log(f"cleanup TIMEOUT: {doc_id}")
        except FileNotFoundError:
            log("cleanup: claude CLI not found — install/login first")
            return
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
    if not args.skip_claude:
        stage_cleanup(m, limit=args.limit)

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
