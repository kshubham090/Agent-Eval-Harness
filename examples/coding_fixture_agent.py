"""Known reference patches for workflow tests. This is not an AI agent."""
from pathlib import Path
import sys


SOLUTIONS = {
    "slugify": '''import re
import unicodedata

def slugify(text):
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    folded = unicodedata.normalize("NFKD", text.casefold()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
''',
    "stable_unique": '''def stable_unique(values):
    return list(dict.fromkeys(values))
''',
    "merge_intervals": '''def merge_intervals(intervals):
    if any(start > end for start, end in intervals):
        raise ValueError("invalid interval")
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result
''',
}


def main():
    sys.stdin.read()
    if Path("/grader").exists():
        raise SystemExit("Grader leaked into the agent container")
    if "--noop" in sys.argv:
        print("Fixture: deliberately left buggy files unchanged.")
        return
    target = Path("solution.py")
    original = target.read_text(encoding="utf-8")
    for name, code in SOLUTIONS.items():
        if f"def {name}(" in original:
            target.write_text(code, encoding="utf-8")
            print(f"Fixture: applied known reference patch for {name}.")
            return
    raise SystemExit("Unrecognized fixture task")


if __name__ == "__main__":
    main()
