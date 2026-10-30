# Run it on your laptop

Everything here runs on one machine. Nothing is deployed and no cloud account
is needed. The only paid part is the model behind the chat, which lives in the
companion [llmops-console](https://github.com/masudur-rahman-musa/llmops-console)
repository.

## What you need

- **Docker** with Compose v2 (Docker Desktop, or Docker Engine on Linux).
- **git** and **openssl**, which most systems already have.
- **[uv](https://docs.astral.sh/uv/)**. It installs Python 3.12 for you, as `backend/.python-version` asks.
- **Node 22 and pnpm 10**, only for the web checks (`make web-verify`) or for running the web app outside Docker.

## 1. Clone

```bash
git clone https://github.com/masudur-rahman-musa/helpdesk-agentic-flow
cd helpdesk-agentic-flow
```

## 2. Make the local env file

The apps read `backend/.env.demo` first, then `backend/.env` over it. Both are
gitignored. `backend/.env.demo` holds working local values. `backend/.env` is
only for real keys you paid for, and this project needs none of its own.

`backend/.env.example` lists every setting. Some lines in it are left empty on
purpose, and an empty value fails the settings check at startup. So copy it
without the empty lines, then add the values that must change:

```bash
cd backend
grep -v -e '=$' -e '^POLICY_DIR=' -e '^JWT_SECRET=' -e '^GATEWAY_API_KEYS=' .env.example > .env.demo
echo "POLICY_DIR=data/policies" >> .env.demo
echo "JWT_SECRET=$(openssl rand -hex 32)" >> .env.demo
cd ..
```

What each added line does:

- `POLICY_DIR=data/policies` points the ingest at the demo policy folder, `backend/data/policies/`. The ingest runs from `backend/`, so the path is relative to it.
- `JWT_SECRET` signs the login tokens. It must be at least 32 bytes.

The placeholder gateway keys are left out too. Real ones come in step 6. The
local Postgres, Redis and Qdrant values in the file already match what
`docker-compose.yml` starts.

## 3. Start the backing services

```bash
docker compose up -d --wait
```

This starts Postgres 16 on port 5432, a second Postgres for the test suite on
5433, Redis 7 on 6379 and Qdrant on 6333. On a fresh volume, an init script in
`infra/local/postgres/` gives the restricted app role its password.

## 4. Create the schema and the demo data

```bash
make migrate
(cd backend && uv run python scripts/seed.py)
```

`make migrate` runs the Alembic migrations and creates LangGraph's checkpoint
tables. The seed writes two invented companies, `nordlicht` and `cuan`, with
their people, systems, roles, grants and segregation-of-duties rules. It gives
the same rows on every run and is safe to run again.

## 5. Start the app

```bash
make up
```

This builds and starts the four app containers.

| Service | Port | Health |
|---|---|---|
| web | 3000 | `http://localhost:3000/healthz` |
| agent-api | 8000 | `http://localhost:8000/healthz` and `/readyz` |
| mcp-server | 8081 | `http://localhost:8081/healthz` and `/readyz` |
| scheduler | 8082 | `http://localhost:8082/healthz` and `/readyz` |

Each of the three Python services also serves Prometheus metrics on `/metrics`.

Open [localhost:3000](http://localhost:3000). The sign-in form asks for a
tenant (`nordlicht` or `cuan`), an email and a password. Every demo login is
in [demo-accounts.md](demo-accounts.md).

At this point everything except the chat answers works. You can sign in, look
at access and requests, approve, run reviews, search and verify the audit log,
and use the admin pages. The agent API's `/readyz` stays at 503 until a
gateway answers, which is expected.

## 6. Connect the model gateway

The chat model and the redaction step of the policy ingest both go through the
gateway in the companion llmops-console repository. The helpdesk expects it at
`http://localhost:8080/v1`. Containers reach it through
`host.docker.internal:8080`, which `docker-compose.yml` already sets.

The two stacks run side by side. The console publishes its own Postgres,
Redis and Qdrant on shifted ports (15432, 16379 and 16333), so nothing clashes.

The gateway checks one key per tenant. The helpdesk holds the key itself, and
the gateway holds only its sha256 digest. Make one pair per tenant:

```bash
nord=$(openssl rand -hex 32)
cuan=$(openssl rand -hex 32)

# For this repo. Add it to backend/.env.demo.
echo "GATEWAY_API_KEYS=nordlicht:$nord,cuan:$cuan" >> backend/.env.demo

# For the console. Copy this line into its backend env file.
echo "OPSCONSOLE_API_KEYS=nordlicht:$(printf %s "$nord" | shasum -a 256 | cut -d' ' -f1),cuan:$(printf %s "$cuan" | shasum -a 256 | cut -d' ' -f1)"
```

Then follow the console's README to start it, with a model provider key in
its `backend/.env`. The helpdesk only ever names the aliases `agent-default`
and `agent-strong`. The console maps them to real models.

Restart the helpdesk containers so they read the new keys:

```bash
make rebuild
```

`curl http://localhost:8000/readyz` should now answer 200.

## 7. Load the policy documents

Policy questions in the chat need the policy corpus in Qdrant. The ingest
redacts each document through the gateway, then embeds it with fastembed. The
first run downloads two small embedding models into `backend/.cache/fastembed`.

```bash
make ingest TENANT=nordlicht VERSION=v2
make ingest TENANT=cuan VERSION=v2
```

Running it again over unchanged files writes nothing. An IT admin can also
ingest one document again from the Policies page.

## Run the tests

The backend tests use the second Postgres on port 5433, plus Redis and
Qdrant, so keep `docker compose up -d` running. They also read
`backend/.env.demo`.

```bash
make test-policy    # the policy engine alone, 100% line and branch coverage, no services
make test-fast      # everyday run, skips slow tests, 3 workers
make test           # the whole backend and repo suite with the 80% coverage floor
make lint typecheck # ruff and mypy --strict
make web-verify     # web types, lint, vitest and a production build
make verify         # all of the above plus the security scans, which need Docker and the network
```

No test calls a real model. Model behaviour is measured by the conversation
evals instead, which cost money and run by hand.

## Daily use

- `make rebuild agent-api` rebuilds one app container. `make rebuild` rebuilds all four. Neither touches the databases.
- `make down` stops the stack. Your data stays in the Docker volumes.
- `make help` lists every target.

**Watch mode, optional.** `docker-compose.override.example.yml` runs the four
app containers from your checkout, so a saved file shows up with no rebuild.
Copy it to `docker-compose.override.yml` to use it. It also publishes Postgres
on 55432 and Qdrant on 26333, so it does not clash with the console.

## Paid runs, by hand

- `make eval-chat` plays the conversation cases in `backend/evals/conversations/cases.jsonl` against the agent API, and the console in `../llmops-console` scores them. The agent API must run with `EVAL_ROUTES_ENABLED=true`.
- `make load-smoke` runs 20 chat sessions for five minutes with locust and prints p50 and p95 per turn.

Both use the real gateway and a real model.

## Known gaps

- `make e2e` resets the database, seeds it and ingests the demo policies through a stub gateway, so it needs no real model. Two browser flows still expect older behaviour, so they will not pass until their tests change. The offboard flow expects a change to run at once, but it now waits for a "yes". The kill switch flow expects an error alert, but a switched-off tool now comes back as a reply.
- `make eval-chat` runs the same reset through the real gateway, so the console must be up first.
