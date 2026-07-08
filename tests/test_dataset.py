import pytest

from harness.dataset import DatasetError, EvalCase, load_dataset


def write_jsonl(tmp_path, lines):
    path = tmp_path / "data.jsonl"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_loads_valid_dataset(tmp_path):
    path = write_jsonl(
        tmp_path,
        [
            '{"id": "q1", "input": "What is 1+1?", "expected_output": "2", "tags": ["math"]}',
            '{"id": "t1", "input": "book it", "expected_output": "done", "expected_trajectory": ["search", "book"]}',
        ],
    )
    cases = load_dataset(path)
    assert cases == [
        EvalCase(id="q1", input="What is 1+1?", expected_output="2", tags=("math",)),
        EvalCase(id="t1", input="book it", expected_output="done", expected_trajectory=("search", "book")),
    ]


def test_skips_blank_lines(tmp_path):
    path = write_jsonl(tmp_path, ['{"id": "a", "input": "x", "expected_output": "y"}', "", ""])
    assert len(load_dataset(path)) == 1


def test_loads_example_datasets():
    qa_cases = load_dataset("datasets/examples/simple_qa.jsonl")
    assert len(qa_cases) == 60
    assert all(c.tags for c in qa_cases)  # every case is taggable for --filter-tags
    trajectory_cases = load_dataset("datasets/examples/tool_calling.jsonl")
    assert len(trajectory_cases) == 18
    assert all(c.expected_trajectory for c in trajectory_cases)


@pytest.mark.parametrize(
    "bad_line, match",
    [
        ("not json", "invalid JSON"),
        ('{"input": "x", "expected_output": "y"}', "'id'"),
        ('{"id": "a", "expected_output": "y"}', "'input'"),
        ('{"id": "a", "input": "x"}', "'expected_output'"),
        ('{"id": "", "input": "x", "expected_output": "y"}', "'id'"),
        ('{"id": "a", "input": "x", "expected_output": "y", "expected_trajectory": "search"}', "list of strings"),
        ('{"id": "a", "input": "x", "expected_output": "y", "tags": "math"}', "list of strings"),
        ('{"id": "a", "input": "x", "expected_output": "y", "extra": 1}', "unknown fields"),
        ('["not", "an", "object"]', "expected a JSON object"),
    ],
)
def test_rejects_malformed_records(tmp_path, bad_line, match):
    path = write_jsonl(tmp_path, [bad_line])
    with pytest.raises(DatasetError, match=match):
        load_dataset(path)


def test_rejects_duplicate_ids(tmp_path):
    line = '{"id": "a", "input": "x", "expected_output": "y"}'
    path = write_jsonl(tmp_path, [line, line])
    with pytest.raises(DatasetError, match="duplicate case id"):
        load_dataset(path)


def test_rejects_empty_dataset(tmp_path):
    path = write_jsonl(tmp_path, [""])
    with pytest.raises(DatasetError, match="empty"):
        load_dataset(path)
