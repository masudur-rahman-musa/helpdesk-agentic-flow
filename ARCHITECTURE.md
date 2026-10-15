# Architecture

A Next.js front door where employees ask for access in plain language; a FastAPI agent service that extracts intent, retrieves the governing policy from Qdrant, and runs the turn as a LangGraph graph that pauses at approval gates; an MCP tool server that is the only path to any mutation, enforcing RBAC, idempotency and the audit chain; a deterministic policy engine (pure Python) that decides risk class and auto/approval/blocked; a scheduler for expiries, reviews and leaver sweeps; Postgres, Redis and Qdrant underneath. Every model call goes through the companion gateway (ai-operations-console). A diagram lands at the first milestone; the request lifecycle and grant state machine go in docs/diagrams/.

## Web app

The web app has ten tabs, one route each. Every tab is shown to every signed-in
user. Who may use one is decided by the tool behind it, not by the navigation:
a caller without the right role gets the tool's forbidden answer as one plain
sentence on the page, never a hidden link or a 404.

| Tab | Route | Job | Who it works for |
|---|---|---|---|
| Chat | `/chat` | The front door. Ask in plain language; see each step the agent takes and a decision card with the reason, expiry, approvers and policy citations. | everyone |
| My requests | `/requests` | Your requests with status, expiry and the approvers still owed. | everyone |
| My access | `/access` | What you hold now, with an expiry countdown and a renewal request. | everyone |
| Approvals | `/approvals` | Requests waiting on you, each with the engine's reasons, risk class, segregation-of-duties check and the policy text it relied on. Approve or reject with a comment. | managers (own department), system owners (own systems), HR, IT admin, platform admin |
| Reviews | `/reviews` | The periodic access review. Each grant is flagged stale, non-standard or conflicting; keep or revoke it. | managers, system owners, HR, IT admin, platform admin |
| Notifications | `/notifications` | In-app messages: approval needed, grant expiring, request decided. | everyone |
| Audit log | `/admin/audit` | Search and filter the audit log, export CSV, and verify the hash chain. | managers, system owners, HR, IT admin, auditor, platform admin (scoped) |
| Kill switch | `/admin/killswitch` | Turn a tool off for one tenant or for all, with a reason. | platform admin |
| Rules | `/admin/rules` | The segregation-of-duties rules, read only. | IT admin, platform admin |
| Policies | `/admin/policies` | Policy documents and their ingested versions; re-ingest one. | IT admin, platform admin |

The browser never calls the agent service directly. Each tab reads and writes
through server routes under `web/app/api/`, which hold the session token and
validate every response against a schema before it reaches a component.
