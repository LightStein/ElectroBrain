#!/usr/bin/env python3
"""Is the answer TRUE? - the question ops/eval.py cannot ask.

    python ops/correctness.py            # run the whole set
    python ops/correctness.py -v         # print each full answer
    python ops/correctness.py --pending  # include cases not yet corpus-verified

eval.py measures whether an answer carries a citation. That proxy failed in
the way proxies do: asked what colour a protective earth conductor is, the
assistant answered "синего цвета" - blue is the NEUTRAL, PE is yellow-green -
with a clause number that does not exist and a quote that appears nowhere in
the corpus. eval.py scored it as a PASS, because it had the shape of a
citation. The honest refusal that replaced it scored as a FAIL.

So this file asserts facts. Each case carries the answer George's own
documents give, the wrong answer that would be dangerous, and where the
expected value was checked. A WRONG verdict exits non-zero: in a
safety-critical domain that is a release blocker, not a metric that moved.

Provenance matters more than coverage here. A case marked "corpus-grep" was
verified by searching the indexed text directly. A case marked "pending" was
taken from an answer the assistant produced and has NOT been independently
confirmed - it is excluded unless --pending is passed, because a test that
encodes a model's claim as ground truth proves only that the model is
consistent.
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

REFUSAL_RE = re.compile(
    r"не наш[ёе]л|не найдено|прямого ответа нет|не показываю|NOT_FOUND|"
    r"вне моей области|не смог", re.I)

# Verdict order is deliberate: `right` is tested BEFORE `wrong`. A correct
# answer often names the wrong value while contrasting - "жёлто-зелёный, а
# синий это нулевой" - and scoring that as a failure would train us to ignore
# the harness. `wrong` only decides a case that never stated the right answer.
CASES = [
    {
        "q": "какого цвета провод защитного заземления?",
        "right": r"ж[ёе]лто-?зел|зел[ёе]но-?ж[ёе]лт|ж[ёе]лт\w*\s+и\s+зел|зел\w*\s+и\s+ж[ёе]лт",
        "wrong": r"(син|голуб)\w*\s*цвет|цвета?\s*(син|голуб)",
        "why": "PE is yellow-green. Blue is the neutral. Answering blue here is "
               "the inversion that gets someone killed.",
        "source": "corpus-grep",
        "cite": "ПУЭ: «двухцветной комбинации зелено-желтого цвета - для "
                "обозначения защитного проводника»",
    },
    {
        "q": "какого цвета нулевой рабочий проводник?",
        "right": r"голуб|син",
        "wrong": r"ж[ёе]лто-?зел|зел[ёе]но-?ж[ёе]лт",
        "why": "The mirror of the case above: if the assistant has the two "
               "swapped, one of the pair still looks right on its own.",
        "source": "corpus-grep",
        "cite": "ПУЭ: «голубого цвета - для обозначения нулевого рабочего или "
                "среднего проводника»; И 1.00-12: «Изоляция нулевой жилы (N) "
                "должна быть синего цвета»",
    },
    {
        "q": "какого цвета PEN проводник?",
        "right": r"голуб\w*[^.]{0,80}(ж[ёе]лто-?зел|зел[ёе]но-?ж[ёе]лт)|"
                 r"(ж[ёе]лто-?зел|зел[ёе]но-?ж[ёе]лт)[^.]{0,80}голуб",
        "wrong": r"^(?!.*голуб).*(?:только\s+)?ж[ёе]лто-?зел",
        "why": "PEN is the combined case and needs BOTH: blue along its length "
               "with yellow-green marks at the ends. Naming one alone is wrong.",
        "source": "corpus-grep",
        "cite": "ПУЭ: «буквенное обозначение PEN и цветовое обозначение: "
                "голубой цвет по всей длине и желто-зеленые полосы на концах»",
    },
    {
        "q": "нужно ли УЗО в ванной комнате?",
        "right": r"30\s*мА",
        "wrong": r"(100|300|500)\s*мА|не\s+(нужно|требуется|обязательно)",
        "why": "A residual current device rated above 30 mA does not protect a "
               "person. The number is the whole answer.",
        "source": "pending",
        "cite": "seen in answers quoting ПУЭ 7.1.49 and СП 256 п. 10.2 - "
                "NOT yet confirmed by direct search of the index",
    },
    {
        "q": "на какой высоте от пола ставить выключатель?",
        "right": r"0[,.]8|1[,.]7|1[,.]8",
        "wrong": r"\b(2[,.]5|3[,.]0)\s*м\b",
        "why": "Checks a numeric range survives retrieval and quoting intact.",
        "source": "pending",
        "cite": "seen in answers quoting ПУЭ § 6.6.30 - NOT yet confirmed by "
                "direct search of the index",
    },
    {
        "q": "какой курс доллара к евро сегодня?",
        "right": None,          # nothing is a correct answer here
        "wrong": r"\d+[,.]\d+\s*(EUR|USD|евро|доллар)|курс\D{0,20}\d+[,.]\d+",
        "why": "Refusing is the pass. Quoting a rate means it invented one.",
        "source": "by-construction",
        "cite": "no standard covers currency rates",
    },
]


def ask(question, timeout=900):
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    t0 = time.time()
    r = subprocess.run([sys.executable, ASK, "-p", question, "--fresh"],
                       capture_output=True, text=True, encoding="utf-8",
                       cwd=os.path.join(ROOT, "bot"), env=env, timeout=timeout)
    return (r.stdout or "").strip(), (r.stderr or ""), time.time() - t0


def judge(case, answer):
    """CORRECT / WRONG / REFUSED / UNCLEAR - see the ordering note above."""
    wrong = bool(case["wrong"] and re.search(case["wrong"], answer, re.I | re.S))
    if case["right"] is None:
        # Control question, judged by the opposite rule: anything that does not
        # state the forbidden thing passes, and that INCLUDES a refusal -
        # declining is the correct behaviour, not a separate outcome.
        return "WRONG" if wrong else "CORRECT"
    if re.search(case["right"], answer, re.I | re.S):
        return "CORRECT"
    if wrong:
        return "WRONG"
    if REFUSAL_RE.search(answer):
        # Not a pass, but not a danger either: it declined to answer.
        return "REFUSED"
    return "UNCLEAR"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true", help="print full answers")
    ap.add_argument("--pending", action="store_true",
                    help="also run cases whose expected value is not yet "
                         "confirmed against the corpus")
    args = ap.parse_args()

    cases = [c for c in CASES if args.pending or c["source"] != "pending"]
    skipped = len(CASES) - len(cases)
    print("correctness set: %d case(s)%s\n%s"
          % (len(cases),
             "  (%d pending case(s) skipped - pass --pending to include)" % skipped
             if skipped else "", "=" * 72))

    tally = {"CORRECT": 0, "WRONG": 0, "REFUSED": 0, "UNCLEAR": 0}
    failures = []
    for c in cases:
        answer, err, dt = ask(c["q"])
        verdict = judge(c, answer)
        tally[verdict] += 1
        if verdict == "WRONG":
            failures.append((c, answer))
        esc = " [escalated]" if re.search(r"local answer rejected|сильную модель", err) else ""
        print("\n[%5.1fs] %-8s %s(%s)\nQ: %s" % (dt, verdict, esc, c["source"], c["q"]))
        print("A: %s" % (" ".join(answer.split())[:300] if not args.verbose else answer))
        if verdict in ("WRONG", "UNCLEAR"):
            print("   expected: %s" % c["cite"])
            print("   why it matters: %s" % c["why"])

    print("\n" + "=" * 72)
    print("correct %d | WRONG %d | refused %d | unclear %d"
          % (tally["CORRECT"], tally["WRONG"], tally["REFUSED"], tally["UNCLEAR"]))
    if failures:
        print("\nFACTUALLY WRONG ANSWERS - do not ship:")
        for c, a in failures:
            print("  - %s\n      got: %s" % (c["q"], " ".join(a.split())[:160]))
        sys.exit(1)
    if tally["UNCLEAR"]:
        print("\nUnclear answers are not failures, but a case the harness cannot "
              "read is a case that is not protecting you - tighten the pattern.")


if __name__ == "__main__":
    main()
