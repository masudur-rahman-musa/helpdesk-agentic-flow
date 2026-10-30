# Security

## Reporting a problem

Please report a security problem privately, through the Security tab of this
repository on GitHub. If private reporting is not open, open an issue that
says you found something, without the details, and the maintainer will
contact you.

This is a portfolio project that runs on demo data. No real account, person or
company is behind it.

## The security model

**The model cannot change anything.** The language model only proposes. Every
change is a typed MCP tool. Code checks the arguments, the person says yes,
and only then does the tool run, under the person's own token.

**Identity comes from a signed token.** Tokens are HS256 with the algorithm
pinned on decode, and they last 8 hours. Tenant and user come only from the
token, never from tool arguments or from model output. Passwords are hashed
with Argon2id. A failed login says nothing about why it failed.

**Every tool call follows one path.** Identity, then the kill switch, then
RBAC, then input validation, then the tool, then the audit event. RBAC runs
before validation, so a refused caller learns nothing about a tool's inputs.

**RBAC with scopes.** Seven principals and four scopes (self only, own
department, own systems, any) are set per tool in one matrix in
`backend/helpdesk/shared/auth/rbac.py`. Hiding a tab in the web app is only
for looks. The tool's own check is what counts.

**Tenant isolation.** Every database query on a tenant table is filtered to
the tenant bound to the session, or it raises. Each tenant has its own Qdrant
collections, its own audit chain and its own kill switches. Another tenant's
row reads as absent, not forbidden.

**Kill switches fail closed.** A platform admin can stop any tool for one
tenant or for all. If Redis cannot be read, every tool call is refused.

**Idempotent changes.** Each change carries a key. The effect and its stored
answer commit together, so a retry returns the first result and never runs
the effect twice.

**Tamper-evident audit.** Each audit event carries the hash of the one before
it. The services connect as a restricted database role that may only read and
insert audit rows, and a trigger refuses any update or delete. The scheduler
checks each chain and stores its head, so a log cut short is caught too.

**Prompt injection.** Tool results, policy text and earlier messages are
passed to the model as data. Any id in a proposed change must come from a
lookup in the same turn. Request numbers, role names and decision words in a
reply must match this turn's tool results. The conversation eval set includes
attack cases.

**Redaction before storage.** Policy documents are redacted through the
gateway before they are chunked and embedded, so raw text is never stored in
the vector store.

**No personal data in telemetry.** Traces, metrics and logs carry the session
and the tenant, never the user. Logs drop email, name, password and user id
fields. The gateway gets a random per-user id for cost counting, never a name
or an email.

## Secrets

- No secret is in code, config, tests or fixtures.
- Local values live in `backend/.env.demo`, and real keys in `backend/.env`. Both are gitignored. `backend/.env.example` lists every name with placeholders only.
- The demo passwords in `docs/demo-accounts.md` open fake accounts on a local database only.

## Scans

`make security` runs the same scans as CI.

- gitleaks over the whole git history.
- pip-audit over the locked Python dependencies.
- `pnpm audit` over the web dependencies.
- Trivy over both compose files.

The image build scans the four built images with Trivy, and a weekly job scans
the pinned base images.

## Known limits

- One shared `JWT_SECRET` signs tokens for both agent-api and mcp-server. A real deployment would move to asymmetric keys or the company's identity provider.
- Sign-in uses local passwords against the simulated directory. There is no single sign-on.
- The `/metrics` routes have no authentication. Their labels never hold a person, but they should not face the internet.
- The rate limit is per chat session. It stops runaway clients and is not abuse protection.
