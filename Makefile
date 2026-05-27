# Resolve the container engine: prefer podman, fall back to docker
CONTAINER_ENGINE := $(shell command -v podman 2>/dev/null || command -v docker 2>/dev/null)
ifeq ($(CONTAINER_ENGINE),)
  $(error No container engine found. Install podman or docker.)
endif

# Production image name
IMAGE_NAME      := "mtv_pipelines"
# Ephemeral image used only for running the test suite
TEST_IMAGE_NAME := "mtv_pipelines_test"
# Working directory inside every container
WORKDIR         := "app"
# Container network shared between the app and its dependencies
NETWORK         := "mtv-dashboard"

.PHONY: update network test-image test test-local build shell dev run

# Install all dependencies including dev (pytest etc.) into the local venv
update:
	poetry install --with dev

# Create the container network if it does not already exist
network:
	$(CONTAINER_ENGINE) network exists $(NETWORK) || $(CONTAINER_ENGINE) network create $(NETWORK)

# Build the lightweight test container image (no skopeo/gh, no root.pem)
test-image:
	$(CONTAINER_ENGINE) build -t $(TEST_IMAGE_NAME) -f Containerfile.test .

# Run the full test suite inside the test container; build depends on this
# passing so a broken test prevents a new production image from being built
test: test-image
	$(CONTAINER_ENGINE) run --rm $(TEST_IMAGE_NAME)

# Run tests directly in the local venv without rebuilding the container —
# useful for quick iteration during development
test-local: update
	poetry run pytest

# Build the production image; tests must pass first
build: test
	$(CONTAINER_ENGINE) build -t $(IMAGE_NAME) -f Containerfile .

logs/:
	mkdir -p logs/

data/:
	mkdir -p data/

# Drop into a bash shell inside a running production container
shell:
	$(CONTAINER_ENGINE) run --rm -it \
		--env-file .env \
		--network $(NETWORK) \
		-v ./logs/:/$(WORKDIR)/logs:z \
		-v ./data/:/$(WORKDIR)/data:z \
		$(IMAGE_NAME) /bin/bash

# Full local development cycle: network → logs/data dirs → build → shell
dev: | network logs/ data/ build shell

# Run the pipeline with arbitrary arguments, e.g.: make run ARGS="--help"
run: | logs/ data/
	@echo "Running with arguments: $(ARGS)"
	$(CONTAINER_ENGINE) run --rm --env-file .env -v ./logs/:/$(WORKDIR)/logs:z -v ./data/:/$(WORKDIR)/data:z -it $(IMAGE_NAME) /bin/bash -c "poetry run python mtv_pipelines/main.py $(ARGS)"
