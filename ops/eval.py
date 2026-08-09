#!/usr/bin/env python3
"""Answer-quality harness for the standards assistant.

    python ops/eval.py                 # local engine
    python ops/eval.py --pro           # Claude escalation, same questions
    python ops/eval.py -q "вопрос"     # single ad-hoc question

Runs a fixed set of questions a revisor would actually ask and reports, per
question: wall-clock time, whether a source citation was produced, and which
document was cited. It does NOT judge whether the answer is factually right -
that needs George. What it does catch is the failure modes that are checkable
mechanically and that matter most here:

  * an answer with no citation (the answer contract is broken)
  * "not found" on a question the corpus should cover (retrieval miss)
  * an engine error dressed up as an answer
  * latency drifting past what is usable in a chat

Written for regression use: run it after any change to retrieval, prompts or
the model, and compare.
"""

import argparse
import os
import re
import subprocess
import sys
import time

ROOT = os.environ.get("STANDARDS_ROOT",
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ASK = os.path.join(ROOT, "bot", "ask.py")

# Deliberately mixed: things the corpus clearly covers, phrasings an
# electrician would actually use (not the standards' own vocabulary), and one
# question that should legitimately find nothing.
QUESTIONS = [
    "какого цвета провод защитного заземления?",
    "на какой высоте от пола ставить выключатель?",
    "какое минимальное сечение медного кабеля для розеточной группы?",
    "как обозначается нулевой рабочий проводник?",
    "нужно ли УЗО в ванной комнате?",
    "какая степень защиты IP нужна для светильника в душевой?",
    "на каком расстоянии от газовой трубы можно вести проводку?",
    "чем отличается система TN-C от TN-S?",
    "какой курс доллара к евро сегодня?",   # must NOT invent an answer
]

CITE_RE = re.compile(r"📄|п\.\s*\d|пункт\s*\d", re.I)
NOTFOUND_RE = re.compile(r"не нашёл|не найдено|прямого ответа нет|NOT_FOUND", re.I)
ERROR_RE = re.compile(r"usage:|Traceback|error:|Внутренняя ошибка|недоступна", re.I)


def run(question, pro=False):
    py = sys.executable
    prompt = ("PRO: " + question) if pro else question
    t0 = time.time()
    r = subprocess.run([py, ASK, "-p", prompt, "--fresh"],
                       capture_output=True, text=True, encoding="utf-8",
                       cwd=os.path.join(ROOT, "bot"), timeout=600)
    return (r.stdout or "").strip(), time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pro", action="store_true", help="use the Claude escalation")
    ap.add_argument("-q", "--question", help="ask one question instead of the set")
    args = ap.parse_args()

    qs = [args.question] if args.question else QUESTIONS
    engine = "claude (/pro)" if args.pro else os.environ.get("ASK_MODEL", "local")
    print(f"engine: {engine}\n" + "=" * 72)

    times, cited, notfound, errors = [], 0, 0, 0
    for q in qs:
        answer, dt = run(q, pro=args.pro)
        times.append(dt)
        has_cite = bool(CITE_RE.search(answer))
        is_nf = bool(NOTFOUND_RE.search(answer))
        is_err = bool(ERROR_RE.search(answer))
        cited += has_cite
        notfound += is_nf
        errors += is_err
        flag = "ERROR" if is_err else ("not-found" if is_nf else
                                       ("cited" if has_cite else "NO CITATION"))
        src = ""
        m = re.search(r"📄\s*([^,:]{0,70})", answer)
        if m:
            src = " <- " + m.group(1).strip()
        print(f"\n[{dt:5.1f}s] [{flag}]{src}\nQ: {q}\nA: {answer[:400]}")

    n = len(qs)
    print("\n" + "=" * 72)
    print(f"questions {n} | cited {cited} | not-found {notfound} | errors {errors}")
    if times:
        print(f"time: min {min(times):.1f}s  median "
              f"{sorted(times)[len(times)//2]:.1f}s  max {max(times):.1f}s")
    if errors:
        print("\nERRORS PRESENT - the engine failed, not just the retrieval.")
        sys.exit(1)


if __name__ == "__main__":
    main()
