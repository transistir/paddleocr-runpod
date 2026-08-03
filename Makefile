.PHONY: test lint format build push deploy smoke qualify

test:
	python -m unittest discover -s tests -v

lint:
	ruff check .

format:
	ruff format .

build:
	docker buildx build --platform linux/amd64 --load -t "$${DOCKER_IMAGE}" .

push:
	docker buildx build --platform linux/amd64 --push -t "$${DOCKER_IMAGE}" .

deploy:
	python scripts/deploy.py

smoke:
	python scripts/smoke_test.py

qualify: test
	@echo "Complete PRODUCTION_CHECKLIST.md, then run make push deploy smoke."
