VENV := venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest

.PHONY: setup test test-collect smoke smoke-mock smoke-prod clean-env verify

## Create a fresh virtualenv and install the pinned dependencies.
setup:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

## Run the offline unit suite (no network, no credentials required).
test:
	$(PYTEST)

## Verify test collection only (fast env sanity check).
test-collect:
	$(PYTEST) --collect-only -q

## Opt-in live FINRA mock smoke suite (requires FINRA_CLIENT_ID/FINRA_CLIENT_SECRET).
smoke-mock:
	RUN_FINRA_SMOKE=1 $(PYTEST) -m finra_smoke -q

## Opt-in live FINRA production smoke suite (requires FINRA credentials).
smoke-prod:
	RUN_FINRA_PRODUCTION_SMOKE=1 $(PYTEST) -m finra_production_smoke -q

## Run both opt-in smoke suites.
smoke: smoke-mock smoke-prod

## Reproducibility check: fresh environment installs and passes the unit suite.
verify: clean-env setup test

clean-env:
	rm -rf $(VENV)