.PHONY: env build up down reset logs test lint e2e ps deploy

# Deno for tests and checks runs in a container, so it needs no local install.
DENO_IMAGE := denoland/deno:2.5.6
DENO_CACHE := $(HOME)/.cache/calendar-sync-deno
DENO := docker run --rm --network host -u $$(id -u):$$(id -g) -e DENO_DIR=/deno-dir \
	-v $(DENO_CACHE):/deno-dir -v $(CURDIR):/w -w /w/supabase/functions $(DENO_IMAGE) deno
SECRET = $$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
FERNET_KEY = $$(python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')

# Create .env (compose) and supabase/functions/.env (Edge Functions) from the examples,
# the latter with freshly generated secrets.
env:
	@test -f .env && echo ".env already exists; not overwriting" || (cp .env.example .env && echo "Wrote .env")
	@test -f supabase/functions/.env && echo "supabase/functions/.env already exists; not overwriting" || ( \
		cp supabase/functions/.env.example supabase/functions/.env && \
		sed -i.bak -e "s|^SESSION_SECRET=.*|SESSION_SECRET=$(SECRET)|" \
			-e "s|^DATA_ENCRYPTION_KEYS=.*|DATA_ENCRYPTION_KEYS=$(FERNET_KEY)|" supabase/functions/.env && \
		rm -f supabase/functions/.env.bak && echo "Wrote supabase/functions/.env" )

# The web and mock images. Plain `docker build --network host` rather than
# `docker compose build`: on some WSL setups BuildKit's default build network has no DNS.
build:
	set -a; [ -f .env ] && . ./.env; set +a; \
	docker build --network host -t calendar-sync-mockgraph:local mockgraph && \
	docker build --network host -t calendar-sync-web:local \
		--build-arg SUPABASE_FUNCTIONS_URL=$${SUPABASE_FUNCTIONS_URL:-http://host.docker.internal:54321/functions/v1} \
		frontend

# Local Supabase (database, Edge Functions, pg_cron) + the web app and the mock.
up: env build
	supabase start
	docker compose up -d

down:
	docker compose down
	supabase stop

ps:
	supabase status
	docker compose ps

logs:
	docker logs -f supabase_edge_runtime_calendar-sync

# Integration tests run in-process against the local Supabase Postgres (database
# `calsync_test`) and a throwaway mock on :8002. Needs `supabase start`.
test:
	mkdir -p $(DENO_CACHE)
	docker build --network host -q -t calendar-sync-mockgraph:local mockgraph
	docker compose --profile test up -d mockgraph-test
	$(DENO) test --allow-net --allow-env --allow-read tests/
	cd frontend && npm run typecheck && npm run lint

lint:
	mkdir -p $(DENO_CACHE)
	$(DENO) lint
	$(DENO) fmt --check
	$(DENO) check api/index.ts auth/index.ts webhooks/index.ts cron/index.ts
	docker run --rm -v $(CURDIR)/mockgraph:/app -w /app calendar-sync-mockgraph:local \
		sh -c "ruff format --no-cache --check . && ruff check --no-cache ."

# Live end-to-end check of a *fresh* stack against the mock (see mockgraph/scripts/e2e_smoke.py).
e2e:
	docker run --rm --network host calendar-sync-mockgraph:local python scripts/e2e_smoke.py

# Wipe all local data (database, mock tenants) and start again, with demo events.
reset: build
	docker compose down -v
	supabase db reset
	docker compose up -d
	@until curl -sf -o /dev/null http://localhost:$${MOCKGRAPH_PORT:-8001}/mock/state; do sleep 1; done
	curl -s -X POST -H 'content-type: application/json' -d '{"demo_events": true}' \
		http://localhost:$${MOCKGRAPH_PORT:-8001}/mock/api/reset

# Push migrations and deploy every function to the linked hosted project
# (`supabase link --project-ref <ref>` first; see README).
deploy:
	supabase db push
	supabase functions deploy --use-api
