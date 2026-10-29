DEMO_STATE ?= var/demo
EVAL_ARGS ?=

.PHONY: demo serve check-serve test eval clean-demo

demo:
	python -m demo --state "$(DEMO_STATE)"

# One command to run the server. Loads .env when present; every value stays
# server-side. Use `make check-serve` to validate configuration without
# opening the database or binding a port.
serve:
	@if [ -f .env ]; then set -a; . ./.env; set +a; fi; python -m chat.server

check-serve:
	@if [ -f .env ]; then set -a; . ./.env; set +a; fi; python -m chat.server --check

test:
	python -m pytest

# Offline eval is deterministic, network-blocked, and under 60s. Pass
# EVAL_ARGS="--live" for the labelled, opt-in provider run.
eval:
	python -m evals $(EVAL_ARGS)

clean-demo:
	rm -rf "$(DEMO_STATE)"
