.PHONY: help setup run migrate ingest test test-policy select-tests test-adjacent lint typecheck format web-verify verify eval infra-check e2e load-smoke images app-up app-down
# Where `scripts/e2e_accounts.py` writes the seeded principals Playwright signs
# in as. Inside `.cache/`, which is already gitignored, because the file holds
# the seed's demo passwords in plaintext: fake by a project rule, and still not
# something to leave in the working tree.
# Absolute so it means the same file to the reset/accounts scripts (run from
# backend/, where they find backend/.env) and to Playwright (run from web/).
E2E_ACCOUNTS_FILE ?= $(CURDIR)/.cache/e2e-accounts.json
# The tag `make images` builds under. `local` rather than a version: nothing
# here pushes, and the compose app profile builds its own images by name.
IMAGE_TAG ?= local

help:       ## Show all targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
setup:      ## Install dependencies and pre-commit hooks
	uv sync --project backend && uv run --project backend pre-commit install
run:        ## Run the project locally
	docker compose up -d && cd backend && uv run uvicorn helpdesk.agent_api.main:app --reload
migrate:    ## Apply both schemas to the local compose database (hard rule 3)
	cd backend && uv run alembic upgrade head && uv run python -m helpdesk.agent_api.cli
ingest:     ## Ingest one tenant's corpus version from POLICY_DIR: make ingest TENANT=acme VERSION=v1
	cd backend && uv run python ../scripts/ingest.py --tenant $(TENANT) --version $(VERSION)
# Per-test coverage contexts feed `select-tests` on a laptop and slow the suite a
# lot. CI keeps the 80% floor and sets COV_CONTEXT= to skip the recording.
COV_CONTEXT ?= --cov-context=test
# Parallel workers (pytest-xdist), empty for a plain run. For a laptop with
# CPUs to spare: `make test TEST_WORKERS="-n 3"`. At most 3: each worker needs
# its own Redis indexes. CI stays on one; two were slower on its 2-CPU runner.
TEST_WORKERS ?=
test:       ## Run tests with the coverage gate (needs the compose Postgres: docker compose up -d)
	cd backend && COVERAGE_FILE=.coverage-baseline uv run pytest -q --cov=helpdesk \
	  $(COV_CONTEXT) $(TEST_WORKERS) --cov-fail-under=80
	# The repo-level tests live outside the backend project's test paths, so they
	# run as their own pass with the repo root and backend both on the path.
	PYTHONPATH="$(CURDIR):$(CURDIR)/backend" uv run --project backend pytest -q infra/tests
select-tests: ## Print the tests whose recorded coverage touches this change
	@uv run --project backend python scripts/select_tests.py --explain
test-adjacent: ## Run only the tests whose coverage touches this change
	@uv run --project backend python scripts/select_tests.py | tr ' ' '\n' | xargs -I{} sh -c 'cd backend && uv run pytest -q {}'
test-policy: ## Policy engine alone: 100% line AND branch coverage (SPEC §9)
	cd backend && uv run pytest -q \
	  tests/shared/test_policy_risk.py \
	  tests/shared/test_policy_decision.py \
	  tests/shared/test_policy_expiry.py \
	  tests/shared/test_policy_sod.py \
	  tests/shared/test_policy_purity.py \
	  --cov=helpdesk.shared.policy --cov-branch --cov-report=term-missing --cov-fail-under=100
lint:       ## Lint
	uv run --project backend ruff check backend scripts
typecheck:  ## Static types
	cd backend && uv run mypy
format:     ## Auto-format
	uv run --project backend ruff format backend scripts
web-verify: ## The web gate: types, lint, unit tests, production build
	pnpm -C web install --frozen-lockfile && pnpm -C web typecheck && pnpm -C web lint \
	  && pnpm -C web test && pnpm -C web build
verify: format lint typecheck test test-policy web-verify   ## The full gate, in order
e2e:        ## The five SPEC 16 flows in a real browser (manual; needs docker compose up -d)
	PLAYWRIGHT_BROWSERS_PATH=.cache/playwright pnpm -C web exec playwright install chromium
	mkdir -p .cache
	cd backend && uv run python ../scripts/e2e_reset.py
	cd backend && E2E_ACCOUNTS_FILE=$(E2E_ACCOUNTS_FILE) uv run python ../scripts/e2e_accounts.py
	pnpm -C web build
	PLAYWRIGHT_BROWSERS_PATH=.cache/playwright E2E_ACCOUNTS_FILE=$(E2E_ACCOUNTS_FILE) \
	  pnpm -C web exec playwright test
load-smoke: ## 20 chat sessions for 5 minutes against the REAL gateway (manual, costs money)
	uv run --project backend locust -f scripts/load_smoke.py --headless \
	  --users 20 --spawn-rate 2 --run-time 5m \
	  --host $${AGENT_API_URL:-http://localhost:8000}
eval:       ## Project evals: golden set vs backend/evals/baseline.json
	echo 'golden set is run by the companion console: see backend/evals/README.md'
infra-check: ## Validate infrastructure as code (no apply)
	echo 'deployed by the companion console infra'
images:     ## Build the four service images locally: make images TAG=local
	uv run --project backend python scripts/build_images.py --tag $(IMAGE_TAG)
app-up:     ## Backing services plus the four containers (needs .env and the images' build inputs)
	docker compose --profile app up -d --build
app-down:   ## Stop the app profile, leaving the backing services and their volumes
	docker compose --profile app down
