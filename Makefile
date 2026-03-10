IMAGE_NAME="mtv_pipelines"
WORKDIR="app"

.PHONY: update
update:
	poetry install

.PHONY: build
build:
	podman build -t $(IMAGE_NAME) -f Containerfile .

logs/:
	mkdir -p logs/

data/:
	mkdir -p data/

.PHONY: shell
shell: | logs/ data/
	podman run --rm --env-file .env -v ./logs/:/$(WORKDIR)/logs:z -v ./data/:/$(WORKDIR)/data:z -it $(IMAGE_NAME) /bin/bash

.PHONY: dev
dev: | logs/ data/ build shell

.PHONY: run
run: | logs/ data/
	@echo "Running with arguments: $(ARGS)"
	podman run --rm --env-file .env -v ./logs/:/$(WORKDIR)/logs:z -v ./data/:/$(WORKDIR)/data:z -it $(IMAGE_NAME) /bin/bash -c "poetry run python mtv_pipelines/main.py $(ARGS)"
