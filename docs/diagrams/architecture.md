# Architecture

What runs, and what talks to what. The same diagram is embedded in the README.

```mermaid
flowchart TB
    user([Employee or approver])
    web["web<br/>Next.js"]
    api["agent-api<br/>FastAPI + agent loop"]
    mcp["mcp-server<br/>typed tools behind RBAC"]
    sched["scheduler<br/>asyncio loop, Redis locks"]
    policy["policy engine<br/>pure, no I/O"]
    gw["model gateway<br/>companion console"]
    pg[("PostgreSQL<br/>rows, audit chain, checkpoints")]
    redis[("Redis<br/>idempotency, kill switches, locks")]
    qdrant[("Qdrant<br/>policy chunks")]

    user --> web
    web -- "HTTP and SSE" --> api
    api -- "aliases only" --> gw
    api -- "hybrid retrieval" --> qdrant
    api --> policy
    api -- "streamable HTTP" --> mcp
    mcp --> policy
    mcp --> pg
    mcp --> redis
    sched --> pg
    sched --> redis
    api --> pg
```

Three properties are visible in the edges rather than in prose.

The model is reachable only from `agent-api`, and only through the gateway
under an alias. No service names a provider model.

Every write to PostgreSQL on a user's behalf arrives through `mcp-server`,
which authorizes the call against the caller's token before the handler runs.
`agent-api` writes only its own session and checkpoint rows.

The policy engine is a library, not a service. Both callers import the same
pure module, so the decision a tool enforces is the decision the turn
explained.
