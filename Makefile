.PHONY: help setup run migrate ingest test test-policy select-tests test-adjacent lint typecheck format web-verify verify eval infra-check e2e load-smoke images app-up app-down
# Where `scripts/e2e_accounts.py` writes the seeded principals Playwright signs
# in as. Inside `.cache/`, which is already gitignored, because the file holds
# the seed's demo passwords in plaintext: fake by a project rule, and still not
# something to leave in the working tree.
E2E_ACCOUNTS_FILE ?= .cache/e2e-accounts.json
# The tag `make images` builds under. `local` rather than a version: nothing
# here pushes, and the compose app profile builds its own images by name.
IMAGE_TAG ?= local

help:       ## Show all targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
setup:      ## Install dependencies and pre-commit hooks
	uv sync && uv run pre-commit install
run:        ## Run the project locally
	docker compose up -d && uv run uvicorn agent_api.main:app --reload
migrate:    ## Apply both schemas to the local compose database (hard rule 3)
	uv run alembic upgrade head && uv run python scripts/checkpointer_setup.py
ingest:     ## Ingest one tenant's corpus version from POLICY_DIR: make ingest TENANT=acme VERSION=v1
	uv run python scripts/ingest.py --tenant $(TENANT) --version $(VERSION)
test:       ## Run tests with the coverage gate (needs the compose Postgres: docker compose up -d)
	COVERAGE_FILE=.coverage-baseline uv run pytest -q --cov=helpdesk --cov=agent_api --cov=mcp_server \
	  --cov-context=test --cov-fail-under=80
select-tests: ## Print the tests whose recorded coverage touches this change
	@uv run python scripts/select_tests.py --explain
test-adjacent: ## Run only the tests whose coverage touches this change
	@uv run python scripts/select_tests.py | tr ' ' '\n' | xargs uv run pytest -q
test-policy: ## Policy engine alone: 100% line AND branch coverage (SPEC §9)
	uv run pytest -q \
	  packages/helpdesk/tests/test_policy_risk.py \
	  packages/helpdesk/tests/test_policy_decision.py \
	  packages/helpdesk/tests/test_policy_expiry.py \
	  packages/helpdesk/tests/test_policy_sod.py \
	  packages/helpdesk/tests/test_policy_purity.py \
	  --cov=helpdesk.policy --cov-branch --cov-report=term-missing --cov-fail-under=100
lint:       ## Lint
	uv run ruff check .
typecheck:  ## Static types
	uv run mypy
format:     ## Auto-format
	uv run ruff format .
web-verify: ## The web gate: types, lint, unit tests, production build
	pnpm -C web install --frozen-lockfile && pnpm -C web typecheck && pnpm -C web lint \
	  && pnpm -C web test && pnpm -C web build
verify: format lint typecheck test test-policy web-verify   ## The full gate, in order
e2e:        ## The five SPEC 16 flows in a real browser (manual; needs docker compose up -d)
	PLAYWRIGHT_BROWSERS_PATH=.cache/playwright pnpm -C web exec playwright install chromium
	mkdir -p .cache
	uv run python scripts/e2e_reset.py
	E2E_ACCOUNTS_FILE=$(E2E_ACCOUNTS_FILE) uv run python scripts/e2e_accounts.py
	pnpm -C web build
	PLAYWRIGHT_BROWSERS_PATH=.cache/playwright E2E_ACCOUNTS_FILE=$(E2E_ACCOUNTS_FILE) \
	  pnpm -C web exec playwright test
load-smoke: ## 20 chat sessions for 5 minutes against the REAL gateway (manual, costs money)
	uv run locust -f scripts/load_smoke.py --headless \
	  --users 20 --spawn-rate 2 --run-time 5m \
	  --host $${AGENT_API_URL:-http://localhost:8000}
eval:       ## Project evals: golden set vs project-evals/baseline.json
	echo 'golden set is run by the companion console: see project-evals/README.md'
infra-check: ## Validate infrastructure as code (no apply)
	echo 'deployed by the companion console infra'
images:     ## Build the four service images locally: make images TAG=local
	uv run python scripts/build_images.py --tag $(IMAGE_TAG)
app-up:     ## Backing services plus the four containers (needs .env and the images' build inputs)
	docker compose --profile app up -d --build
app-down:   ## Stop the app profile, leaving the backing services and their volumes
	docker compose --profile app down
