"""Phase 9 in action: measure how well the LLM judge agrees with human grades.

Run with ANTHROPIC_API_KEY set:
    .venv\\Scripts\\python scripts\\calibrate_judge.py

Compares the real LLM judge against exact match on the same calibration set,
so you can see the agreement gap side by side. ~10 Haiku calls (~1 cent).
"""

from dotenv import load_dotenv

from harness.meta_eval import evaluate_judge, load_judge_dataset
from harness.scorers import ExactMatchScorer, LLMJudgeScorer

load_dotenv()  # pick up ANTHROPIC_API_KEY from the local .env file


def report(label, result):
    print(f"\n{label}")
    print(f"  mean absolute error:   {result.mean_absolute_error:.2f}")
    print(f"  agreement with humans: {result.agreement_rate:.0%}  (tolerance +/-{result.tolerance})")
    for p in result.pairs:
        flag = "DISAGREES" if p.error > result.tolerance else "ok"
        print(f"    {p.case_id:<4} human={p.human:.1f} judge={p.judge:.1f}  {flag}")


def main():
    cases = load_judge_dataset("datasets/examples/judge_calibration.jsonl")
    print(f"calibration set: {len(cases)} human-graded cases")

    report("exact match as judge:", evaluate_judge(cases, ExactMatchScorer()))
    report("LLM judge (Haiku):", evaluate_judge(cases, LLMJudgeScorer()))


if __name__ == "__main__":
    main()
