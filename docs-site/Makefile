# BlenderMCP docs site — convenience targets for the docker compose stack.
# All commands respect the COMPOSE_PROJECT_NAME / DOMAIN values from .env.

SHELL := /bin/bash
COMPOSE := docker compose

.PHONY: help prod dev down logs build check rebuild clean shell

help: ## Show this help
	@awk 'BEGIN{FS=":.*##"; printf "\nUsage: make <target>\n\nTargets:\n"} \
		/^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}' \
		$(MAKEFILE_LIST)

prod: ## Start the production stack (built static site behind in-container Caddy)
	$(COMPOSE) up -d --build docs
	@echo "→ Site should come up at https://$$(grep '^DOMAIN=' .env | cut -d= -f2)"

dev: ## Start the dev stack (Astro HMR survived by host Caddy)
	$(COMPOSE) --profile dev up -d --build docs-dev
	@echo "→ Dev site at https://$$(grep '^DOMAIN=' .env | cut -d= -f2) (HMR via wss)"

down: ## Stop both stacks
	$(COMPOSE) --profile dev down

logs: ## Tail logs from whichever stack is running
	$(COMPOSE) --profile dev logs -f --tail=200

build: ## Build the production image without starting it
	$(COMPOSE) build docs

check: ## Run `astro check` locally (no Docker)
	ASTRO_TELEMETRY_DISABLED=1 npx astro check

rebuild: down build prod ## Clean rebuild + restart prod

clean: ## Remove build artifacts (dist/, .astro/)
	rm -rf dist .astro
	@echo "→ Cleared dist/ and .astro/ caches"

shell: ## Open a shell in the running prod container
	docker exec -it $$(grep '^COMPOSE_PROJECT_NAME=' .env | cut -d= -f2)-prod sh
