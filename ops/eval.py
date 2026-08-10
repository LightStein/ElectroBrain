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
]
# Judged by the opposite rule to everything above: answering it is the failure.
CONTROL = "какой курс доллара к евро сегодня?"

CITE_RE = re.compile(r"📄\s*\S|п\.\s*\d|пункт\s*\d", re.I)
NOTFOUND_RE = re.compile(r"не нашёл|не найдено|прямого ответа нет|NOT_FOUND", re.I)
ERROR_RE = re.compile(r"usage:|Traceback|error:|Внутренняя ошибка|недоступна", re.I)
# A refusal, in either engine's wording. The control passes only by matching
# this and naming no rate.
REFUSAL_RE = re.compile(r"вне моей области|не знаю|не могу|не располагаю|"
                        r"не работаю|не о курсах|не нашёл|не найдено", re.I)
RATE_RE = re.compile(r"\d[\d\s.,]*\s*(?:руб|USD|EUR|евро|доллар|₽|\$)", re.I)
# ask.py logs these to stderr when it hands a question to claude.
ESCALATED_RE = re.compile(r"local answer rejected|Подключаю сильную модель", re.I)
# Measured over the earlier /pro run: ~200k tokens per escalated question.
COST_PER_ESCALATION = 0.076


def run(question, pro=False):
    py = sys.executable
    prompt = ("PRO: " + question) if pro else question
    t0 = time.time()
    r = subprocess.run([py, ASK, "-p", prompt, "--fresh"],
                       capture_output=True, text=True, encoding="utf-8",
                       cwd=os.path.join(ROOT, "bot"), timeout=600)
    # stderr carries ask.py's own log, which is the only place that says
    # whether the local model answered or claude was called. With
    # auto-escalation on, that is the difference between a free answer and a
    # paid one, so it belongs in the report.
    escalated = bool(ESCALATED_RE.search(r.stderr or ""))
    return (r.stdout or "").strip(), time.time() - t0, escalated


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pro", action="store_true", help="use the Claude escalation")
    ap.add_argument("-q", "--question", help="ask one question instead of the set")
    args = ap.parse_args()

    qs = [args.question] if args.question else (QUESTIONS + [CONTROL])
    engine = "claude (/pro)" if args.pro else os.environ.get("ASK_MODEL", "local")
    print(f"engine: {engine}\n" + "=" * 72)

    times, cited, notfound, errors, escalations = [], 0, 0, 0, 0
    control_ok = None
    for q in qs:
        answer, dt, escalated = run(q, pro=args.pro)
        times.append(dt)
        escalations += escalated
        is_err = bool(ERROR_RE.search(answer))
        errors += is_err

        if q == CONTROL:
            # Inverted: a citation here means a source was invented for a
            # question the corpus cannot possibly answer.
            control_ok = bool(REFUSAL_RE.search(answer)) and not RATE_RE.search(answer)
            flag = "control OK (refused)" if control_ok else "CONTROL FAILED"
        else:
            has_cite = bool(CITE_RE.search(answer))
            is_nf = bool(NOTFOUND_RE.search(answer))
            cited += has_cite
            notfound += is_nf
            flag = "ERROR" if is_err else ("not-found" if is_nf else
                                           ("cited" if has_cite else "NO CITATION"))
        src = ""
        m = re.search(r"📄\s*([^,:\n]{1,70})", answer)
        if m:
            src = " <- " + m.group(1).strip()
        via = " [escalated]" if escalated else ""
        print(f"\n[{dt:5.1f}s] [{flag}]{via}{src}\nQ: {q}\nA: {answer[:400]}")

    n = len(qs) - (1 if CONTROL in qs else 0)
    print("\n" + "=" * 72)
    print(f"real questions {n} | cited {cited} | not-found {notfound} | errors {errors}")
    if control_ok is not None:
        print(f"control: {'refused correctly' if control_ok else 'FAILED - invented an answer'}")
    print(f"escalated to claude: {escalations}/{len(qs)}"
          f"  (~${escalations * COST_PER_ESCALATION:.2f} this run,"
          f" ~${COST_PER_ESCALATION:.3f} each)")
    if times:
        print(f"time: min {min(times):.1f}s  median "
              f"{sorted(times)[len(times)//2]:.1f}s  max {max(times):.1f}s")
    if errors:
        print("\nERRORS PRESENT - the engine failed, not just the retrieval.")
        sys.exit(1)
    if control_ok is False:
        print("\nCONTROL FAILED - the assistant invented an answer. Fix before shipping.")
        sys.exit(1)


if __name__ == "__main__":
    main()
