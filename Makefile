# Developer shortcuts. Stage commands remain explicit; none of these starts model work.
CONFIG ?= configs/company.local.toml

.PHONY: help setup browser doctor preflight lint test check status

help:
	@printf '%s\n' 'make setup      Install locked dependencies and create a local config' 'make doctor     Check config and catalogs without inference' 'make preflight  Probe runtime socket access without inference' 'make check      Run lint and regression tests' 'make status COMPANY=workspace/companies/ID'

setup:
	uv sync --frozen --extra browser
	@test -f configs/company.local.toml || cp configs/company.example.toml configs/company.local.toml

browser:
	uv run playwright install --with-deps chromium-headless-shell
	uv run python scripts/browser_path.py

doctor:
	uv run company-envs --config "$(CONFIG)" doctor

preflight:
	uv run company-envs --config "$(CONFIG)" preflight --scope runtime

lint:
	uv run ruff check src scripts tests

test:
	uv run pytest -q

check: lint test

status:
	@test -n "$(COMPANY)" || { echo 'Set COMPANY to an exported company folder'; exit 2; }
	uv run company-envs --config "$(CONFIG)" pilot-status "$(COMPANY)"
