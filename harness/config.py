"""Small, strict TOML configuration; paths are relative to the config file."""
from __future__ import annotations

import json
import tomllib
from pathlib import Path


_SECTIONS = {
    "agent": {"type", "path", "command", "url", "model", "cwd", "timeout", "extra_args",
              "output_format", "output_key", "token_env"},
    "eval": {"dataset", "scorers", "concurrency", "runs", "filter_tags", "pass_threshold",
             "min_pass_rate", "allow_errors"},
    "output": {"json", "html"},
    "baseline": {"name", "directory", "threshold", "allow_dataset_change"},
}

_STRING_FIELDS = {
    "agent": {"type", "path", "url", "model", "cwd", "output_format", "output_key", "token_env"},
    "eval": {"dataset"},
    "output": {"json", "html"},
    "baseline": {"name", "directory"},
}
_INTEGER_FIELDS = {"eval": {"concurrency", "runs"}}
_NUMBER_FIELDS = {
    "agent": {"timeout"},
    "eval": {"pass_threshold", "min_pass_rate"},
    "baseline": {"threshold"},
}
_BOOLEAN_FIELDS = {"eval": {"allow_errors"}, "baseline": {"allow_dataset_change"}}


def _validate_scalar_types(section: str, values: dict) -> None:
    """Do not let Click coerce TOML booleans, floats, or arrays into another type."""
    for key, value in values.items():
        name = f"{section}.{key}"
        if key in _STRING_FIELDS.get(section, ()) and not isinstance(value, str):
            suffix = "path string" if key in {"path", "cwd", "dataset", "json", "html", "directory"} else "string"
            raise ValueError(f"{name} must be a {suffix}")
        if key in _INTEGER_FIELDS.get(section, ()) and type(value) is not int:
            raise ValueError(f"{name} must be an integer")
        if key in _NUMBER_FIELDS.get(section, ()) and type(value) not in (int, float):
            raise ValueError(f"{name} must be a number")
        if key in _BOOLEAN_FIELDS.get(section, ()) and type(value) is not bool:
            raise ValueError(f"{name} must be a boolean")


def load_config(path: str | Path) -> dict:
    path = Path(path).resolve()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    unknown = set(data) - set(_SECTIONS)
    if unknown:
        raise ValueError(f"unknown config sections: {sorted(unknown)}")
    for section, values in data.items():
        if not isinstance(values, dict):
            raise ValueError(f"config [{section}] must be a table")
        unknown = set(values) - _SECTIONS[section]
        if unknown:
            raise ValueError(f"unknown config [{section}] keys: {sorted(unknown)}")
        _validate_scalar_types(section, values)
    options = {}
    agent = data.get("agent", {})
    kind = agent.get("type", "python")
    if kind not in {"python", "command", "http", "codex", "claude-code"}:
        raise ValueError(f"unknown agent type {kind!r}")
    targets = {"path": "python", "command": "command", "url": "http"}
    for key, required_kind in targets.items():
        if key in agent and kind != required_kind:
            raise ValueError(f"agent.{key} requires agent.type = {required_kind!r}")
    if kind in {"codex", "claude-code"}:
        options["agent_path"] = kind
    if "path" in agent:
        options["agent_path"] = agent["path"]
    for key in ("command", "extra_args"):
        if key in agent and (not isinstance(agent[key], list) or
                             not all(isinstance(x, str) for x in agent[key]) or
                             (key == "command" and not agent[key])):
            raise ValueError(f"agent.{key} must be an array of strings")
    if "command" in agent:
        options["command"] = json.dumps(agent["command"])
    for key in ("url", "model", "cwd", "timeout", "output_format", "output_key", "token_env"):
        if key in agent:
            options[key] = agent[key]
    if "extra_args" in agent:
        options["agent_args"] = tuple(agent["extra_args"])
    evaluation = dict(data.get("eval", {}))
    for key in ("scorers", "filter_tags"):
        if key in evaluation:
            if not isinstance(evaluation[key], list) or not all(isinstance(x, str) for x in evaluation[key]):
                raise ValueError(f"eval.{key} must be an array of strings")
            evaluation[key] = ",".join(evaluation[key])
    if "scorers" in evaluation:
        evaluation["scorer_names"] = evaluation.pop("scorers")
    options.update(evaluation)
    for key, value in data.get("output", {}).items():
        options[{"json": "output", "html": "html_path"}[key]] = value
    for key, value in data.get("baseline", {}).items():
        options[{"name": "baseline_name", "directory": "baselines_dir"}.get(key, key)] = value
    for key in ("dataset", "output", "html_path", "cwd", "baselines_dir"):
        if key in options:
            if not isinstance(options[key], str):
                raise ValueError(f"{key} must be a path string")
            options[key] = str(path.parent / options[key])
    if kind == "python" and "agent_path" in options:
        options["agent_path"] = str(path.parent / options["agent_path"])
    # Relative command arguments run beside the configuration by default.
    options.setdefault("cwd", str(path.parent))
    return options
