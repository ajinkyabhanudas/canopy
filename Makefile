.PHONY: help test lint check build run smoke ui eval eval-spanish eval-adversarial benchmark clean playwright-install e2e screenshots

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
	@echo "  make screenshots       Build image, start container, capture UI screenshots, stop"
	@echo ""
	@echo "  Eval (needs live DB + ANTHROPIC_API_KEY)"
	@echo "  make eval              Ground-truth + adversarial eval suites"
	@echo "  make eval-gt           Ground-truth suite only"
	@echo "  make eval-es           Spanish language variants"
	@echo "  make eval-adv          Adversarial suite only"
	@echo ""
	@echo "  Benchmark (needs live DB + all API keys in .env)"
	@echo "  make benchmark         Run all models from models.yaml, print comparison table"
	@echo ""
	@echo "  E2E browser tests (needs Playwright browsers installed once)"
	@echo "  make playwright-install  Install Chromium for E2E tests (run once)"
	@echo "  make e2e               Run E2E browser tests against a mocked Gradio server"
	@echo ""
	@echo "  Housekeeping"
	@echo "  make clean             Remove build artefacts and caches"
	@echo ""

# ── Development ──────────────────────────────────────────────────────────────

lint:
	ruff check src/ tests/ scripts/

test:
	pytest tests/ -q --ignore=tests/e2e/

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

screenshots: build
	@echo "Starting canopy:dev container on :7860 …"
	./scripts/docker_run.sh -d --name canopy-screenshots
	@echo "Waiting for Gradio to start …"
	@until curl -sf http://localhost:7860 >/dev/null 2>&1; do sleep 2; done
	@echo "Capturing screenshots …"
	python scripts/capture_screenshots.py --url http://localhost:7860
	docker stop canopy-screenshots
	@echo "Done. Screenshots saved to docs/screenshots/"

# ── Eval ─────────────────────────────────────────────────────────────────────

eval:
	python scripts/run_eval.py

benchmark:
	python scripts/run_benchmark.py

eval-gt:
	python scripts/run_eval.py --ground-truth

eval-es:
	python scripts/run_eval.py --spanish

eval-adv:
	python scripts/run_eval.py --adversarial

# ── E2E browser tests ────────────────────────────────────────────────────────

playwright-install:
	playwright install chromium

e2e:
	pytest tests/e2e/ -v

# ── Housekeeping ─────────────────────────────────────────────────────────────

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .coverage htmlcov/ .pytest_cache/ dist/ build/ *.egg-info/
	@echo "Clean."
