# AGENTS.md

A short guide for any coding agent, or person, picking up this project.

## What this is

An access help desk. Employees ask for access in plain language; an agent
decides, grants or escalates, and every decision can be audited. The agent
never changes anything itself: every change goes through a typed tool that
checks permissions first.

## Where things live

- `backend/helpdesk/agent_api/`: the chat and the agent loop (FastAPI, LangGraph).
- `backend/helpdesk/mcp_server/`: the typed tools the agent may call, behind permission checks.
- `backend/helpdesk/scheduler/`: background jobs.
- `backend/helpdesk/shared/`: the policy engine, data models and code both services use.
- `backend/migrations/`, `backend/data/policies/`, `backend/evals/`: schema, policy documents, conversation evals.
- `web/`: the Next.js front end.
- `infra/`: local stack support, Kubernetes manifests and load tests.
- `docs/`: guides; `ARCHITECTURE.md` has the big picture.

## How to work on it

Every change, a feature or a fix, goes through the same loop:

1. **Plan it.** Write down what changes and why, which files it touches, and
   the acceptance criteria: the checks that prove it works, including what must
   be refused or must fail safely.
2. **Write the tests first.** One test per acceptance criterion. They fail at
   this point, because the code does not exist yet. A coding agent can write
   them straight from the criteria.
3. **Make the smallest change that turns them green.** Touch nothing the plan
   does not name.
4. **Run `make verify`.** It runs the same checks CI runs, so a green run here
   means a green run there.
5. **Commit** with a plain message that says what the change does.

Keep your plan files out of git, for example in a folder git ignores. The
repository holds the code, the tests and the history of each change.

## Commands

- `make setup`: install dependencies and the pre-commit hooks.
- `docker compose up -d`, then `make migrate`: start the backing services and create the schema.
- `make test-fast`: the everyday test run.
- `make verify`: everything CI checks, in order.
- `make help`: every other target.
