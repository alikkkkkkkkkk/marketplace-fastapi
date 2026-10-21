OPENAPI_GENERATOR_VERSION ?= v7.26.0
SPEC := $(PWD)/src/main/resources/openapi/openapi.yaml
GENERATED := $(PWD)/generated

.PHONY: generate up down logs test clean db-shell

generate:
	rm -rf generated
	docker run --rm \
	  --user "$$(id -u):$$(id -g)" \
	  -v "$(CURDIR):/local" \
	  openapitools/openapi-generator-cli:v7.26.0 generate \
	  -i /local/src/main/resources/openapi/openapi.yaml \
	  -g python-fastapi \
	  -o /local/generated \
	  -t /local/templates/python-fastapi \
	  --additional-properties=packageName=marketplace_api,fastapiImplementationPackage=marketplace_impl,useExternalImplementationPackage=true,sourceFolder=src

up:
	docker compose up --build

down:
	docker compose down -v

logs:
	docker compose logs -f app db flyway

test: generate
	PYTHONPATH=generated/src:. pytest -q tests

clean:
	rm -rf generated .pytest_cache **/__pycache__

db-shell:
	docker compose exec db psql -U marketplace -d marketplace
