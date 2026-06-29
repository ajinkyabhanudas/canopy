.PHONY: help test lint check build run smoke ui eval eval-spanish eval-adversarial clean

# Default target
help:
	@echo ""
	@echo "canopy — available commands"
	@echo ""
	@echo "  Development"
	@echo "  make lint              Lint src/ and tests/ with ruff"
	@echo "  make test              Run unit tests (no DB or API key needed)"
	@echo "  make check             lint + test"
	@echo "  make ui                Start the app locally (needs .env)"
	@echo ""
	@echo "  Docker"
	@echo "  make build             Build the Docker image (canopy:dev)"
	@echo "  make run               Build and run in Docker (needs .env)"
	@echo "  make smoke             Build image and run Docker smoke test"
	@echo ""
	@echo "  Eval (needs live DB + ANTHROPIC_API_KEY)"
	@echo "  make eval              Ground-truth + adversarial eval suites"
	@echo "  make eval-gt           Ground-truth suite only"
	@echo "  make eval-es           Spanish language variants"
	@echo "  make eval-adv          Adversarial suite only"
	@echo ""
	@echo "  Housekeeping"
	@echo "  make clean             Remove build artefacts and caches"
	@echo ""

# ── Development ──────────────────────────────────────────────────────────────

lint:
	ruff check src/ tests/ scripts/

test:
	pytest tests/ -q

check: lint test

ui:
	python scripts/run_ui.py

# ── Docker ───────────────────────────────────────────────────────────────────

build:
	docker build -t canopy:dev .

run: build
	./scripts/docker_run.sh

smoke:
	./scripts/smoke_test_docker.sh

# ── Eval ─────────────────────────────────────────────────────────────────────

eval:
	python scripts/run_eval.py

eval-gt:
	python scripts/run_eval.py --ground-truth

eval-es:
	python scripts/run_eval.py --spanish

eval-adv:
	python scripts/run_eval.py --adversarial

# ── Housekeeping ─────────────────────────────────────────────────────────────

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .coverage htmlcov/ .pytest_cache/ dist/ build/ *.egg-info/
	@echo "Clean."
