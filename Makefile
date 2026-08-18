.PHONY: help install install-dev check run dry watch test cov lint fmt clean docker

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:      ## Install runtime dependencies
	pip install -r requirements.txt

install-dev:  ## Install runtime + test dependencies
	pip install -r requirements-dev.txt

check:        ## Validate configuration and show which modules will run
	python main.py check

run:          ## Execute one pipeline pass
	python main.py run

dry:          ## Full pipeline with alerts suppressed
	python main.py run --dry-run

watch:        ## Run continuously on POLL_INTERVAL_MINUTES
	python main.py watch

test:         ## Run the test suite
	pytest -q

cov:          ## Run the suite with a coverage report
	pytest -q --cov=miami_bot --cov-report=term-missing

lint:         ## Static checks
	ruff check .

fmt:          ## Auto-fix what ruff can
	ruff check --fix .

clean:        ## Remove caches and build artefacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov build dist *.egg-info

docker:       ## Build the container image
	docker build -t miami-condo-monitor:latest .
