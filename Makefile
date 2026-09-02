.PHONY: help setup run test lint typecheck format verify eval infra-check
help:       ## Show all targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
setup:      ## Install dependencies and pre-commit hooks
	uv sync && uv run pre-commit install
run:        ## Run the project locally
	docker compose up -d && uv run uvicorn services.agent_api.main:app --reload
test:       ## Run tests with the coverage gate
	uv run pytest -q --cov=helpdesk --cov-fail-under=80
lint:       ## Lint
	uv run ruff check .
typecheck:  ## Static types
	uv run mypy
format:     ## Auto-format
	uv run ruff format .
verify: format lint typecheck test   ## The full gate, in order
eval:       ## Project evals: golden set vs project-evals/baseline.json
	echo 'golden set is run by the companion console: see project-evals/README.md'
infra-check: ## Validate infrastructure as code (no apply)
	echo 'deployed by the companion console infra'
