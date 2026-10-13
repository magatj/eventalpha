# Optional (Git Bash / WSL). On Windows PowerShell use scripts\eventalpha.ps1 instead.
TEST_DB=postgresql+psycopg://eventalpha:eventalpha_local_dev@db:5432/eventalpha_test

.PHONY: setup start stop migrate test reset-demo seed-demo frontend logs tf-validate

setup:
	test -f .env || cp .env.example .env
	docker compose build
	cd frontend && npm install --no-audit --no-fund

start:
	docker compose up -d --build db migrate api worker scheduler
	cd frontend && npm run dev

frontend:
	cd frontend && npm run dev

stop:
	docker compose --profile frontend down

migrate:
	docker compose run --rm migrate

test:
	docker compose up -d db
	docker compose run --rm --no-deps -e TEST_DATABASE_URL=$(TEST_DB) api sh -c "ruff check app tests && pytest -q"
	cd frontend && npm run lint && npm run typecheck && npm test

reset-demo:
	curl -s -X POST http://localhost:8000/api/demo/reset

seed-demo:
	curl -s -X PUT http://localhost:8000/api/settings -H 'content-type: application/json' -d '{"data_mode":"demo"}'
	curl -s -X POST http://localhost:8000/api/discovery/run

logs:
	docker compose logs -f --tail 100

tf-validate:
	MSYS_NO_PATHCONV=1 docker run --rm -v "$(CURDIR)/infra/terraform:/tf" hashicorp/terraform:1.10.5 -chdir=/tf fmt -recursive -check
	for e in dev prod; do \
	  MSYS_NO_PATHCONV=1 docker run --rm -v "$(CURDIR)/infra/terraform:/tf" hashicorp/terraform:1.10.5 -chdir=/tf/environments/$$e init -backend=false -input=false >/dev/null && \
	  MSYS_NO_PATHCONV=1 docker run --rm -v "$(CURDIR)/infra/terraform:/tf" hashicorp/terraform:1.10.5 -chdir=/tf/environments/$$e validate; done
