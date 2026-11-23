.DEFAULT_GOAL := help
.PHONY: help setup env lint typecheck test test-integration eval migrate up down logs stats

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-17s %s\n", $$1, $$2}'

setup: env ## Install dependencies and git hooks, create .env
	uv sync --frozen
	uv run pre-commit install

env: ## Create .env from .env.example with random local passwords (never overwrites)
	@test -f .env || uv run --no-project python -c "import secrets,re,pathlib; \
	t=pathlib.Path('.env.example').read_text(); \
	t=re.sub(r'^(POSTGRES_PASSWORD|REDIS_PASSWORD)=$$', lambda m: m.group(0)+secrets.token_urlsafe(24), t, flags=re.M); \
	import os; fd=os.open('.env', os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600); os.write(fd, t.encode()); os.close(fd); \
	print('created .env')"

lint: ## Ruff lint and format check
	uv run ruff check .
	uv run ruff format --check .

typecheck: ## Pyright (strict on core/)
	uv run pyright

test: ## Unit tests with coverage
	uv run pytest --cov --cov-report=term

test-integration: ## Tests against the running stack (make up first)
	uv run pytest -m integration

eval: ## Offline evals (the first eval set arrives in M5b)
	@echo "No eval sets yet. The first one arrives in M5b."

migrate: ## Database migrations (Alembic arrives in M1)
	@echo "No migrations yet. Alembic arrives in M1."

up: env ## Build and start the full stack, wait until healthy
	docker compose up -d --build --wait

down: ## Stop the stack (keeps volumes)
	docker compose down

logs: ## Follow logs
	docker compose logs -f

stats: ## Memory and CPU per container
	docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}'
