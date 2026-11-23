EVAL_ARGS ?=

.PHONY: serve check-serve test audit eval phone

# One command to run the server. Loads .env when present; every value stays
# server-side. Use `make check-serve` to validate configuration without
# opening the database or binding a port.
serve:
	@if [ -f .env ]; then set -a; . ./.env; set +a; fi; python -m chat.server

check-serve:
	@if [ -f .env ]; then set -a; . ./.env; set +a; fi; python -m chat.server --check

test:
	python -m pytest

# Dependency audit: the runtime dependencies declared in pyproject.toml,
# checked against published advisories. Needs network and the auditor:
# `pip install pip-audit==2.10.1`. A match fails the command; an empty
# dependency list fails too.
audit:
	python -m tools.audit_deps

# Offline eval is deterministic, network-blocked, and under 60s. Pass
# EVAL_ARGS="--live" for the labelled, opt-in provider run.
eval:
	python -m evals $(EVAL_ARGS)

# Build the phone web app (PWA) into phone/dist/, which the server mounts at
# /phone/. Requires node/npm on the build host. phone/dist/ is not committed, so
# until this target has run, /phone/ answers 404 phone_not_built. The Docker
# image does not include the phone app (docs/DEPLOY.md section 1).
phone:
	cd phone && npm ci && npm run build
