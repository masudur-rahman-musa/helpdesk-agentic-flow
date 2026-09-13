# AI Helpdesk with Governed Access

An IT helpdesk agent that performs the fix instead of describing it. Employees ask for access in plain language; the system decides, grants, or escalates — and every decision is policy-cited, permissioned, time-bound, and auditable.

The model never executes anything: it interprets intent, while a deterministic policy engine decides and typed, RBAC-guarded tools act. Approval queues, segregation-of-duties checks, idempotent mutations, and a hash-chained audit log make wrong answers recoverable instead of dangerous.

**Status: under active development.** Architecture docs, live demo, and evaluation numbers land as milestones complete.

## End-to-end flows and load

`make e2e` drives the five flows of the specification in a real browser against
the real stack: Postgres, Redis, Qdrant, the MCP server, the policy engine and
the built Next app. Exactly one component is replaced, the model, by a stub
gateway replaying canned structured answers, because the flows exist to prove
policy, RBAC, approvals, the kill switch and the audit chain, and none of those
is decided by the model. The specs select on the accessible roles and labels
the UI already carries, so they read the way a screen reader does and double as
an accessibility check.

Needs the local stack up (`docker compose up -d`); it resets the database
before the run, so it is manual and never part of `make verify`.

`make load-smoke` drives 20 concurrent chat sessions for five minutes against
the real gateway under the `agent-default` alias and prints p50 and p95 per
chat turn.

Measured p95: not yet recorded. The number and the machine it was measured on
land here after the first run against a real gateway; a figure copied from a
stubbed run would measure this repo's own overhead and nothing a reader would
care about.

Companion project: [ai-operations-console](https://github.com/developermusa/ai-operations-console) — the gateway, evaluation harness, and observability layer this system runs on.

License: Apache-2.0
