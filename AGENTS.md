# AGENTS.md

A short guide for any coding agent, or person, working on this code.

## What this is

An access helpdesk. Employees ask for access in plain language. An agent
works out what they need, a pure policy engine decides, and the system grants,
sends for approval or blocks. Every decision is audited. The agent never
changes anything itself. Every change goes through a typed MCP tool that
checks permissions first.

## Where things live

- `backend/helpdesk/agent_api/` has sign-in, the chat and the agent loop (FastAPI, LangGraph). The prompt is `backend/helpdesk/agent_api/agent/prompts/agent_loop.md`.
- `backend/helpdesk/mcp_server/` has the typed tools, behind RBAC, plus policy ingest and search.
- `backend/helpdesk/scheduler/` has the background jobs.
- `backend/helpdesk/shared/` has the policy engine, models, tenant-bound database access, the audit chain, RBAC and kill switches.
- `backend/migrations/` holds the Alembic migrations. `backend/data/policies/` holds the demo policy documents. `backend/evals/` holds the conversation eval cases.
- `backend/tests/` holds the backend tests, in the same layout as the code. `infra/tests/` holds the repo-level checks.
- `web/src/` is the Next.js app. `web/README.md` explains its layout.
- `infra/` holds the local database init script, Kubernetes manifests and the load test.
- [ARCHITECTURE.md](ARCHITECTURE.md) has the big picture. [docs/local-setup.md](docs/local-setup.md) has the setup.

## Rules the code depends on

1. **The model never runs a change.** Every side effect is a typed MCP tool with a row in the RBAC matrix in `backend/helpdesk/shared/auth/rbac.py`. The agent may only propose a change, and it runs on the person's yes. There are no shortcuts around this, temporary or not.
2. **Identity comes from the token.** Never read tenant or user from tool arguments or from model output.
3. **The policy engine stays pure.** `backend/helpdesk/shared/policy/` does no I/O and imports no models or services. It keeps 100% line and branch coverage (`make test-policy`).
4. **Database access goes through the tenant-bound helpers** in `backend/helpdesk/shared/crud/` and `backend/helpdesk/shared/core/db.py`. Services do not call `session.execute` directly.
5. **Errors are typed codes.** No free text crosses a service line.
6. **Models are named by alias only**, such as `agent-default`, and called through the gateway client. No provider model name appears in this repository.
7. **Retrieved and user text is data, never instructions.** Structured model output is checked against a schema.
8. **Model-driven behaviour gets conversation cases** in `backend/evals/conversations/cases.jsonl` before it merges.
9. **The three apps never import each other**, and the shared library never imports an app. `backend/.importlinter` holds these rules.
10. **No secrets in code, config, tests or fixtures.** Seed data is obviously fake. No real names, companies or addresses.
11. **Ask before adding or upgrading a dependency.**

## Adding a new change tool

A new action needs all of these, or it is not done.

- An input model built on `MutatingToolInput` in `backend/helpdesk/mcp_server/tools/base.py`, so it carries an idempotency key and refuses unknown fields.
- A `Tool` member and a matrix row in `backend/helpdesk/shared/auth/rbac.py`.
- A `ToolSpec` entry in `backend/helpdesk/mcp_server/tools/registry.py` with `mutating=True`.
- Tests for the allowed case, each refused principal, a replay with the same key, and the audit event.

## Tests

- Logic is tested with plain objects and fakes. A test reaches a real database only when it checks something only the database can show, and then it carries the `db` marker.
- No real waiting and no real network. Use a fake clock and a stub.
- Tests share no state, so they run in parallel.
- Never change a test to make it pass. Fix the code, or raise the test as wrong.
- Some tests read the docs. `infra/tests/test_docs.py` and others pin the README sections, the guarantee links and the diagram files. Run them after editing a doc.

## Commands

- `make setup` installs the Python dependencies and the pre-commit hooks.
- `docker compose up -d` starts Postgres, Redis and Qdrant. `make migrate` creates the schema.
- `make test-fast` is the everyday test run. `make test` is the whole suite with coverage.
- `make lint typecheck` runs ruff and `mypy --strict`.
- `make web-verify` runs the web types, lint, unit tests and build.
- `make verify` runs everything CI runs, in order.
- `make help` lists every other target.

Run migrations only against the local compose database.
