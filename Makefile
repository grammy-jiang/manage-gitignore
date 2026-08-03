.PHONY: help test lint format typecheck verify build install-skill clean

help:
	@echo "test           run the test suite"
	@echo "lint           ruff check + format check"
	@echo "format         ruff format (writes)"
	@echo "typecheck      mypy"
	@echo "verify         lint + typecheck + test  (run before shipping a change)"
	@echo "build          sdist + wheel into dist/"
	@echo "install-skill  copy the skill into ~/.claude/skills/"

test:
	python3 -m pytest

lint:
	python3 -m ruff check .
	python3 -m ruff format --check .

format:
	python3 -m ruff format .
	python3 -m ruff check --fix .

typecheck:
	python3 -m mypy

verify: lint typecheck test

build:
	python3 -m build

install-skill:
	python3 -m manage_gitignore.cli install-skill --force

clean:
	rm -rf dist build .pytest_cache .mypy_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
