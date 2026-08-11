PY := backend/.venv/bin

.PHONY: help setup up down logs migrate revision seed run test lint fmt typecheck check clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup:  ## Create the venv, install deps, copy .env
	python3.11 -m venv backend/.venv
	$(PY)/pip install --upgrade pip
	cd backend && .venv/bin/pip install -e ".[dev]"
	@test -f .env || cp .env.example .env

up:  ## Start Postgres + Redis
	docker compose up -d postgres redis

down:  ## Stop all containers
	docker compose down

logs:  ## Tail container logs
	docker compose logs -f

migrate:  ## Apply migrations to the configured database
	cd backend && .venv/bin/alembic upgrade head

revision:  ## Autogenerate a migration: make revision m="add x"
	cd backend && .venv/bin/alembic revision --autogenerate -m "$(m)"

seed:  ## Load a demo agent, listings and leads
	cd backend && .venv/bin/python -m app.scripts.seed

run:  ## Run the API with reload
	cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000

chat:  ## Talk to the conversation engine in the terminal (needs ANTHROPIC_API_KEY)
	cd backend && .venv/bin/python -m app.scripts.chat $(ARGS)

worker:  ## Run the follow-up worker (polls for due nudges)
	cd backend && .venv/bin/python -m app.workers.followup_worker $(ARGS)

worker-once:  ## Single follow-up pass, then exit (for cron)
	cd backend && .venv/bin/python -m app.workers.followup_worker --once

token:  ## Issue a dashboard API token: make token [EMAIL=agent@example.com]
	cd backend && .venv/bin/python -m app.scripts.issue_token $(if $(EMAIL),--email $(EMAIL),)

dashboard:  ## Run the Next.js dashboard on :3000
	cd frontend && npm run dev

dashboard-setup:  ## Install dashboard dependencies
	cd frontend && npm install && npx playwright install chromium

e2e:  ## Playwright tests for the dashboard's critical flows
	cd frontend && npx playwright test

test:  ## Run the test suite
	cd backend && .venv/bin/pytest -q

lint:  ## Lint + format check
	cd backend && .venv/bin/ruff check . && .venv/bin/ruff format --check .

fmt:  ## Auto-fix lint and format
	cd backend && .venv/bin/ruff check --fix . && .venv/bin/ruff format .

typecheck:  ## Run mypy
	cd backend && .venv/bin/mypy app

check: lint typecheck test  ## Everything CI would run (backend)

check-all: check  ## Backend checks plus the dashboard
	cd frontend && npx tsc --noEmit && npm run build && npx playwright test

clean:  ## Remove caches
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf backend/.pytest_cache backend/.mypy_cache backend/.ruff_cache
