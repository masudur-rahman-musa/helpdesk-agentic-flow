# AI Helpdesk with Governed Access

An IT helpdesk agent that performs the fix instead of describing it. Employees ask for access in plain language; the system decides, grants, or escalates — and every decision is policy-cited, permissioned, time-bound, and auditable.

The model never executes anything: it interprets intent, while a deterministic policy engine decides and typed, RBAC-guarded tools act. Approval queues, segregation-of-duties checks, idempotent mutations, and a hash-chained audit log make wrong answers recoverable instead of dangerous.

**Status: under active development.** Architecture docs, live demo, and evaluation numbers land as milestones complete.

Companion project: [ai-operations-console](https://github.com/developermusa/ai-operations-console) — the gateway, evaluation harness, and observability layer this system runs on.

License: Apache-2.0
