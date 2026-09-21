.PHONY: help setup lint test test-unit docker-build docker-up docker-down clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'

setup: ## Create venv hints + install deps (host)
	python -m pip install -r requirements.txt -r requirements-dev.txt
	playwright install chromium
	mkdir -p data/generated reference/model reference/styles
	@test -f .env || cp .env.example .env
	@echo "Fill .env, add reference/model/model.jpg, then save Gemini/Pinterest sessions."

lint: ## Run ruff
	ruff check .

test: ## Run unit tests safe for CI (no live APIs)
	SKIP_LIVE_GENERATION=1 python -m unittest discover -s tests -p 'test_*.py' -v

test-unit: test ## Alias

docker-build: ## Build container image
	docker compose build

docker-up: ## Run bot via compose
	docker compose up -d

docker-down: ## Stop compose stack
	docker compose down

clean: ## Remove caches
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .ruff_cache .pytest_cache
