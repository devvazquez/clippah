# clipper - detector automatico de momentos en VODs
#
#   make setup     instala backend (venv) y frontend (npm)
#   make dev       levanta backend :8000 y frontend :3000
#   make check     ruff check + tsc --noEmit
#   make export    interfaz estatica en frontend/out (para subirla a un hosting)

SHELL := /bin/bash
PY     := python3
VENV   := backend/.venv
PYBIN  := $(VENV)/bin
BACKEND_PORT ?= 8000
FRONTEND_PORT ?= 3000

.DEFAULT_GOAL := help
.PHONY: help setup setup-backend setup-frontend setup-font setup-local dev backend backend-keep frontend export check check-queue drain lint typecheck fmt clean clean-data doctor doctor-backend

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: setup-backend setup-frontend setup-emoji setup-font ## Instala todas las dependencias

setup-backend: ## Crea el venv del backend e instala dependencias
	$(PY) -m venv $(VENV)
	$(PYBIN)/pip install --upgrade pip
	$(PYBIN)/pip install -e "backend[dev]"
	@test -f backend/.env || cp backend/.env.example backend/.env
	@echo "backend listo. Edita backend/.env si tienes API keys (opcional)."

setup-local: ## Anade faster-whisper para transcribir en local sin API keys
	$(PYBIN)/pip install -e "backend[local]"

setup-emoji: ## Baja el artwork de emojis de Apple para los titulos de los clips
	@tmp=$$(mktemp -d); cd $$tmp && npm install --silent --no-audit --no-fund emoji-datasource-apple; \
	 mkdir -p $(CURDIR)/backend/assets/emoji; \
	 rm -rf $(CURDIR)/backend/assets/emoji/apple; \
	 cp -r $$tmp/node_modules/emoji-datasource-apple/img/apple/64 $(CURDIR)/backend/assets/emoji/apple; \
	 rm -rf $$tmp; \
	 echo "$$(ls $(CURDIR)/backend/assets/emoji/apple | wc -l) emojis instalados"

setup-font: ## Baja las dos fuentes del clip: Montserrat (titulo) y Barlow (subtitulos)
	@mkdir -p backend/assets/fonts
	@curl -fsSL -o backend/assets/fonts/Montserrat-Bold.ttf \
	  https://raw.githubusercontent.com/JulietaUla/Montserrat/master/fonts/ttf/Montserrat-Bold.ttf
	@curl -fsSL -o backend/assets/fonts/Barlow-Bold.ttf \
	  https://raw.githubusercontent.com/google/fonts/main/ofl/barlow/Barlow-Bold.ttf
	@echo "fuentes instaladas: $$(ls backend/assets/fonts | tr '\n' ' ')"

setup-frontend: ## Instala las dependencias del frontend
	cd frontend && npm install

dev: ## Levanta backend (:8000) y frontend (:3000) con un solo comando
	@$(MAKE) --no-print-directory doctor
	@echo "backend  -> http://127.0.0.1:$(BACKEND_PORT)"
	@echo "frontend -> http://localhost:$(FRONTEND_PORT)"
	@echo "Ctrl+C para parar los dos"
	@# Ctrl+C ya llega a los dos hijos por el grupo de procesos: este shell lo ignora y
	@# solo espera. Reenviarselo otra vez hace que uvicorn --reload muera con segfault.
	@back=; front=; \
	 trap 'kill $$back $$front 2>/dev/null' EXIT TERM; \
	 trap "" INT; \
	 ( cd backend && ../$(PYBIN)/python -m uvicorn app.main:app --reload --port $(BACKEND_PORT) ) & back=$$!; \
	 ( cd frontend && npm run dev -- --port $(FRONTEND_PORT) ) & front=$$!; \
	 wait $$back $$front; code=$$?; \
	 if [ $$code -eq 130 ] || [ $$code -eq 2 ]; then exit 0; fi; exit $$code

backend: ## Solo el backend
	cd backend && ../$(PYBIN)/python -m uvicorn app.main:app --reload --port $(BACKEND_PORT)

backend-keep: ## El backend con guardian: si se muere, se vuelve a levantar
	@# En una sandbox el proceso se cae por cosas de fuera (un corte de red contra
	@# Supabase, el matador por memoria) y sin nadie escuchando la cola las ediciones que
	@# se guardan desde la interfaz se quedan esperando en `rerender_queued`. Esto no
	@# arregla la causa, solo hace que no haya que estar mirando.
	@$(MAKE) --no-print-directory doctor-backend
	@echo "backend con guardian -> http://127.0.0.1:$(BACKEND_PORT)  (Ctrl+C para parar)"
	@intentos=0; \
	 while true; do \
	   ( cd backend && ../$(PYBIN)/python -m uvicorn app.main:app --port $(BACKEND_PORT) ); \
	   code=$$?; \
	   if [ $$code -eq 0 ] || [ $$code -eq 130 ]; then echo "backend parado a mano"; exit 0; fi; \
	   intentos=$$((intentos + 1)); \
	   echo "el backend murio (codigo $$code, caida $$intentos): otra vez en 5 s"; \
	   sleep 5; \
	 done

frontend: ## Solo el frontend
	cd frontend && npm run dev -- --port $(FRONTEND_PORT)

export: ## Construye la interfaz estatica en frontend/out
	@test -f frontend/.env.local || { echo "FALTA frontend/.env.local con NEXT_PUBLIC_SUPABASE_URL y NEXT_PUBLIC_SUPABASE_ANON_KEY (copia frontend/.env.example)"; exit 1; }
	cd frontend && npm run build
	@echo "listo: frontend/out (subelo a cualquier hosting estatico)"

check: lint typecheck ## ruff check + tsc --noEmit

check-queue: ## Prueba el worker de la cola contra un Supabase de mentira
	$(PYBIN)/python scripts/check_queue.py

drain: ## Procesa lo que haya en la cola de Supabase y termina (el turno programado)
	@$(MAKE) --no-print-directory doctor-backend
	$(PYBIN)/python scripts/drain.py

lint: ## ruff check del backend y de scripts/
	$(PYBIN)/ruff check --config backend/pyproject.toml backend scripts

typecheck: ## tsc --noEmit del frontend
	cd frontend && npx tsc --noEmit

fmt: ## ruff format + fix
	$(PYBIN)/ruff check --config backend/pyproject.toml --fix backend scripts

doctor: doctor-backend ## Comprueba que ffmpeg/ffprobe y las dependencias estan disponibles
	@test -d frontend/node_modules || { echo "FALTAN dependencias del frontend: ejecuta 'make setup'"; exit 1; }

doctor-backend: ## Solo lo que necesita el motor: ffmpeg, ffprobe y el venv
	@command -v ffmpeg  >/dev/null || { echo "FALTA ffmpeg (apt install ffmpeg / brew install ffmpeg)"; exit 1; }
	@command -v ffprobe >/dev/null || { echo "FALTA ffprobe (viene con ffmpeg)"; exit 1; }
	@test -x $(PYBIN)/python || { echo "FALTA el venv del backend: ejecuta 'make setup-backend'"; exit 1; }

clean: ## Borra caches de build
	rm -rf frontend/.next backend/.ruff_cache
	find backend -name __pycache__ -type d -prune -exec rm -rf {} +

clean-data: ## Borra media, miniaturas y la base de datos
	rm -rf backend/data/media backend/data/thumbs backend/data/clipper.db*
