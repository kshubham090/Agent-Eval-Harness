"""Independent held-out checks; candidate functions execute in a child process."""
from __future__ import annotations

import json
from pathlib import Path
import random
import subprocess
import sys

# Only arguments and the function name cross into the candidate process. The
# expected answers and comparison code remain in this trusted parent process.
_PROBE = r"""
import contextlib
import copy
import io
import json
from pathlib import Path
import runpy
import sys

payload = json.loads(sys.stdin.read())
workspace = Path(sys.argv[1])
sys.path.insert(0, str(workspace))
with contextlib.redirect_stdout(io.StringIO()):
    namespace = runpy.run_path(str(workspace / 'solution.py'))
    function = namespace[payload['function']]
    records = []
    for args in payload['calls']:
        before = copy.deepcopy(args)
        try:
            value = function(*args)
            original = args[0]
            records.append({
                'value': value,
                'unchanged': args == before,
                'is_list': isinstance(value, list),
                'fresh': value is not original,
                'fresh_rows': isinstance(value, list) and all(
                    isinstance(row, list) and all(row is not old for old in original)
                    for row in value
                ) if isinstance(original, list) else True,
            })
        except BaseException as exc:
            records.append({'error': type(exc).__name__})
print(json.dumps(records, allow_nan=False))
"""


def strict_equal(actual, expected):
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(strict_equal(a, b) for a, b in zip(actual, expected))
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(strict_equal(actual[k], expected[k]) for k in expected)
    return actual == expected


def check(function, examples, *, fresh=False, fresh_rows=False):
    # The optional path supports host-side grader verification. Normal sandbox
    # grading uses the default fixed workspace and passes no additional argv.
    workspace = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('/workspace')
    calls = [example[0] for example in examples]
    process = subprocess.run(
        [sys.executable, '-I', '-B', '-c', _PROBE, str(workspace)],
        input=json.dumps({'function': function, 'calls': calls}),
        text=True, capture_output=True, timeout=10, check=False,
    )
    if process.returncode != 0:
        raise AssertionError(f'candidate process failed: {process.stderr[-2000:]}')
    try:
        records = json.loads(process.stdout)
    except ValueError as exc:
        raise AssertionError('candidate process did not return valid probe results') from exc
    if not isinstance(records, list) or len(records) != len(examples):
        raise AssertionError('candidate process returned incomplete probe results')
    for index, (record, (_, expected, error)) in enumerate(zip(records, examples)):
        if error:
            assert record == {'error': error}, f'case {index}: expected {error}'
            continue
        assert isinstance(record, dict) and 'value' in record, f'case {index}: function raised or returned no value'
        assert strict_equal(record['value'], expected), f'case {index}: wrong value'
        assert record.get('unchanged') is True, f'case {index}: input mutated'
        if fresh:
            assert record.get('is_list') is True and record.get('fresh') is True, f'case {index}: expected a new list'
        if fresh_rows:
            assert record.get('fresh_rows') is True, f'case {index}: returned interval aliases input or is not a list'
    print(json.dumps({'passed': len(examples), 'total': len(examples)}))


if __name__ == '__main__':
    cases = [
        [], ['x'], ['x', 'x'], ['z', 'a', 'z', 'b', 'a'],
        ['red', 'blue', 'red', 'green', 'blue'],
        ['', ' ', '', '\t', ' '], ['A', 'a', 'A', 'Á', 'a'],
        ['café', '東京', 'café', 'cafe\u0301', '東京'],
    ]
    rng = random.Random(731)
    vocabulary = ['', 'red', 'blue', 'Green', 'green', 'é', 'e\u0301', '東京', ' ']
    cases += [[rng.choice(vocabulary) for _ in range(rng.randrange(0, 80))] for _ in range(24)]
    examples = []
    for values in cases:
        expected = []
        for value in values:
            if value not in expected:
                expected.append(value)
        examples.append(([values], expected, None))
    check('stable_unique', examples, fresh=True)
