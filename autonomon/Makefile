.PHONY: install-dev test lint format type-check check clean help

install-dev:
	uv sync --all-extras

test:
	uv run pytest tests/ -v

lint:
	uv run ruff check src/ tests/
	uv run black --check src/ tests/

format:
	uv run black src/ tests/
	uv run ruff check --fix src/ tests/

type-check:
	uv run mypy src/ tests/

check: lint type-check test

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	rm -rf build/ dist/ .coverage htmlcov/ .pytest_cache/ .mypy_cache/

help:
	@echo "  install-dev  - Install package and dev dependencies"
	@echo "  test         - Run tests"
	@echo "  lint         - ruff + black --check"
	@echo "  format       - black + ruff --fix"
	@echo "  type-check   - mypy"
	@echo "  check        - lint + type-check + test"
	@echo "  clean        - Remove generated files"
