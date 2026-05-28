PROJECT = webcal_mcp


.PHONY: clean
clean: clean_py

.PHONY: lint
lint: lint_py

.PHONY: typecheck
typecheck: typecheck_py

.PHONY: tidy
tidy: tidy_py

.PHONY: test
test: venv
	PYTHONPATH=./.venv/bin ./.venv/bin/python -m pytest tests


include Makefile.pyproject
