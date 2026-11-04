# Design notes

Three short parts. What the project is built with, the choices made on purpose,
and what would change at ten and a hundred times the load.

## What it is built with

The agent is a tool-calling loop, run as a four-node LangGraph `StateGraph` that
saves each step in Postgres. It lives in `backend/helpdesk/agent_api/agent/graph.py`.
The model can only propose a change. Any id in a change must come from a lookup
in the same turn, and replies are checked against what the tools returned.
There is one prompt file, `backend/helpdesk/agent_api/agent/prompts/agent_loop.md`,
and its sha256 hash is sent with every model call. Each turn has a budget of
40,000 tokens and 90 seconds. Why it is built this way is in
[agent-loop-and-langgraph.md](agent-loop-and-langgraph.md).

The tools run on an MCP server over streamable HTTP. Every tool goes through
one path, in `backend/helpdesk/mcp_server/tools/dispatch.py`.

Policy search is in `backend/helpdesk/mcp_server/services/rag/`. Documents are
redacted, cut into chunks and stored in Qdrant, one collection per tenant.
Search uses dense and sparse vectors from fastembed, and refuses when no
passage is close enough.

The backend is Python 3.12 under `mypy --strict`, with FastAPI, asyncio,
SQLAlchemy 2 and asyncpg. PostgreSQL holds the data, with Alembic migrations in
`backend/migrations/`. Every query is filtered to the signed-in tenant, in
`backend/helpdesk/shared/core/db.py`. Redis holds the kill switches, the chat
rate limit and the scheduler's locks. The web app is Next.js 15, React 19 and
strict TypeScript in `web/src/`.

The backend tests run in parallel with pytest-xdist and need 80% coverage. The
policy engine needs 100%. There are 42 multi-turn conversation cases in
`backend/evals/conversations/cases.jsonl`, some of them prompt injection
attempts. Traces go to Langfuse, and Prometheus metrics are in
`backend/helpdesk/shared/core/observability/metrics.py`. Each service has a
Dockerfile, Kubernetes manifests are in `infra/k8s/`, and GitHub Actions runs
the checks in `.github/workflows/ci.yml`.

## Choices made on purpose

- **The chat is an agent loop with locked writes.** Code holds each change the model proposes and runs it only when the person says yes.
- **The policy engine is pure.** It does no input or output, so it is easy to test fully, and the decision a tool enforces is the one the reply explains.
- **The stream carries the checked reply.** A word sent to the screen cannot be taken back, so the reply check runs first.
- **Idempotency keys live in PostgreSQL, next to the change.** The change and its saved answer are written in one transaction, so a crash cannot lose one of them.
- **Kill switches fail closed.** If Redis cannot be read, every tool call is refused. The price is that every tool call needs Redis.
- **The scheduler is a plain loop with Redis locks.** So every sweep can be called from a test with a fixed time.
- **The approval queue lives on the request row.** So a decision and the grant it causes are saved together.
- **Model access goes through a gateway by alias.** Code names an alias such as `agent-default`. The companion [llmops-console](https://github.com/masudur-rahman-musa/llmops-console) maps it to a real model.

## At 10x and 100x

At ten times the load, nothing in the design has to change. The services keep
no state between requests, and the scheduler claims each job with a Redis key.
Postgres feels it first. Connection pooling and a read replica for audit search
fix that.

At a hundred times, three things need a new shape.

- **The audit log.** Each tenant's chain is kept in order, so new events wait in line. The fix is to add events in batches, not in parallel.
- **Policy search.** Thousands of per-tenant Qdrant collections stop being cheap. Tenants would share collections with a tenant filter. That is a weaker promise and needs its own tests.
- **The scheduler.** One run sweeps every tenant, and would take longer than its own time window. It would become a queue of small per-tenant jobs.

None of this is needed today.
