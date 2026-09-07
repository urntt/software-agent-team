.PHONY: install uninstall setup doctor validate loopback-check lock lock-runtime format format-check lint test check

UV ?= $(HOME)/.local/bin/uv
OPENCLAW ?= $(CURDIR)/.sat/openclaw/bin/openclaw
RUNTIME_EXCLUDE_NEWER ?= 2026-08-09

install:
	./scripts/install.sh

uninstall:
	./scripts/uninstall.sh

setup:
	./scripts/setup.sh

doctor:
	./scripts/doctor.sh

validate:
	$(UV) run --frozen sat validate-config

loopback-check:
	$(UV) run --frozen python -m software_agent_team.loopback_validation \
		--openclaw "$(OPENCLAW)"

lock:
	$(UV) lock

lock-runtime:
	$(UV) pip compile runtime/python/requirements.in \
		--universal \
		--python-version 3.12 \
		--exclude-newer $(RUNTIME_EXCLUDE_NEWER) \
		--no-annotate \
		--output-file runtime/python/requirements.lock

format:
	$(UV) run ruff format .

format-check:
	$(UV) run ruff format --check .

lint:
	$(UV) run ruff check .

test:
	$(UV) run pytest

check:
	SAT_UV_BIN="$(UV)" $(UV) run --frozen python -m software_agent_team.full_gate
