# Architecture

For the same system in plain words, without the engineering, see [How access decisions work](docs/how-access-decisions-work.md).

This page explains how the parts fit together and why. The README has the
short version and the diagram. The four lifecycle diagrams are in
`docs/diagrams/`.

## The parts

One Python project in `backend/` holds a shared library and three apps. The
web app is in `web/`.

| Part | Folder | What it does |
|---|---|---|
| agent-api | `backend/helpdesk/agent_api/` | FastAPI. Sign-in, the chat (JSON and Server-Sent Events), and the routes behind every web page. Runs the agent loop in LangGraph. Calls the model gateway and the MCP server. |
| mcp-server | `backend/helpdesk/mcp_server/` | The only place a change happens. 36 typed tools over MCP streamable HTTP, policy ingest and policy search. |
| scheduler | `backend/helpdesk/scheduler/` | A plain asyncio loop that runs the sweeps for every tenant. |
| shared library | `backend/helpdesk/shared/` | The policy engine, data models, tenant-bound database access, the audit chain, RBAC, kill switches, notifications and settings. |
| web | `web/src/` | Next.js 15, React 19 and strict TypeScript. The browser talks only to the web app's own server routes. |

The three apps never import each other, and the shared library never imports
an app. `backend/.importlinter` states those rules, and a test runs them.

Underneath sit PostgreSQL 16, Redis 7 and Qdrant. The model gateway is a
separate service from the companion llmops-console repository.

## One chat turn

1. The browser posts the message to a server route under `web/src/app/api/`. The route adds the session token from an http-only cookie and calls agent-api.
2. agent-api checks the token and the per-session rate limit (30 messages per 10 minutes, counted in Redis).
3. The turn runs as a LangGraph graph with four nodes. `held_answer` reads the person's answer when a change is waiting. `agent` is one model step. `tools` runs lookups through MCP. `check_reply` holds the reply to the tool results. [docs/agent-loop-and-langgraph.md](docs/agent-loop-and-langgraph.md) covers each node.
4. Each model step goes to the gateway under the `agent-default` alias, with the tools the person's roles allow.
5. Lookups run at once through MCP, with the person's own token. A proposed change does not run. Code checks its arguments, gives it an idempotency key, saves it on the chat and asks the person in fixed words.
6. On the person's yes, the saved calls run exactly as they were checked. The model then writes the reply in one answer-only step.
7. `check_reply` makes sure every request number, role name, decision word and promise of future work comes from this turn's tool results. A reply that fails goes back to the model, at most twice. After that, code writes the reply from the results.
8. The reply streams back as small slices of the already checked text. The steps the turn took stream before it.

A turn has a budget of 4 model steps, 6 tool calls, 40,000 tokens and 90
seconds.

## One tool call

Every MCP tool call goes through `backend/helpdesk/mcp_server/tools/dispatch.py`
in a fixed order:

1. **Identity.** The bearer token is checked (HS256, algorithm pinned, 8 hour life). Tenant and user come only from the token. No tool argument can name them.
2. **Kill switch.** A global switch and a per-tenant switch are read from Redis. An unreadable switch refuses the call.
3. **RBAC.** The matrix in `backend/helpdesk/shared/auth/rbac.py` lists, for every tool, which of the seven principals may call it and with what scope. The principals are employee, manager, system_owner, hr, it_admin, auditor and platform_admin. The scopes are self only, own department, own systems and any.
4. **Input validation.** Every tool has a Pydantic input model, and every change tool refuses unknown fields. RBAC runs first, so a refused caller learns nothing about a tool's inputs.
5. **Tenant-bound session.** Every ORM query on a tenant table is filtered to the bound tenant, or raises when none is bound.
6. **Idempotency, for changes.** The effect and its stored answer commit in one transaction. An advisory lock on the tenant and key makes a second caller wait, then get the first answer. Keys last 7 days.
7. **Audit.** One event per call, success or refusal, appended to the tenant's hash chain in the same transaction as the change. A call whose token fails the check writes no event, because there is no tenant to file it under.

A failure answers with one typed error code and no other text.

## The policy engine

`backend/helpdesk/shared/policy/` is pure Python. It does no I/O and imports
no models or services. `classify` runs in a fixed order:

1. **Risk class.** Low, elevated, privileged or critical, from the role's privilege and the system's sensitivity. The highest matching row wins.
2. **Self-admin bump.** Asking for an admin role on yourself moves the request up one class, so it is never automatic.
3. **Expiry cap.** The grant ends at the smallest of the ask, the role default and the class cap. The caps are 365, 180, 90 and 30 days. Asking for longer is not an error. It is capped, and the cap is a cited reason.
4. **Approver chain.** Low needs nobody. Elevated needs the department manager. Privileged needs the manager and the system owner. Critical adds IT admin for production infrastructure or HR for the HR system. Nobody approves their own request, and a seat that would fall to the requester or the target is moved to someone else.
5. **Blocks.** A segregation-of-duties conflict blocks the request and names the rule.

The engine is table-driven and held at 100% line and branch coverage by
`make test-policy`. The same check runs again when a grant is made, so a rule
that changed while a request waited still applies.

## Approvals and grants

The approver chain is stored as seats on the request row in PostgreSQL. An
approval locks the row, fills the caller's seat and, when the last seat is
filled, makes the grant in the same transaction. One person fills at most one
seat per chain. The chat never waits for an approval. The person hears about
the decision through an in-app notification.

Grants have three states, active, expired and revoked. An IT admin can roll a
request back within 24 hours, which revokes what it granted.

## Policy search (RAG)

Ingest, in `backend/helpdesk/mcp_server/services/rag/`, reads
`{tenant}/{version}/{file}` from `POLICY_DIR`. Each document is redacted
through the gateway first, so raw text is never stored. Then it is chunked and
embedded with two fastembed models. `BAAI/bge-small-en-v1.5` gives the dense
vector and `Qdrant/bm25` the sparse one. An unchanged file is skipped.

Search runs both arms in the tenant's own Qdrant collection, takes 30
candidates from each, and fuses them with Reciprocal Rank Fusion (k = 60) into
a page of 6. The refusal reads the best dense cosine, not the fused score.
Below 0.35 the search refuses and offers a ticket.

A semantic cache sits beside each collection. A new question whose vector is
at least 0.95 close to a recent one, under a day old, gets the cached page. A
policy change makes every old entry miss.

## The audit chain

Each event stores `sha256(previous hash + the event's fields as JSON with
sorted keys)`. An advisory lock per tenant keeps appends in line. The chain is
protected three ways.

- **Database role.** The services connect as a restricted role that may only read and insert audit rows.
- **Trigger.** A trigger refuses any update or delete on the audit table, whoever asks.
- **Anchors.** The scheduler checks each chain every 15 minutes and remembers the last good head. A log cut short at the end, or chained again from the start, no longer matches its anchor.

The Audit log page can search, export CSV and verify a tenant's chain on
demand.

## The scheduler

`backend/helpdesk/scheduler/runner.py` wakes every 60 seconds. Each job takes
one Redis key with `SET NX PX <cadence>`, so a second scheduler cannot run the
same job in the same window. The job then works through every tenant, each in
its own tenant-bound session. A job that fails drops its key and retries on
the next tick.

| Job | Every | What it does |
|---|---|---|
| Expire grants | 5 minutes | Ends grants past their end date and tells the holder and their manager. |
| Expiry warnings | day | Warns holders 14 days before a grant ends. |
| Leaver sweep | day | Offboards people whose last day has passed, through the same code the offboarding tool uses. |
| Review generation | day | Opens the quarter's access reviews. |
| Review reminders | day | Chases late reviews. |
| Metrics sweep | 5 minutes | Reads the gauges, including whether each audit chain still checks out. |
| Chain anchor | 15 minutes | Checks each chain and stores its head. |
| Anchor retention | day | Removes anchors older than 90 days, never the newest. |

## Observability

- **Traces.** Langfuse, through its v3 SDK, which is built on OpenTelemetry. Tracing is off unless both keys are set, and a tracing error never fails a turn. Spans hold names, ids and counts, never message text.
- **Shared trace with the gateway.** Every model call carries a W3C `traceparent`, so the helpdesk turn and the gateway call sit in one trace.
- **Metrics.** Prometheus counters and histograms on every Python service at `/metrics`, plus gauges from the scheduler. Labels are enums, tool names or tenant ids, never a person.
- **Logs.** Structured lines with session and tenant. A filter drops email, display name, password and user id fields.
- **Health.** `/healthz` says the process is up. `/readyz` says its stores, and for agent-api the gateway, answer.

## The web app

The web app has twelve tabs, one route each. The nav shows the tabs that the
user's roles may use, based on `/api/me`. Hiding a tab is only for looks. The
tool behind each page is the real check, and a refused call shows its
forbidden answer as one plain sentence.

| Tab | Route | What it is for | Who can use it |
|---|---|---|---|
| Chat | `/chat` | Ask in plain language. See each step the agent takes, and a decision card with the reason, expiry, approvers and policy citations. | everyone |
| My requests | `/requests` | Your requests with status, expiry and the approvers still to act. | everyone |
| My access | `/access` | What you hold now, with an expiry countdown and a renewal request. | everyone |
| Approvals | `/approvals` | Requests waiting on you, with the engine's reasons, risk class, segregation-of-duties check and policy text. Approve or reject with a comment. | managers, system owners, HR, IT admin, platform admin |
| Reviews | `/reviews` | The periodic access review. Keep or revoke each flagged grant. | managers, system owners, HR, IT admin, platform admin |
| Notifications | `/notifications` | Approval needed, grant expiring, request decided. | everyone |
| Tickets | `/admin/tickets` | Every open ticket in the tenant, and solving one. | IT admin, platform admin |
| Audit log | `/admin/audit` | Search and filter the audit log, export CSV, verify the chain. | managers, system owners, HR, IT admin, auditor, platform admin, each within their scope |
| Kill switch | `/admin/killswitch` | Turn a tool off for one tenant or for all, with a reason. | platform admin |
| Rules | `/admin/rules` | The segregation-of-duties rules, read only. | IT admin, platform admin |
| Policies | `/admin/policies` | Policy documents and their ingested versions. Ingest one again. | IT admin, platform admin |
| Settings | `/admin/settings` | Whether the chat shows the agent's tool steps. | platform admin |

The browser never calls agent-api directly. Each page reads and writes through
server routes under `web/src/app/api/`, which hold the session token and check
every response against a zod schema before it reaches a component.
`web/README.md` explains the folder layout.

## Deployment pieces

These are included but not needed to run locally.

- `backend/Dockerfile.agent-api`, `backend/Dockerfile.mcp-server`, `backend/Dockerfile.scheduler` and `web/Dockerfile` build one image per service. Each image holds only its own app's dependencies, so only mcp-server carries fastembed.
- `infra/k8s/` holds Kustomize manifests for the four services. The image tags are set by a deploy job.
- `.github/workflows/` holds CI, the image build and a weekly image scan.
