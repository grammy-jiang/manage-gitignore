.PHONY: help test coverage lint format typecheck verify build install uninstall clean

help:
	@echo "test           run the test suite"
	@echo "lint           ruff check + format check"
	@echo "format         ruff format (writes)"
	@echo "typecheck      mypy"
	@echo "verify         lint + typecheck + test  (run before shipping a change)"
	@echo "build          sdist + wheel into dist/"
	@echo "install        symlink the skill into ~/.claude/skills/"
	@echo "uninstall      remove that symlink again"

test:
	python3 -m pytest

# MG_COVER_SUBPROCESS makes the suite run each script under coverage too --
# most of it drives them as subprocesses, which a plain run cannot see.
# COVERAGE_FILE is absolute because those subprocesses run in throwaway cwds.
coverage:
	rm -f .coverage .coverage.*
	COVERAGE_FILE=$(CURDIR)/.coverage MG_COVER_SUBPROCESS=1 \
	    python3 -m pytest --cov --cov-report= -q
	COVERAGE_FILE=$(CURDIR)/.coverage python3 -m coverage report
	COVERAGE_FILE=$(CURDIR)/.coverage python3 tests/check_coverage.py --min 90

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

# PYTHONPATH=src rather than relying on an editable install: these targets then
# work from a bare checkout, and they always link *this* working tree rather
# than whatever happens to be installed.
install:
	PYTHONPATH=src python3 -m manage_gitignore.cli install

uninstall:
	PYTHONPATH=src python3 -m manage_gitignore.cli uninstall

clean:
	rm -rf dist build .pytest_cache .mypy_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
