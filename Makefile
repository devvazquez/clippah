# clipper - detector automatico de momentos en VODs
#
#   make setup     instala backend (venv) y frontend (npm)
#   make dev       levanta backend :8000 y frontend :3000
#   make check     ruff check + tsc --noEmit

SHELL := /bin/bash
PY     := python3
VENV   := backend/.venv
PYBIN  := $(VENV)/bin
BACKEND_PORT ?= 8000
FRONTEND_PORT ?= 3000

.DEFAULT_GOAL := help
.PHONY: help setup setup-backend setup-frontend setup-local dev backend frontend check lint typecheck fmt clean clean-data doctor

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: setup-backend setup-frontend ## Instala todas las dependencias

setup-backend: ## Crea el venv del backend e instala dependencias
	$(PY) -m venv $(VENV)
	$(PYBIN)/pip install --upgrade pip
	$(PYBIN)/pip install -e "backend[dev]"
	@test -f backend/.env || cp backend/.env.example backend/.env
	@echo "backend listo. Edita backend/.env si tienes API keys (opcional)."

setup-local: ## Anade faster-whisper para transcribir en local sin API keys
	$(PYBIN)/pip install -e "backend[local]"

setup-frontend: ## Instala las dependencias del frontend
	cd frontend && npm install

dev: ## Levanta backend (:8000) y frontend (:3000) con un solo comando
	@$(MAKE) --no-print-directory doctor
	@echo "backend  -> http://127.0.0.1:$(BACKEND_PORT)"
	@echo "frontend -> http://localhost:$(FRONTEND_PORT)"
	@trap 'kill 0' EXIT INT TERM; \
	 ( cd backend && ../$(PYBIN)/python -m uvicorn app.main:app --reload --port $(BACKEND_PORT) ) & \
	 ( cd frontend && npm run dev -- --port $(FRONTEND_PORT) ) & \
	 wait

backend: ## Solo el backend
	cd backend && ../$(PYBIN)/python -m uvicorn app.main:app --reload --port $(BACKEND_PORT)

frontend: ## Solo el frontend
	cd frontend && npm run dev -- --port $(FRONTEND_PORT)

check: lint typecheck ## ruff check + tsc --noEmit

lint: ## ruff check del backend
	cd backend && ../$(PYBIN)/ruff check .

typecheck: ## tsc --noEmit del frontend
	cd frontend && npx tsc --noEmit

fmt: ## ruff format + fix
	cd backend && ../$(PYBIN)/ruff check --fix . && ../$(PYBIN)/ruff format .

doctor: ## Comprueba que ffmpeg/ffprobe y el venv estan disponibles
	@command -v ffmpeg  >/dev/null || { echo "FALTA ffmpeg (apt install ffmpeg / brew install ffmpeg)"; exit 1; }
	@command -v ffprobe >/dev/null || { echo "FALTA ffprobe (viene con ffmpeg)"; exit 1; }
	@test -x $(PYBIN)/python || { echo "FALTA el venv del backend: ejecuta 'make setup'"; exit 1; }
	@test -d frontend/node_modules || { echo "FALTAN dependencias del frontend: ejecuta 'make setup'"; exit 1; }

clean: ## Borra caches de build
	rm -rf frontend/.next backend/.ruff_cache
	find backend -name __pycache__ -type d -prune -exec rm -rf {} +

clean-data: ## Borra media, miniaturas y la base de datos
	rm -rf backend/data/media backend/data/thumbs backend/data/clipper.db*
