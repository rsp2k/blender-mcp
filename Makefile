# blender-mcp — convenience targets for the docker compose stack.
# All commands respect COMPOSE_PROJECT_NAME / DOMAIN values from .env.

SHELL := /bin/bash
COMPOSE := docker compose

.PHONY: help prod dev down logs restart build rebuild ps shell caddy-reload secret-gen health clean

help: ## Show this help
	@awk 'BEGIN{FS=":.*##"; printf "\nUsage: make <target>\n\nTargets:\n"} \
		/^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}' \
		$(MAKEFILE_LIST)

prod: ## Start the production stack (FastMCP server behind caddy-docker-proxy)
	$(COMPOSE) up -d --build blender-mcp
	@echo "-> Server should come up at https://$$(grep '^DOMAIN=' .env | cut -d= -f2)/mcp"

dev: ## Start the dev stack with hot reload
	$(COMPOSE) --profile dev up -d --build blender-mcp-dev
	@echo "-> Dev server at https://$$(grep '^DOMAIN=' .env | cut -d= -f2)/mcp"

# --profile dev so profile-gated services are also torn down, not just the default service
down: ## Stop both stacks (prod + dev)
	$(COMPOSE) --profile dev down

logs: ## Tail logs from whichever stack is running
	$(COMPOSE) --profile dev logs -f --tail=200

restart: ## Graceful restart without rebuild (prod, falls back to dev if running)
	@if docker ps --format '{{.Names}}' | grep -q "blender-mcp-dev$$"; then \
		$(COMPOSE) --profile dev restart blender-mcp-dev; \
	else \
		$(COMPOSE) restart blender-mcp; \
	fi

build: ## Build the production image without starting it
	$(COMPOSE) build blender-mcp

rebuild: ## Full no-cache rebuild of prod image
	$(COMPOSE) build --no-cache blender-mcp

ps: ## Show running services
	$(COMPOSE) ps

shell: ## Open an interactive bash shell in the running prod container
	$(COMPOSE) exec blender-mcp /bin/bash

# caddy-docker-proxy normally re-renders on label changes automatically;
# this is the escape hatch for the rare case where it doesn't pick up.
caddy-reload: ## Force the host Caddy to re-read its config
	docker exec $$(docker ps -q -f name=caddy) caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile

# IDEMPOTENT: never overwrites an existing OAUTH_SECRET_KEY. Regenerating
# would invalidate every live JWT signed by the previous key, which is a
# silent way to lock every authenticated client out.
secret-gen: ## Generate OAUTH_SECRET_KEY into .env only if missing/blank
	@ENV_FILE=$${ENV_FILE:-.env}; \
	if [ ! -f "$$ENV_FILE" ]; then \
		if [ -f .env.example ]; then \
			cp .env.example "$$ENV_FILE"; \
			echo "-> Created $$ENV_FILE from .env.example"; \
		else \
			echo "OAUTH_SECRET_KEY=" > "$$ENV_FILE"; \
			echo "-> Created empty $$ENV_FILE (no .env.example present)"; \
		fi; \
	fi; \
	CURRENT=$$(grep -E '^OAUTH_SECRET_KEY=' "$$ENV_FILE" | head -1 | cut -d= -f2-); \
	if [ -n "$$CURRENT" ]; then \
		echo "-> OAUTH_SECRET_KEY already set in $$ENV_FILE; leaving it alone"; \
	else \
		NEW_KEY=$$(python3 -c "import secrets; print(secrets.token_urlsafe(48))"); \
		if grep -qE '^OAUTH_SECRET_KEY=' "$$ENV_FILE"; then \
			sed -i "s|^OAUTH_SECRET_KEY=.*|OAUTH_SECRET_KEY=$$NEW_KEY|" "$$ENV_FILE"; \
		else \
			echo "OAUTH_SECRET_KEY=$$NEW_KEY" >> "$$ENV_FILE"; \
		fi; \
		echo "-> Generated OAUTH_SECRET_KEY into $$ENV_FILE"; \
	fi

health: ## Hit the public /health endpoint and pretty-print the response
	@curl -fsS "https://$$(grep '^DOMAIN=' .env | cut -d= -f2)/health" | \
		(command -v jq >/dev/null && jq . || cat)

# Only removes this project's volumes; the shared caddy network is untouched.
clean: ## Stop everything and remove project volumes
	$(COMPOSE) --profile dev down -v --remove-orphans
