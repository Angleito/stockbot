PYTHON ?= python3.14
VENV := venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest

.PHONY: setup test typecheck test-collect smoke smoke-mock smoke-prod smoke-robinhood clean-env verify

## Create a fresh virtualenv and install the pinned dependencies.
setup:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

## Run the offline unit suite (no network, no credentials required).
test:
	$(PYTEST)

## Run the checked-in Pyrefly configuration.
typecheck:
	$(VENV)/bin/pyrefly check

## Verify test collection only (fast env sanity check).
test-collect:
	$(PYTEST) --collect-only -q

## Opt-in live FINRA mock smoke suite (requires FINRA_CLIENT_ID/FINRA_CLIENT_SECRET).
smoke-mock:
	RUN_FINRA_SMOKE=1 $(PYTEST) -m finra_smoke -q

## Opt-in live FINRA production smoke suite (requires FINRA credentials).
smoke-prod:
	RUN_FINRA_PRODUCTION_SMOKE=1 $(PYTEST) -m finra_production_smoke -q

## Opt-in live Robinhood MCP smoke suite (requires OAuth state; run make login first).
smoke-robinhood:
	RUN_ROBINHOOD_SMOKE=1 $(PYTEST) -m robinhood_smoke -q

## Run both opt-in smoke suites.
smoke: smoke-mock smoke-prod

## Reproducibility check: fresh environment installs and passes the unit suite.
verify: clean-env setup test typecheck

clean-env:
	rm -rf $(VENV)
