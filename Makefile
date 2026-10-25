# Every target is safe to run more than once.
SHELL := /bin/bash
export HOST_UID := $(shell id -u)
export HOST_GID := $(shell id -g)
SCENARIOS := short-outage long-outage high-latency low-bandwidth

.PHONY: up down generate certs status report logs test-delivery test-rogue $(SCENARIOS) clean

BUILD_INPUTS := Dockerfile $(wildcard pipeline/*.py) gcs-sim/Dockerfile gcs-sim/telemetry_sim.py

up: generate certs .build-stamp  ## deploy or update everything
	mkdir -p data/receiver
	docker compose up -d --remove-orphans

# Images are rebuilt only when their inputs change. Rebuilding unchanged code still gives
# a new image id (BuildKit attestations), and Compose would recreate every container.
.build-stamp: $(BUILD_INPUTS)
	docker compose build
	touch $@

generate:
	python3 scripts/generate.py

certs:
	scripts/certs.sh

down:
	docker compose --profile rogue down --remove-orphans

status:
	docker compose ps

report:  ## what arrived in the last 5 minutes
	docker compose exec receiver python3 -m pipeline.report --minutes 5

logs:
	docker compose logs -f --tail 20 receiver $(shell grep -Ev '^\s*(#|$$)' teams.txt | sed 's/^/agent-/')

test-delivery:
	scripts/test-delivery.sh

test-rogue:
	scripts/test-rogue.sh

$(SCENARIOS):
	scripts/scenario.sh $@

clean: down  ## removes stored telemetry; keeps the certificates
	rm -rf data
