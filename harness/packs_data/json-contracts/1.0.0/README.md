# JSON output contracts — 1.0.0

Sixteen original prompt-only smoke tasks covering code tracing, data transforms,
small algorithms, and exact output constraints. The structural JSON scorer
ignores whitespace and object key ordering while checking values and types.
Extra commentary, Markdown fences, duplicate object keys, and nonfinite values
are not valid contract responses.

This version preserves the exact `benchmarks/agent_smoke.jsonl` bytes from
Agent Eval Harness revision `14acbcda093646ecd5df1ab405b86293e453b130`.
The corresponding `benchmarks/generate_smoke.py` derives references using Python
standard-library computations. Both were authored in this repository, without
importing another benchmark dataset. See provenance.toml and LICENSE.

These are public teaching and integration tasks, not a held-out evaluation or a
proxy for autonomous repository editing. Passing does not establish production
readiness. Report this pack's version and fingerprint alongside the agent model,
prompt, tool configuration, and run settings. Preserve released pack versions;
bump the version when updating any content.
