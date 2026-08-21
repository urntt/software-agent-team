.PHONY: install setup doctor validate lock lock-benchmark format format-check lint test check

UV ?= $(HOME)/.local/bin/uv
BENCHMARK_EXCLUDE_NEWER ?= 2026-08-09

install:
	./scripts/install.sh

setup:
	./scripts/setup.sh

doctor:
	./scripts/doctor.sh

validate:
	$(UV) run --frozen sat validate-config

lock:
	$(UV) lock

lock-benchmark:
	$(UV) pip compile benchmarks/task_manager/requirements.in \
		--universal \
		--python-version 3.12 \
		--exclude-newer $(BENCHMARK_EXCLUDE_NEWER) \
		--no-annotate \
		--output-file benchmarks/task_manager/requirements.lock

format:
	$(UV) run ruff format .

format-check:
	$(UV) run ruff format --check .

lint:
	$(UV) run ruff check .

test:
	$(UV) run pytest

check: doctor format-check lint test
