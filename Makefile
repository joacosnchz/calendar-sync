.PHONY: env up down reset logs test lint migration e2e ps

# Create .env from the example with freshly generated secrets.
env:
	@test -f .env && echo ".env already exists; not overwriting" || ( \
		cp .env.example .env && \
		SECRET=$$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))') && \
		KEY=$$(python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())') && \
		sed -i.bak -e "s|^SESSION_SECRET=.*|SESSION_SECRET=$$SECRET|" -e "s|^DATA_ENCRYPTION_KEYS=.*|DATA_ENCRYPTION_KEYS=$$KEY|" .env && \
		rm -f .env.bak && echo "Wrote .env" )

up: env
	docker compose up -d --build

down:
	docker compose down

ps:
	docker compose ps

logs:
	docker compose logs -f api worker scheduler

# Backend test suite runs inside the backend image against the compose Postgres/Redis.
test:
	docker compose up -d db redis
	docker compose run --rm --no-deps \
		-e TEST_DATABASE_URL=postgresql+asyncpg://calsync:$${POSTGRES_PASSWORD:-calsync}@db:5432/calsync_test \
		-e TEST_REDIS_URL=redis://redis:6379/15 \
		api pytest -q
	cd frontend && npm run typecheck && npm run lint

lint:
	docker compose run --rm --no-deps api sh -c "ruff format --check . && ruff check ."

# Live end-to-end check of a *fresh* stack against the mock (see backend/scripts/e2e_smoke.py).
e2e:
	docker run --rm --network host calendar-sync-backend:local python scripts/e2e_smoke.py

# Wipe all local data (database, queue, mock tenants) and start again.
reset:
	docker compose down -v
	docker compose up -d --build

# Usage: make migration m="add foo"
migration:
	docker compose run --rm --user $$(id -u) -v ./backend:/app migrate alembic revision --autogenerate -m "$(m)"
