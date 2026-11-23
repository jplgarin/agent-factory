.DEFAULT_GOAL := help
.PHONY: help setup env lint typecheck test test-integration eval migrate up down logs stats

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-17s %s\n", $$1, $$2}'

setup: env ## Install dependencies and git hooks, create .env
	uv sync --frozen
	uv run pre-commit install

env: ## Create .env with random local passwords, or add keys new in .env.example (never overwrites)
	@uv run --no-project python scripts/make_env.py

lint: ## Ruff lint and format check
	uv run ruff check .
	uv run ruff format --check .

typecheck: ## Pyright (strict on core/)
	uv run pyright

test: ## Unit and database tests with coverage (database tests need Docker)
	uv run pytest --cov --cov-report=term
	uv run coverage report --include='core/*' --fail-under=90

test-integration: ## Tests against the running stack (make up first)
	uv run pytest -m integration

eval: ## Offline evals (the first eval set arrives in M5b)
	@echo "No eval sets yet. The first one arrives in M5b."

migrate: env ## Create or update database roles and apply migrations to the local stack
	docker compose up -d --wait postgres
	docker compose run --rm --build db-roles
	docker compose run --rm --build db-migrate

up: env ## Build and start the full stack, wait until healthy
	docker compose up -d --build --wait

down: env ## Stop the stack (keeps volumes)
	docker compose down

logs: env ## Follow logs
	docker compose logs -f

stats: ## Memory and CPU per container
	docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}'
