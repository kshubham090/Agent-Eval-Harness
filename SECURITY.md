# Security and trust boundaries

Evaluation invokes agent code, commands and optional model providers. Only run
agent modules and configurations you trust on your host. Python, command and
CLI adapters have the permissions of the user running the harness.

The coding workflow uses disposable Docker containers, no host bind mounts or
Docker socket, disabled network by default, bounded resources and separate
grader containers. Docker and the selected image remain part of the trust
boundary. Use a dedicated runner for untrusted code; containers alone are not
a proof of isolation from a hostile adversary.

Pack grader commands are executable code. Inspect third-party packs and images
before running them. Public pack tests are withheld from the agent workspace
at runtime; they are not secret or immune to benchmark contamination. Candidate
code may share the grader's interpreter, so this is functional evaluation,
not an adversarially tamper-proof grading service.

Saved prompts, outputs, patches and tool events can contain sensitive data.
Review artifacts before sharing or enabling uploads in public workflows.
Provider credentials must be supplied explicitly to coding containers. Do not
run credentialed evaluations on untrusted pull requests or enable
`pull_request_target` with a checkout of untrusted code.

## Reporting a vulnerability

Use GitHub's **Security → Report a vulnerability** when private reporting is
available for this repository. Otherwise contact the maintainer through their
[GitHub profile](https://github.com/kshubham090) to arrange private disclosure.
Include affected versions and a minimal reproduction; do not publish working
exploits or credentials in public issues. The project has no guaranteed response
time or bounty program.

The latest released version is the supported security-fix target.
