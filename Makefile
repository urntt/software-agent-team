.PHONY: setup doctor validate lock format format-check lint test check

UV ?= $(HOME)/.local/bin/uv

setup:
	./scripts/setup.sh

doctor:
	./scripts/doctor.sh

validate:
	$(UV) run --frozen sat validate-config

lock:
	$(UV) lock

format:
	$(UV) run ruff format .

format-check:
	$(UV) run ruff format --check .

lint:
	$(UV) run ruff check .

test:
	$(UV) run pytest

check: doctor format-check lint test
