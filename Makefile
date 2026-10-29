.PHONY: help setup run up down rebuild all agent-api mcp-server scheduler web migrate ingest test test-fast test-slowest test-policy lint typecheck format web-verify security verify infra-check e2e load-smoke eval-chat images app-up app-down
# Where `backend/scripts/e2e_accounts.py` writes the seeded principals Playwright signs
# in as. Inside `.cache/`, which is already gitignored, because the file holds
# the seed's demo passwords in plaintext: fake, and still not
# something to leave in the working tree.
# Absolute so it means the same file to the reset/accounts scripts (run from
# backend/, where they find backend/.env) and to Playwright (run from web/).
E2E_ACCOUNTS_FILE ?= $(CURDIR)/.cache/e2e-accounts.json
# The companion console, whose `ops-eval` scores the conversation cases and holds the only baseline.
CONSOLE_DIR ?= ../llmops-console
# The agent-api the eval calls; it must run with EVAL_ROUTES_ENABLED=true.
AGENT_API_URL ?= http://localhost:8000
# The tag `make images` builds under. `local` rather than a version: nothing
# here pushes, and the compose app profile builds its own images by name.
IMAGE_TAG ?= local

help:       ## Show all targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
setup:      ## Install dependencies and pre-commit hooks
	uv sync --project backend && uv run --project backend pre-commit install
run:        ## Run the project locally
	docker compose up -d && cd backend && uv run uvicorn helpdesk.agent_api.api.router:app --reload
migrate:    ## Apply both schemas to the local compose database
	cd backend && uv run alembic upgrade head && uv run python -m helpdesk.agent_api.cli
ingest:     ## Ingest one tenant's corpus version from POLICY_DIR: make ingest TENANT=acme VERSION=v1
	cd backend && uv run python -m helpdesk.mcp_server.cli --tenant $(TENANT) --version $(VERSION)
# Parallel workers (pytest-xdist), empty for a plain run. For a laptop with
# CPUs to spare: `make test TEST_WORKERS="-n 3"`. At most 3: each worker needs
# its own Redis indexes. CI stays on one; two were slower on its 2-CPU runner.
TEST_WORKERS ?=
test:       ## Run tests with the coverage gate (needs the compose Postgres: docker compose up -d)
	cd backend && uv run pytest -q --cov=helpdesk \
	  $(TEST_WORKERS) --cov-fail-under=80
	# The repo-level tests live outside the backend project's test paths, so they
	# run as their own pass with the repo root and backend both on the path.
	PYTHONPATH="$(CURDIR):$(CURDIR)/backend" uv run --project backend pytest -q infra/tests
# The everyday run: everything NOT marked slow, on 3 parallel workers. 3 is the
# cap each worker's own Redis indexes allow. Slow tests, the whole
# suite and the coverage gate are `make test`, `make verify` and CI.
test-fast:  ## Everyday run: tests not marked slow, on 3 workers
	cd backend && uv run pytest -q -m "not slow" -n 3
# One serial run that prints the 30 slowest tests and their fixture setup. No
# -n and no --cov, so workers and coverage tracing do not bend the numbers
#. This is the reading that decides which tests get the slow marker.
test-slowest: ## Print the 30 slowest tests and fixtures, serial, no coverage
	cd backend && uv run pytest --durations=30
test-policy: ## Policy engine alone: 100% line AND branch coverage
	cd backend && uv run pytest -q \
	  tests/shared/test_policy_risk.py \
	  tests/shared/test_policy_decision.py \
	  tests/shared/test_policy_expiry.py \
	  tests/shared/test_policy_sod.py \
	  tests/shared/test_policy_purity.py \
	  --cov=helpdesk.shared.policy --cov-branch --cov-report=term-missing --cov-fail-under=100
lint:       ## Lint
	# .github/scripts, infra/scripts, infra/load and infra/tests sit outside backend/, where the one ruff config
	# lives, so --config holds them to the same rule set as backend, not ruff's default.
	uv run --project backend ruff check --config backend/pyproject.toml backend .github/scripts infra/scripts infra/load infra/tests
typecheck:  ## Static types
	cd backend && uv run mypy
format:     ## Auto-format
	uv run --project backend ruff format --config backend/pyproject.toml backend .github/scripts infra/scripts infra/load infra/tests
web-verify: ## The web gate: types, lint, unit tests, production build
	pnpm -C web install --frozen-lockfile && pnpm -C web typecheck && pnpm -C web lint \
	  && pnpm -C web test && pnpm -C web build
# The four security scans CI's `security` job runs, moved here so a leak or an
# advisory shows on the laptop before a push. Needs Docker and the
# network. Pinned by infra/tests/test_security_config.py.
#
# Secrets over the full history. --baseline-path excuses the nine findings that
# live only in history: the renamed test constants, the web idempotency_key and
# .env.example's placeholder, each against a commit no rename can reach. A
# baseline entry is matched on every field including Commit, so a secret in a
# NEW commit is never covered by it. Pinned by
# test_the_gitleaks_step_passes_a_baseline_path.
# --redact is deliberately absent: it rewrites Match and Secret to "REDACTED",
# and gitleaks compares both against the baseline, so the two flags cannot both
# be on. Every baselined value is a fake test fixture or a placeholder, so
# printing them costs nothing; a real secret would be a NEW finding and must be
# readable to be fixed.
#
# Python advisories. --strict fails on an advisory with a fix available, which
# is the only kind Dependabot can act on. No --ignore-vuln flag exists today; one
# is added only with a comment carrying the reason and the date.
# --no-emit-workspace drops the local editable lines, which pip-audit cannot
# hash and which are this repo's own code anyway.
# --disable-pip reads the exported versions as they are, hashes and all, instead
# of letting pip solve them again: pip does not see uv's pillow override in
# backend/pyproject.toml, so it would refuse the very set the lock file holds.
#
# Compose configuration. Both compose files, because the override example is
# copied verbatim by anyone running the stack locally. infra/.trivyignore is
# empty at HEAD.
security:   ## Secret scan, Python and web advisories, compose config scan (needs Docker)
	docker run --rm -v "$(CURDIR):/repo" zricethezav/gitleaks:v8.24.3 \
	  detect --source=/repo --config=/repo/.gitleaks.toml --no-banner \
	  --baseline-path=/repo/.gitleaks-baseline.json
	cd backend && uv export --frozen --no-emit-project --no-emit-workspace --format requirements-txt \
	  --output-file requirements-audit.txt && uvx pip-audit --strict --disable-pip -r requirements-audit.txt
	cd web && pnpm audit --audit-level high
	docker run --rm -v "$(CURDIR):/work" -w /work aquasec/trivy:0.58.1 \
	  config --severity HIGH,CRITICAL --exit-code 1 --ignorefile infra/.trivyignore \
	  docker-compose.yml
	docker run --rm -v "$(CURDIR):/work" -w /work aquasec/trivy:0.58.1 \
	  config --severity HIGH,CRITICAL --exit-code 1 --ignorefile infra/.trivyignore \
	  docker-compose.override.example.yml
verify: format lint typecheck test test-policy web-verify security   ## The full gate, in order
e2e:        ## The five end-to-end flows in a real browser (manual; needs docker compose up -d)
	PLAYWRIGHT_BROWSERS_PATH=.cache/playwright pnpm -C web exec playwright install chromium
	mkdir -p .cache
	cd backend && uv run python scripts/e2e_reset.py
	cd backend && E2E_ACCOUNTS_FILE=$(E2E_ACCOUNTS_FILE) uv run python scripts/e2e_accounts.py
	pnpm -C web build
	PLAYWRIGHT_BROWSERS_PATH=.cache/playwright E2E_ACCOUNTS_FILE=$(E2E_ACCOUNTS_FILE) \
	  pnpm -C web exec playwright test
load-smoke: ## 20 chat sessions for 5 minutes against the REAL gateway (manual, costs money)
	uv run --project backend locust -f infra/load/load_smoke.py --headless \
	  --users 20 --spawn-rate 2 --run-time 5m \
	  --host $${AGENT_API_URL:-http://localhost:8000}
eval-chat:  ## Conversation evals: the 33 cases played here, scored by the console (manual, costs money)
	cd backend && uv run python scripts/e2e_reset.py
	cd backend && uv run python scripts/seed.py
	cd $(CONSOLE_DIR) && uv run --project backend ops-eval conversations --target $(AGENT_API_URL) \
	  --cases $(CURDIR)/backend/evals/conversations/cases.jsonl
infra-check: ## Validate infrastructure as code (no apply)
	echo 'deployed by the companion console infra'
images:     ## Build the four service images locally: make images TAG=local
	uv run --project backend python infra/scripts/build_images.py --tag $(IMAGE_TAG)
up:         ## Start everything in Docker: backing services and the four app containers (same as app-up)
	$(MAKE) app-up
# `make rebuild` or `make rebuild all`: rebuild and restart every app container,
# never the databases. `make rebuild agent-api` (or mcp-server, scheduler, web)
# does just that one. The words after `rebuild` are empty targets so make accepts them.
REBUILD_WHAT = $(filter-out rebuild,$(MAKECMDGOALS))
rebuild:    ## Rebuild app containers, never the databases: make rebuild [all|agent-api|mcp-server|scheduler|web]
	docker compose --profile app up -d --build --no-deps $(if $(filter-out all,$(REBUILD_WHAT)),$(addprefix helpdesk-,$(filter-out all,$(REBUILD_WHAT))),helpdesk-agent-api helpdesk-mcp-server helpdesk-scheduler helpdesk-web)
all agent-api mcp-server scheduler web:
	@:
down:       ## Stop the whole Docker stack, keeping its data (same as app-down)
	$(MAKE) app-down
app-up:     ## Backing services plus the four containers (needs .env and the images' build inputs)
	docker compose --profile app up -d --build
app-down:   ## Stop the app profile, leaving the backing services and their volumes
	docker compose --profile app down
