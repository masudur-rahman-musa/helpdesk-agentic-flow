# Run it on your laptop

Everything runs on one machine. The only paid part is the model behind the
chat, reached through the companion
[llmops-console](https://github.com/masudur-rahman-musa/llmops-console).

## What you need

- Docker with Compose v2.
- git and openssl.
- [uv](https://docs.astral.sh/uv/). It installs Python 3.12 for you.
- Node 22 and pnpm 10, only for the web checks (`make web-verify`).

## 1. Clone

```bash
git clone https://github.com/masudur-rahman-musa/helpdesk-agentic-flow
cd helpdesk-agentic-flow
```

## 2. Make the local env file

The apps read `backend/.env.demo` first, then `backend/.env` over it. Both are
gitignored. `backend/.env.example` lists every setting. Copy it without the two
placeholders, then add a login secret of at least 32 bytes.

```bash
cd backend
grep -v -e '^JWT_SECRET=' -e '^GATEWAY_API_KEYS=' .env.example > .env.demo
echo "JWT_SECRET=$(openssl rand -hex 32)" >> .env.demo
cd ..
```

## 3. Start the backing services

```bash
docker compose up -d --wait
```

## 4. Create the schema and the demo data

```bash
make migrate
(cd backend && uv run python scripts/seed.py)
```

The seed writes two made-up companies, `nordlicht` and `cuan`. It is safe to run again.

## 5. Start the app

```bash
make up
```

Open [localhost:3000](http://localhost:3000). Sign in with a tenant, an email and
a password from [demo-accounts.md](demo-accounts.md). Everything except the chat
works now. The agent API runs on port 8000, and its `/readyz` answers 503 until
a gateway answers.

## 6. Connect the model gateway

The chat and the policy ingest go through the gateway in llmops-console, at
`http://localhost:8080/v1`. The gateway checks one key per tenant. This repo
keeps the key, and the console keeps only its sha256 digest. Make one pair per tenant.

```bash
nord=$(openssl rand -hex 32)
cuan=$(openssl rand -hex 32)

# For this repo.
echo "GATEWAY_API_KEYS=nordlicht:$nord,cuan:$cuan" >> backend/.env.demo

# For the console. Copy this line into its backend env file.
echo "OPSCONSOLE_API_KEYS=nordlicht:$(printf %s "$nord" | shasum -a 256 | cut -d' ' -f1),cuan:$(printf %s "$cuan" | shasum -a 256 | cut -d' ' -f1)"
```

Start the console as its README says, with a model provider key in its
`backend/.env`. Then restart the containers here so they read the new keys.

```bash
make rebuild
```

`curl http://localhost:8000/readyz` should now answer 200.

## 7. Load the policy documents

```bash
make ingest TENANT=nordlicht VERSION=v2
make ingest TENANT=cuan VERSION=v2
```

## Run the tests

Keep `docker compose up -d` running. No test calls a real model.
`make test-fast` is the everyday run. `make verify` runs everything CI runs.
`make e2e` runs the Playwright flows against a stub gateway, so it costs nothing.
`make eval-chat` and `make load-smoke` use the real gateway and cost money.
`make down` stops the stack and keeps your data. `make help` lists every target.

Tracing to Langfuse is optional. Its `LANGFUSE_*` keys come from your own
Langfuse project and go in `backend/.env`.
