# How this was built

This project was built by one engineer working with AI coding agents (Claude
Code). The agents wrote much of the code. What kept the result reliable was a
fixed way of working, with tests and checks deciding when a change was done.

## The way of working

1. **Write down the change first.** What changes and why, which files it touches, and the checks that prove it works. Those checks include what must be refused or must fail safely.
2. **Write the tests before the code.** One test per check. They fail at first, because the code does not exist yet.
3. **Make the smallest change that turns them green.** Touch nothing else.
4. **Run the checks.** `make verify` runs the same checks CI runs, so a green run on the laptop means a green run in CI.
5. **Review the change** with fresh eyes before it is committed, then commit it with a plain message that says what it does.

Two things make this work with an agent. The checks are written before any
code, so "done" means the same thing to the person and to the agent. And the
tests come from those checks, not from the code, so they test what was asked
for rather than what happened to be built.

## What `make verify` runs

- `format`, `lint` and `typecheck` run ruff and `mypy --strict`.
- `test` runs the backend and repo-level tests with an 80% coverage floor.
- `test-policy` runs the policy engine alone at 100% line and branch coverage.
- `web-verify` runs the web app's types, lint, unit tests and production build.
- `security` runs a secret scan over the whole git history, dependency audits for Python and the web app, and a config scan of the compose files.

## Guard rails in the repository

Some rules are checked by tests rather than trusted to memory.

- `backend/.importlinter` keeps the three apps from importing each other.
- `infra/tests/test_docs.py` fails when a README guarantee links a test that no longer exists, or when a diagram goes missing.
- `backend/tests/agent_api/test_loop_graph.py` fails when the agent graph and its docs page disagree.
- `infra/tests/test_security_config.py` keeps the secret scan's exceptions to three known fake files.

## What is not in this repository

The working notes and change plans were kept out of this public copy. What is
here is the result, meaning the code, the tests and a commit for each change.
