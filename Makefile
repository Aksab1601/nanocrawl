.PHONY: help install lint format typecheck test cov check demo crawl bench bench-dedup docker clean

PYTHON ?= python3

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Install the package and development tooling
	$(PYTHON) -m pip install -e ".[dev]"

lint:  ## Run the linter
	ruff check .

format:  ## Format the code
	ruff format .

typecheck:  ## Run the type checker
	mypy

test:  ## Run the test suite
	pytest -q

cov:  ## Run tests with a coverage report
	pytest -q --cov=nanocrawl --cov-report=term-missing

check: lint typecheck test  ## Everything CI runs

demo:  ## Crawl the bundled test site (no network needed)
	$(PYTHON) -m nanocrawl.cli --demo --out demo-corpus.jsonl --delay 0

bench:  ## Throughput and politeness
	$(PYTHON) benchmarks/bench_crawl.py

bench-dedup:  ## SimHash thresholds and Bloom filter memory
	$(PYTHON) benchmarks/bench_simhash.py

docker:  ## Build the image and crawl the demo site in it
	docker build -t nanocrawl:local .
	docker run --rm nanocrawl:local --demo --delay 0 --max-pages 20 --out /tmp/c.jsonl

clean:  ## Remove build artefacts and caches
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
