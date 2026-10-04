#!/usr/bin/env python3
"""Author-owned, deterministic prompt-only smoke tasks; no public benchmark data.

Regenerate with: python benchmarks/generate_smoke.py
These small tasks test response plumbing, not production coding performance.
"""
from __future__ import annotations

import json
import posixpath
import re
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path


SUFFIX = " Reply with only the JSON value, without Markdown or explanation."


def build_cases() -> list[dict]:
    cases = []

    def add(name: str, category: str, prompt: str, answer) -> None:
        cases.append({
            "id": name, "input": prompt + SUFFIX,
            "expected_output": json.dumps(answer, separators=(",", ":"), sort_keys=True),
            "tags": ["smoke", "prompt-only", category],
        })

    values = [8, -3, 5, 0, 2]
    add("trace-enumerate", "code-tracing",
        f"In Python 3, what does sum(i * x for i, x in enumerate({values}, start=1) if x > 0) evaluate to?",
        sum(i * x for i, x in enumerate(values, start=1) if x > 0))
    rows = [[1], [2]]
    alias = rows[:]
    alias[0].append(7)
    alias.append([3])
    add("trace-alias", "code-tracing",
        "Run this Python 3 code mentally: rows = [[1], [2]]; alias = rows[:]; "
        "alias[0].append(7); alias.append([3]). What is rows?", rows)
    numbers = list(range(10))
    add("trace-slice", "code-tracing",
        "In Python 3, what is list(range(10))[8:1:-3]?", numbers[8:1:-3])
    mapping = {"b": 1, "a": 2}
    mapping["b"] = 3
    mapping["c"] = 4
    add("trace-dict-order", "code-tracing",
        'In Python 3, d = {"b": 1, "a": 2}; d["b"] = 3; d["c"] = 4. What is list(d.keys())?',
        list(mapping.keys()))

    records = [{"id": "d", "score": 2}, {"id": "b", "score": 7}, {"id": "a", "score": 7}]
    add("data-sort", "data-transform",
        f"Sort these records by score descending, then id ascending. Return only the ordered array of ids: {json.dumps(records)}.",
        [row["id"] for row in sorted(records, key=lambda row: (-row["score"], row["id"]))])
    values = ["red", "blue", "red", "green", "blue", "red", "amber"]
    add("data-stable-dedup", "data-transform",
        f"Remove duplicates from {json.dumps(values)}, preserving first occurrence order.", list(dict.fromkeys(values)))
    words = "pear plum pear apple plum pear".split()
    add("data-count", "data-transform",
        f"Return an object mapping each word to its count in this array: {json.dumps(words)}.", dict(Counter(words)))
    records = [{"name": "Ira", "enabled": True, "level": 4}, {"name": "Ada", "enabled": False, "level": 8}, {"name": "Bo", "enabled": True, "level": 2}]
    add("data-filter-project", "data-transform",
        f"Return the names of records with enabled=true and level>=3, in input order: {json.dumps(records)}.",
        [row["name"] for row in records if row["enabled"] and row["level"] >= 3])

    graph = {"A": ["B", "D"], "B": ["C"], "C": ["F"], "D": ["E"], "E": ["F"], "F": []}
    queue = deque([("A", ["A"])])
    seen = {"A"}
    shortest = None
    while queue:
        node, path = queue.popleft()
        if node == "F":
            shortest = path
            break
        for neighbor in sorted(graph[node]):
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append((neighbor, [*path, neighbor]))
    add("algorithm-shortest-path", "algorithms",
        f"For this directed unweighted adjacency list, return the shortest path from A to F as an array. "
        f"If tied, choose the lexicographically smallest complete path: {json.dumps(graph)}.", shortest)
    coins, amount = [1, 4, 6], 8
    minimum = [0] + [amount + 1] * amount
    for subtotal in range(1, amount + 1):
        minimum[subtotal] = min((minimum[subtotal - coin] + 1 for coin in coins if coin <= subtotal), default=amount + 1)
    add("algorithm-min-coins", "algorithms",
        f"With unlimited coins of denominations {coins}, what is the minimum number of coins needed to total {amount}?",
        minimum[amount])
    intervals = [[8, 9], [1, 4], [3, 6], [9, 11], [15, 16]]
    merged = []
    for left, right in sorted(intervals):
        if merged and left <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    add("algorithm-merge-intervals", "algorithms",
        f"Merge these closed intervals, including intervals that touch at an endpoint, and return them in ascending start order: {intervals}.", merged)
    text = "aabcccccaaa"
    encoded = []
    for char in text:
        if encoded and encoded[-1][0] == char:
            encoded[-1][1] += 1
        else:
            encoded.append([char, 1])
    add("algorithm-run-length", "algorithms",
        f'Return the run-length encoding of "{text}" as an array of [character, consecutive_count] pairs.', encoded)

    candidates = ["AB12", "A123", "xy09", "XY00", "XYZ1", "QZ98"]
    add("constraint-regex", "constraints",
        f"Return only the entries fully matching regex [A-Z]{{2}}[0-9]{{2}}, preserving order: {json.dumps(candidates)}.",
        [value for value in candidates if re.fullmatch(r"[A-Z]{2}[0-9]{2}", value)])
    path = "/alpha/beta/../gamma/./delta/../../omega"
    add("constraint-path", "constraints",
        f'Normalize this absolute POSIX path lexically by resolving . and .., with no symbolic links: "{path}". Return a JSON string.',
        posixpath.normpath(path))
    instant = datetime(2026, 12, 31, 23, 45, tzinfo=timezone.utc)
    add("constraint-utc", "constraints",
        'Add 95 minutes to 2026-12-31T23:45:00Z. Return a JSON string in the same YYYY-MM-DDTHH:MM:SSZ UTC format.',
        (instant + timedelta(minutes=95)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    add("constraint-integer-filter", "constraints",
        "Return all integers from 1 through 30 inclusive that are divisible by 3 but not by 2, in ascending order.",
        [value for value in range(1, 31) if value % 3 == 0 and value % 2 != 0])
    return cases


if __name__ == "__main__":
    destination = Path(__file__).with_name("agent_smoke.jsonl")
    cases = build_cases()
    destination.write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases), encoding="utf-8")
    print(f"Wrote {len(cases)} authored smoke cases to {destination}")
