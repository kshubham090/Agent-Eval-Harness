# Contributing

Start with a reproducible evaluation problem: an adapter that loses information,
a grader that accepts an incorrect answer, or a task that exposes a useful failure.
Small, independently testable pull requests are easiest to review.

## Local development

Use Python 3.12 or newer:

```sh
python -m venv .venv
source .venv/bin/activate # Windows: .venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
python -m pytest -q
```

The default suite makes no model calls. Docker tests require an explicit opt-in;
see [coding evaluation](docs/coding.md). CI tests supported platforms, builds a
wheel, installs it into a clean environment and exercises the bundled packs.

## What belongs in a contribution

- Explain the behavior and why it matters, with a small reproduction.
- Add a regression test for behavior that could misreport results or break users.
- Preserve compatibility with existing adapters and saved results where possible.
- Document new flags, defaults, artifact fields and any unsupported cases.
- For benchmark changes, commit raw samples and provenance. Label fixtures as
  fixtures; do not present self-authored tasks as an independent leaderboard.

New packs follow the [pack format and contribution guide](docs/packs.md). Include
clear specifications, independent graders, license/source attribution and hard
negative examples. Changes to task or grader content require a new pack version.
Existing published versions should remain unchanged.

The lightweight core should not require a provider SDK or paid service. Put
provider-specific dependencies behind extras. Keep prompts and credentials out
of logs and metadata unless the user deliberately returns them as evidence.

## Review and license

Open a PR against `master`. Include test commands and actual outcomes; flag tests
you could not run. Contributors retain their copyright and license contributions
under Apache-2.0, as described in [LICENSE](LICENSE). Do not add material without
the right to redistribute it under its stated license. There is no CLA.

Be respectful and specific in reviews. Discuss behavior and evidence, and help
new contributors find a small starting point.
