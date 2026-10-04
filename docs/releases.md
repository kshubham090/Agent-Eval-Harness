# Installation and releases

The project is Apache-2.0 licensed. Python 3.12+ is required; the core installs
Click and python-dotenv. Docker is only needed for coding-task evaluation.

Until a registry release has been published, install from a reviewed Git tag or
commit (replace `COMMIT_SHA` with the full SHA you intend to trust):

```sh
python -m pip install 'git+https://github.com/kshubham090/Agent-Eval-Harness.git@COMMIT_SHA'
agent-eval --version
agent-eval pack list
```

For local development use `python -m pip install -e '.[dev]'`. Optional extras
are `embedding` and `judge`. The repository and wheel include versioned starter
packs; users do not need a repository checkout to load them.

## Maintainer release procedure

1. Update `project.version` and `CHANGELOG.md`. Keep existing released pack
   versions immutable; create a new pack version when task content changes.
2. Run the full CI suite and review the built distribution smoke test. Build
   locally with `python -m build` and check with `python -m twine check dist/*`.
3. Create a GitHub release with tag `v<project.version>`. The release workflow
   verifies the exact version, builds the wheel and source distribution, and
   installs the wheel into a fresh environment to test the bundled workflows.
4. Before publishing the first release, reserve/configure the PyPI project and
   add GitHub Trusted Publishing for this repository, `release.yml`, and the
   `pypi` environment. Protect that environment with a maintainer approval rule.
   The publishing job uses short-lived OIDC credentials, not a repository token.

The workflow is provided; adding it does not create a PyPI account or publish a
release. Registry installation commands should be advertised only after a
successful publish. If a release fails publication, inspect its build artifacts
and resolve the publisher configuration before retrying. Never move an existing
release tag or overwrite a published pack version.

Consumers of the composite GitHub Action should pin a full reviewed commit SHA.
See [the CI guide](ci.md) for inputs and artifact handling.
