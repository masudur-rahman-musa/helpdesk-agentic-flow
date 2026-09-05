.PHONY: help setup run test test-policy select-tests test-adjacent lint typecheck format verify eval infra-check
help:       ## Show all targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
setup:      ## Install dependencies and pre-commit hooks
	uv sync && uv run pre-commit install
run:        ## Run the project locally
	docker compose up -d && uv run uvicorn agent_api.main:app --reload
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
verify: format lint typecheck test test-policy   ## The full gate, in order
eval:       ## Project evals: golden set vs project-evals/baseline.json
	echo 'golden set is run by the companion console: see project-evals/README.md'
infra-check: ## Validate infrastructure as code (no apply)
	echo 'deployed by the companion console infra'
