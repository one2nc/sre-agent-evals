.PHONY: help up down reset build ask status all test export \
	load-traces create-dataset experiment-runs offline-judges

# Langfuse runs from Langfuse's own docker-compose.yml, downloaded from the release
# tag on first use (not kept in the repo) and checked against its SHA-256. The
# overlay docker-compose.yml pins the images to the same version and adds the demo.
# To upgrade: change both values below (sha256 of the new file).
export LANGFUSE_VERSION := 3.225.11
LANGFUSE_COMPOSE_SHA256 := 26510ab5cc9163bf2212d5dfb991b3a71e1ce5cf7d032b595e7eee122bec1687
LANGFUSE_COMPOSE := langfuse/docker-compose.yml
LANGFUSE_COMPOSE_URL := https://raw.githubusercontent.com/langfuse/langfuse/v$(LANGFUSE_VERSION)/docker-compose.yml
SHA256SUM := $(shell command -v sha256sum >/dev/null 2>&1 && echo sha256sum || echo "shasum -a 256")

# Add your own overlay (e.g. to remap ports that are taken) with
#   make up COMPOSE_FILES="-f langfuse/docker-compose.yml -f docker-compose.yml -f my-ports.yml"
COMPOSE_FILES ?= -f $(LANGFUSE_COMPOSE) -f docker-compose.yml
COMPOSE := docker compose --project-directory . $(COMPOSE_FILES)
DEMO := $(COMPOSE) --profile tools run --rm demo

help:
	@echo "Langfuse stack"
	@echo "  make up               start Langfuse, logs in the foreground (leave it running)"
	@echo "  make down             stop it; make reset also deletes its data"
	@echo
	@echo "The evaluation, one question per step, in order (each step builds on the previous ones):"
	@echo "  make load-traces      what did the agent do? its investigations as traces"
	@echo "  make create-dataset   what should it have found? 17 problems with root causes"
	@echo "  make experiment-runs  how often is it right, at what cost? runs with SREGym's grades"
	@echo "  make offline-judges   can an LLM judge in Langfuse grade it? managed evaluator + SDK judge"
	@echo
	@echo "  make all              run every step in order"
	@echo "  make status           what is loaded, and the next step"
	@echo "  make ask              replay one investigation (smoke test)"
	@echo "  make test             check that data/ holds what the steps need (no Docker)"

.env:
	@echo "Missing .env: cp .env.example .env (and review it)"; exit 1

$(LANGFUSE_COMPOSE):
	@mkdir -p $(dir $@)
	@echo "Downloading Langfuse's docker-compose.yml (v$(LANGFUSE_VERSION))"
	curl -fsSL --retry 3 -o $@.tmp $(LANGFUSE_COMPOSE_URL)
	@echo "$(LANGFUSE_COMPOSE_SHA256)  $@.tmp" | $(SHA256SUM) -c - >/dev/null \
		|| { echo "Checksum mismatch for $(LANGFUSE_COMPOSE_URL); not using it."; rm -f $@.tmp; exit 1; }
	@mv $@.tmp $@

build: .env $(LANGFUSE_COMPOSE)
	$(COMPOSE) --profile tools build demo

up: .env $(LANGFUSE_COMPOSE)
	$(COMPOSE) up

down: $(LANGFUSE_COMPOSE)
	$(COMPOSE) --profile tools down --remove-orphans

reset: $(LANGFUSE_COMPOSE)
	$(COMPOSE) --profile tools down --remove-orphans --volumes

ask: build
	$(DEMO) ask

status: build
	$(DEMO) status

load-traces create-dataset experiment-runs offline-judges all: build
	$(DEMO) $@

# Needs only python3 on the host: the test reads data/ with the standard library.
test:
	python3 -m unittest discover -s tests -v

# Maintainer only: re-export data/ from the Langfuse that holds the original runs.
# Needs EXPORT_LANGFUSE_* in the environment; see export/export_demo_data.py.
# What the export keeps: the two agent runs, their evaluator replays (the suffix is
# the time replay_judge_check.py ran), and the scores the steps import.
EXPORT_DATASET ?= sregym-lite
EXPORT_RUNS ?= holmes-noise-run-1,holmes-noise-run-2-steps100,\
	replay-holmes-noise-run-1-1792767906,replay-holmes-noise-run-2-steps100-1792767906
EXPORT_SCORES ?= diagnosis-pass,diagnosis-score,diagnosis-localization,diagnosis-characterization,\
	diagnosis-scope,run-complete,time-to-diagnose-s,\
	holmes-cost-usd,holmes-input-tokens,holmes-output-tokens,holmes-llm-calls,\
	sdk-diagnosis-score,sdk-diagnosis-pass,sdk-diagnosis-localization,\
	sdk-diagnosis-characterization,sdk-diagnosis-scope,sregym

export: build
	$(COMPOSE) --profile tools run --rm --no-deps -u "$$(id -u):$$(id -g)" -v "$(CURDIR)/data:/app/data" \
		-e EXPORT_LANGFUSE_HOST -e EXPORT_LANGFUSE_PUBLIC_KEY -e EXPORT_LANGFUSE_SECRET_KEY \
		-e EXPORT_DATASET="$(EXPORT_DATASET)" -e EXPORT_RUNS="$(EXPORT_RUNS)" -e EXPORT_SCORES="$(EXPORT_SCORES)" \
		-e EXPORT_DIR=/app/data \
		-e EXPORT_SOURCE_LANGFUSE_VERSION -e EXPORT_HOLMESGPT_COMMIT -e EXPORT_SREGYM_COMMIT \
		--entrypoint python demo export/export_demo_data.py
