.DEFAULT_GOAL := help
COMPOSE := docker compose
SVC     := blender-onboarding
-include .env
export

help: ## list targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

pull: ## pull the image version pinned in .env
	$(COMPOSE) pull

up: ## start (re-picking VNC ports if another instance took them) and tail the boot log
	@if command -v blender-instance >/dev/null; then blender-instance up .; else $(COMPOSE) up -d; fi
	@sleep 3; $(COMPOSE) logs --tail=40 $(SVC)

down: ## stop and remove the container (config volume is kept)
	$(COMPOSE) down

restart: ## restart the container
	$(COMPOSE) restart $(SVC)
	@sleep 3; $(COMPOSE) logs --tail=40 $(SVC)

logs: ## follow logs
	$(COMPOSE) logs -f $(SVC)

shell: ## bash inside the running container
	$(COMPOSE) exec $(SVC) bash

info: ## print how to connect
	@echo "instance: $(INSTANCE)"
	@echo "VNC:      $(VNC_BIND):$(VNC_PORT)  password: $(VNC_PASSWORD)"
	@test -z "$(VNC_VIEWONLY_PASSWORD)" || echo "          view-only password: $(VNC_VIEWONLY_PASSWORD)"
	@echo "noVNC:    https://$(WEB_HOSTNAME)"
	@test "$(VNC_WATCH_ENABLED)" != 1 || { echo "watch:    https://$(WATCH_HOSTNAME)   (no password, view only)"; echo "          vnc://127.0.0.1:$(VNC_WATCH_PORT)"; }

gpu-check: ## GPU visible? nvidia-smi + Cycles device list
	$(COMPOSE) exec $(SVC) nvidia-smi --query-gpu=name,driver_version,memory.used,memory.total --format=csv
	$(COMPOSE) exec $(SVC) blender -b --python-expr \
	  'import bpy;p=bpy.context.preferences.addons["cycles"].preferences;p.get_devices();[print(f"{d.type:6} {d.name:40} use={d.use}") for d in p.devices]' 2>/dev/null | grep -E '^(CPU|CUDA|OPTIX)'

clean: ## remove container and the config volume (projects/ is untouched)
	$(COMPOSE) down -v

.PHONY: help pull up down restart logs shell info gpu-check clean
