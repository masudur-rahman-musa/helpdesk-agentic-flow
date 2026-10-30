# AI Helpdesk Agentic Flow

Employees ask for system access in plain language. An AI agent works out what
they need. A deterministic policy engine decides. The system then grants the
access, sends it to human approvers, or blocks it. Every decision names the
rule behind it, carries an end date, and lands in a tamper-evident audit log.

The hard part is safety, not chat. The language model never changes anything
itself. It can only propose a change. Code checks the proposal, the person
says yes, and a typed MCP tool runs it behind role-based access control
(RBAC). Risky access waits for one to three human approvers. Each tenant's
data is kept apart, and tenant and user always come from the signed token,
never from what the model wrote.

**What was built**

- An agent service (FastAPI) that runs each chat turn as a tool-calling loop
  in LangGraph, with a fixed budget per turn and a check on every reply.
- An MCP server with 36 typed tools. Every call passes the token check, a kill
  switch, the RBAC matrix and input validation, and is written to the audit
  log. Every change also runs at most once per idempotency key.
- A pure Python policy engine (no I/O, 100% line and branch coverage) that
  sets the risk class, the expiry cap, the approver chain and the
  segregation-of-duties blocks.
- Hybrid RAG over the policy documents in Qdrant, with a refusal when no
  passage is close enough.
- A scheduler that expires grants, warns before expiry, offboards leavers,
  opens access reviews and checks the audit chain.
- A Next.js 15 and TypeScript web app for chat, approvals, reviews, audit
  search and admin work.
- 45 recorded conversation cases for evals, over 6,500 backend tests, CI on
  GitHub Actions, Docker images and Kubernetes manifests.

To run it on a laptop, see the [Quickstart](#quickstart) below or the full
guide in [docs/local-setup.md](docs/local-setup.md).

## Problem statement

Access requests are the most common ticket in most IT queues, and the one
nobody wants to automate. The work is small. Read the request, find the policy
that covers it, check the person may have it, grant it for a set time, and
write down why. It stays manual because a mistake is costly in one direction.
Granting too much stays invisible until an audit or a breach. Six months
later, the person who approved it cannot remember why.

The obvious use of a language model makes this worse. A model that can call a
write API can grant production database access because a sentence sounded
confident. So the real problem is not getting a model to act. It is building a
system where the model reads and explains, and deterministic code decides. That
keeps the speed without the failure that made the queue manual in the first
place.

This system answers in one turn what a helpdesk answers in a day. Every grant
can be questioned afterwards, because it names the rule that allowed it, the
people who approved it, and the date it ends.

## Demo

Video not yet recorded.

Until it exists, [docs/demo-accounts.md](docs/demo-accounts.md) has a short
walkthrough. It signs in as a seeded employee, files one critical request, and
takes it through three approvers to a granted role.

## Architecture diagram

```mermaid
flowchart TB
    user([Employee or approver])
    web["web<br/>Next.js 15"]
    api["agent-api<br/>FastAPI, agent loop in LangGraph"]
    mcp["mcp-server<br/>36 typed tools behind RBAC"]
    sched["scheduler<br/>asyncio loop, Redis locks"]
    policy["policy engine<br/>pure library, no I/O"]
    gw["model gateway<br/>companion llmops-console"]
    pg[("PostgreSQL<br/>rows, audit chain, idempotency keys, saved steps")]
    redis[("Redis<br/>kill switches, rate limit, job locks")]
    qdrant[("Qdrant<br/>policy chunks, search cache")]

    user --> web
    web -- "server routes, JSON and SSE" --> api
    api -- "chat completions, alias only" --> gw
    api -- "MCP over streamable HTTP, caller's token" --> mcp
    api -- "chat sessions, saved steps" --> pg
    api -- "rate limit" --> redis
    mcp --> policy
    mcp --> pg
    mcp -- "kill switches" --> redis
    mcp -- "hybrid search" --> qdrant
    mcp -- "redaction before embedding" --> gw
    sched --> policy
    sched --> pg
    sched -- "job locks" --> redis
```

The chat model is reached from one service, through the gateway, under an
alias such as `agent-default`. No file in this repository names a provider
model. Every change made on a person's behalf goes through the MCP server,
which checks the caller's token before the tool runs.

The diagrams are Mermaid sources that GitHub draws, so the picture and its
source cannot disagree:

- [docs/diagrams/architecture.md](docs/diagrams/architecture.md) is the one above.
- [docs/diagrams/request-lifecycle.md](docs/diagrams/request-lifecycle.md) follows one turn from sentence to reply.
- [docs/diagrams/grant-lifecycle.md](docs/diagrams/grant-lifecycle.md) shows how access ends.
- [docs/diagrams/approval-lifecycle.md](docs/diagrams/approval-lifecycle.md) shows what a request waits for.
- [docs/agent-loop-and-langgraph.md](docs/agent-loop-and-langgraph.md) explains why the agent is a loop and why it runs in LangGraph.
- [ARCHITECTURE.md](ARCHITECTURE.md) covers every part in more depth.

## Guarantees

Seven claims, each linked to the test that fails if it stops being true.

1. **No direct mutation.** The model plans and typed tools act. Every tool call is checked against the caller's token at the transport, before the handler runs. Proven by [test_matrix_expectation_holds_through_the_transport](backend/tests/mcp_server/test_mcp_rbac_matrix.py).
2. **Tenant isolation.** Every read and write is bound to the tenant on the token. Another tenant's row is not forbidden, it is absent. Proven by [test_other_tenants_id_is_not_found](backend/tests/shared/crud/test_repository.py).
3. **Mandatory expiry.** Access is granted for a set time, capped by its risk class, and the cap is applied when the grant is made. Proven by [test_the_grant_expires_exactly_one_class_cap_after_granted_at](backend/tests/shared/test_access_execute.py).
4. **Segregation of duties blocks, and cites.** A conflicting pair of roles is refused with the id of the rule that refused it, not a generic denial. Proven by [test_classification_accepts_a_sod_conflict_reason_with_an_id](backend/tests/shared/test_policy_decision.py).
5. **A cited answer or none.** Below the retrieval threshold the policy search refuses and offers a ticket instead of an answer from the model's memory. Proven by [test_a_best_cosine_just_below_the_threshold_refuses](backend/tests/mcp_server/services/rag/test_rag_refusal.py).
6. **Hash-chained audit.** Each event carries the hash of the one before it, so an edited, deleted or relabelled row breaks verification. Proven by [test_relabelling_a_chain_onto_another_tenant_is_detected](backend/tests/shared/audit/test_audit_writer.py).
7. **Idempotent retry.** A repeated call with the same key returns the first result and runs no second effect, so a timeout is safe to retry. Proven by [test_replay_returns_the_stored_response_without_running_the_effect](backend/tests/mcp_server/services/idempotency/test_idempotency_guard.py).

A guarantee whose link breaks fails the suite. `infra/tests/test_docs.py`
parses this README, opens each target and looks up each named test.

## Skills and tech shown

Each line names the file or feature that shows it.

- **Python.** Python 3.12 across `backend/helpdesk/`, typed under `mypy --strict`.
- **Async Python.** asyncio services, SQLAlchemy 2 async with asyncpg, and an asyncio scheduler loop in `backend/helpdesk/scheduler/runner.py`.
- **Agentic systems.** A tool-calling agent with a turn budget, changes held for the person's yes, and a reply check, in `backend/helpdesk/agent_api/agent/graph.py`.
- **LangGraph.** A four-node `StateGraph` with a Postgres checkpoint saver, explained in [docs/agent-loop-and-langgraph.md](docs/agent-loop-and-langgraph.md).
- **MCP.** An MCP Python SDK server over streamable HTTP, with one dispatch path for every tool, in `backend/helpdesk/mcp_server/tools/dispatch.py`.
- **RAG.** Redaction, chunking, hybrid retrieval and a refusal threshold in `backend/helpdesk/mcp_server/services/rag/`.
- **Vector DBs.** Qdrant with one collection per tenant, dense and sparse vectors from fastembed, and a semantic search cache in `backend/helpdesk/mcp_server/services/rag/search_cache.py`.
- **Prompt engineering.** One versioned prompt file, `backend/helpdesk/agent_api/agent/prompts/agent_loop.md`, whose sha256 travels with every model call as the prompt version.
- **Guardrails and AI safety.** The model only proposes changes. Ids in a change must come from this turn's lookups (`backend/tests/agent_api/test_agent_guardrails.py`). Replies are checked against tool results, and tool output is treated as data, never as instructions.
- **Evals and LLM evaluation.** 45 multi-turn cases in `backend/evals/conversations/cases.jsonl`, including prompt injection attacks, scored by exact tool calls and a judge. Scores per engine are in `backend/evals/engine-scores.json`.
- **Multi-tenant isolation.** A session guard that filters every query to the bound tenant, in `backend/helpdesk/shared/core/db.py`, plus per-tenant Qdrant collections, audit chains and kill switches.
- **PostgreSQL.** Alembic migrations in `backend/migrations/`, row locks, advisory locks, an append-only trigger on the audit table, and a restricted app role.
- **Redis.** Kill switches that fail closed, a per-session chat rate limit, and scheduler job locks.
- **FastAPI.** The agent API in `backend/helpdesk/agent_api/api/`, plus health and metrics routes on every service.
- **pytest.** Over 6,500 backend tests, parallel with pytest-xdist, an 80% coverage floor and a 100% floor on the policy engine.
- **TypeScript and React.** A strict TypeScript, React 19 and Next.js 15 app in `web/src/`, with TanStack Query and zod checks on every response.
- **Docker.** One Dockerfile per service and one `docker-compose.yml` for the whole local stack.
- **CI/CD and GitHub Actions.** `.github/workflows/ci.yml` runs lint, types, sharded tests, coverage, the web checks and security scans. `build.yml` builds the images and scans them with Trivy.
- **Kubernetes.** Kustomize manifests in `infra/k8s/`.
- **Observability for AI.** Langfuse traces (its v3 SDK is built on OpenTelemetry), a W3C `traceparent` shared with the gateway, and Prometheus metrics in `backend/helpdesk/shared/core/observability/metrics.py`.
- **Cost and latency awareness.** A 40,000 token and 90 second budget per turn, a cheap model alias by default, prompt order kept stable so the gateway can cache, and a locust load test in `infra/load/load_smoke.py`.
- **LLMOps.** Model aliases behind a gateway, prompt versions on every trace, and eval scores tracked per agent engine.
- **System design.** Four services with clear jobs, transactions that tie each change to its audit event, and the scaling notes in [At 10x and 100x](#at-10x-and-100x).

## What broke and how I fixed it

- **A key check accepted a trailing newline.** Pydantic's `pattern=` matches anywhere in the string, so a value ending in a newline passed a pattern anchored only at the start. The fix was a full-string match in an explicit validator, plus a trailing newline, a carriage return and a space in every rejection table.
- **The refusal threshold refused everything.** The number was chosen for a similarity score but compared to a fused rank score on a very different scale, so almost no answer cleared it. The fix measures the threshold on the dense cosine, while the fused score keeps ordering the page.
- **An approval could have granted twice.** The approval tool runs the grant in the same transaction as the last approver's seat. An older chat design also tried to run it when the waiting conversation picked up again. The fix was to make the chat never run it. The person now hears about the decision through a notification.
- **A turn that ran out of budget told the user nothing had happened.** The fixed refusal was sent even when the turn had already filed a request, so the reply and the audit log disagreed. The fix sends the fixed refusal only when nothing ran. When something did run, the model gets one last answer-only step to report it.
- **Two paths turned the same object into different bytes.** The idempotency fingerprint and the audit chain each built their own JSON, and key order differed, so a replay looked like a conflict. The fix makes both call one shared serialiser, pinned by a test whose keys sort differently in Python and in the database.
- **One client had a timeout and its neighbour did not.** One call could hang forever while the one beside it gave up in seconds. The fix gives every client the code builds a connect and a read timeout, checked on the client object rather than on a constant.

## Design decisions

These choices were made on purpose and shape the product.

- **The chat is an agent loop in LangGraph, with locked writes.** The loop is the method. The model looks things up with tools and proposes changes. LangGraph is the frame, with four nodes and each step saved. Code holds each change, shows it in fixed words, and runs it only on the person's own yes. The full reasoning and the scores are in [docs/agent-loop-and-langgraph.md](docs/agent-loop-and-langgraph.md).
- **The policy engine is pure.** It has no input or output and imports no models or services. It is table-driven and held at 100% coverage. The decision a tool enforces is the same decision the reply explains.
- **The stream carries the checked reply.** Streamed tokens are slices of a reply that already passed the reply check, because a token on the wire cannot be taken back.
- **Idempotency keys live in PostgreSQL, next to the effect.** The effect and its stored answer commit in one transaction, so a crash cannot run an action and lose its result. An advisory lock makes a second caller wait and then replay.
- **Kill switches fail closed.** If Redis cannot be read, every tool call is refused. An operator must be able to trust a switch they set, and the price is that Redis is needed for every call.
- **The scheduler is a plain loop with Redis locks**, not a job framework, so every sweep can be called from a test with a session and a fixed time.
- **The approval queue lives in PostgreSQL**, on the request row, so a decision and its grant share one transaction.
- **Model access is a gateway with aliases.** Code names an alias such as `agent-default`. The mapping to a provider model lives in the companion console, next to the budgets and the traces.

## Failure modes

What the system does when a part is gone, rather than when everything works.

| Failure | What the system does | What the user sees |
|---|---|---|
| Model gateway down | The model step fails, so the turn ends on a fixed failure reply with a typed error. The agent API's `/readyz` reports not ready. Tools, the policy engine and the audit log do not use the gateway. | A plain "try again, or open a ticket" message. Approvals, reviews and admin pages keep working. |
| Qdrant down | `search_policy` returns a typed error, so the turn has no passage to cite. The prompt tells the model to answer rules questions only from returned passages. That part is a prompt rule, not a code check. | No cited policy answer. Access requests still work, because the policy engine does not use Qdrant. |
| Redis down | The kill switch check fails closed, so every MCP tool call is refused with a typed error. The chat rate limit refuses the turn too. Scheduler jobs cannot take their locks. | A typed error on chat and on every page that reads through a tool. Nothing changes while Redis is gone. |
| Model returns garbage | A change with bad arguments goes back to the model once with the reason, then the turn ends on a fixed reply. A reply that claims a request number, role or decision word no tool result backs goes back at most twice, then code writes the reply from the results. | A correct reply, a fixed reply built from the tool results, or the failure message. Never an invented decision. |
| A tool call timeout mid-execution | Every client has connect and read timeouts, and a turn has a 90 second deadline. When time runs out with results in hand, the model gets one answer-only step. With no results, the turn ends on a fixed reply. A change that ran keeps its idempotency key, so a retry returns the first result. | An answer built from what did run, or a plain "ran out of time" message. |
| Two approvers act at once | Both decisions lock the request row first. Two approvers on the last seat send different idempotency keys, so the row lock is what stops a double grant. | One approval recorded. The other approver is told the request is no longer pending. No double grant. |

## Numbers

Filled in only from a real run. An unmeasured cell says so rather than showing
a likely number.

| Metric | Result |
|---|---|
| Decision accuracy | not yet measured |
| Refusal precision and recall | not yet measured |
| Citation accuracy | not yet measured |
| Cost per turn | not yet measured |
| p95 latency per chat turn | not yet measured |

`make load-smoke` drives 20 chat sessions at once for five minutes against the
real gateway and prints p50 and p95 per turn. `make eval-chat` plays the
conversation cases and the companion console scores them. Both need a real
gateway and cost money. A figure from a stubbed model would only measure this
repository's own overhead.

Conversation eval pass rate is 21 of 33 conversation cases on the last scored
run (2026-10-23, the LangGraph engine), as recorded in
[`backend/evals/engine-scores.json`](backend/evals/engine-scores.json). The
set now holds 45 cases, and the 12 newest wait for the next scored run. The
newest per-case report, from an earlier run that passed 12 of 33
([`backend/evals/reports/chat-report.json`](backend/evals/reports/chat-report.json)),
shows which groups still pass under half of their cases. They are typo,
short answer, follow up, change of mind, other person and other language.

Tests, as counted by `pytest --collect-only` today, are 6,558 in `backend/tests`
and 220 repo-level checks in `infra/tests`. The web app has its own vitest unit
tests in `web/tests/` and Playwright flows in `web/e2e/`.

## At 10x and 100x

At ten times this load, nothing in the design moves. A turn is bounded by the
model call. The services keep no state of their own between requests. The
scheduler claims each job with a Redis key, so a second instance is safe. The
first part to feel it is Postgres, and the first fix is ordinary connection
pooling and a read replica for audit search.

At a hundred times, three things break in a way settings do not fix.

The audit chain is in order per tenant, so appends within one tenant wait in
line behind an advisory lock. That is the price of a chain that can be
checked. The way out is batching appends inside a transaction, not appending
in parallel.

Retrieval over one Qdrant collection per tenant stops being cheap when tenants
number in thousands. Collections then become shared with a tenant filter. The
isolation argument moves from the collection name to the filter, which is a
weaker argument and needs its own tests.

The scheduler sweeps every tenant inside one job run. At a hundred times the
tenants, a sweep outlasts its own window. The job then has to become a queue of
per-tenant units with a pool of workers.

None of this is needed today. It is written down so the person who hits it
knows which parts were chosen for clarity and which were chosen to scale.

## Works council note

In several European countries, a system like this needs the agreement of the
employee representatives before it goes near real staff. The worry is rarely
automation. It is monitoring. A tool that logs what each employee asked for,
and when, is a record of behaviour whether or not anybody meant it as one.

Three properties exist for that conversation.

Telemetry carries the session and the tenant, never the user. Traces, metrics
and logs cannot be joined back to a person. There is one exception. Each model
call for a signed-in user sends the gateway a random id in the `X-User`
header, so cost can be counted per person. The id is made once, never changes,
and is never a name or an email. Only the helpdesk's own database can map it
back to a person, and that trade belongs in the same conversation.

The audit log records decisions about access, not behaviour. It answers who
holds this, since when, approved by whom, and until when. That is what an
auditor asks. It is not a transcript of what anybody typed.

Every decision can be challenged, because it cites the rule it applied and the
people who approved it. An employee can be told why. That is the practical
difference between an automated decision and one nobody can explain.

## Quickstart

You need Docker, git and [uv](https://docs.astral.sh/uv/). The full guide,
with the gateway setup and what works without it, is
[docs/local-setup.md](docs/local-setup.md).

```bash
git clone https://github.com/masudur-rahman-musa/helpdesk-agentic-flow
cd helpdesk-agentic-flow

# 1. The local env file. Drop the two placeholders, then add a login secret.
cd backend
grep -v -e '^JWT_SECRET=' -e '^GATEWAY_API_KEYS=' .env.example > .env.demo
echo "JWT_SECRET=$(openssl rand -hex 32)" >> .env.demo
cd ..

# 2. Postgres, Redis and Qdrant, then the schema and the fake demo data.
docker compose up -d --wait
make migrate
(cd backend && uv run python scripts/seed.py)

# 3. The four app containers, which are agent-api, mcp-server, scheduler and web.
make up
```

Open [localhost:3000](http://localhost:3000) and sign in with a tenant
(`nordlicht` or `cuan`), an email and a password from
[docs/demo-accounts.md](docs/demo-accounts.md). All seeded data is invented,
and every email uses a reserved `.example` domain.

The chat needs the model gateway from the companion
[llmops-console](https://github.com/masudur-rahman-musa/llmops-console),
listening on port 8080, plus one real gateway key per tenant in
`GATEWAY_API_KEYS`. Policy answers also need `make ingest` once per tenant.
Both steps are in [docs/local-setup.md](docs/local-setup.md). Without the
gateway, sign-in, approvals, reviews, audit search and the admin pages still
work.

Checks, from cheapest to most expensive:

```bash
make lint typecheck     # seconds
make test-policy        # the policy engine alone, 100% line and branch coverage
make test-fast          # everyday run, skips slow tests, 3 workers, needs docker compose up -d
make test-slowest       # print the 30 slowest tests and fixtures, serial, no coverage
make test               # the whole suite with the coverage floor
make verify             # everything CI runs, including the web build and security scans
```

How the project was built is described in
[docs/how-it-was-built.md](docs/how-it-was-built.md).

## Integration path to a real directory

This repository ships a simulated directory. Users, departments, memberships
and grants live in its own tables, so the demo runs with no outside system.
Everything above them talks to a port of seven methods, not to those tables.

An adapter for Entra ID, Okta or a SCIM endpoint is one class that implements
that port. Reads answer from the real directory, and `apply_grant` and
`revoke_grant` become calls to it. The policy engine, the RBAC matrix, the
approval chain and the audit chain do not change, because none of them knows
where a membership came from.

### Use it with your own policies

The policy corpus is a folder, not a fixture. Point `POLICY_DIR` at your own
folder and ingest it. The ingest sends each document through the gateway's
redaction before embedding, so the gateway must be running.

```bash
POLICY_DIR=/srv/company-policies make ingest TENANT=your-tenant-slug VERSION=v1
```

The layout is `{tenant}/{version}/{file}`. The shipped `backend/data/policies/`
folder is a demo corpus, not a dependency.

The five things to change first are listed in
[docs/customizing.md](docs/customizing.md). The port is walked through method
by method in [docs/directory-adapter.md](docs/directory-adapter.md).

## Also included

- **Kubernetes.** Kustomize manifests for the four services in `infra/k8s/`. A deploy job builds them. They are never applied from a laptop.
- **CI.** `.github/workflows/ci.yml` runs the checks `make verify` runs. Most jobs call the `make` target itself. The test job splits the same suite into three shards and judges coverage over all of them.
- **Images.** `.github/workflows/build.yml` builds the four images on a push to `main`, scans them with Trivy and publishes them to GHCR. `.github/workflows/image-scan.yml` scans the pinned base images and the four built images every Monday.
- **Security notes.** [SECURITY.md](SECURITY.md) covers the security model and how to report a problem.
- **Agent guide.** [AGENTS.md](AGENTS.md) is a short guide for coding agents and people working on the code.

Companion project
[llmops-console](https://github.com/masudur-rahman-musa/llmops-console) is the
model gateway, eval harness and observability stack this system runs on.

License is Apache-2.0.
