SHELL   := /bin/bash
PYTHON  ?= python3

SCRIPTS := fix-newlines.py sort-apk.py

export PYTHONWARNINGS := default

.PHONY: all test lint lint-extra

all:

test: lint lint-extra

lint:
	flake8 $(SCRIPTS)
	pylint $(SCRIPTS)

lint-extra:
	mypy $(SCRIPTS)
