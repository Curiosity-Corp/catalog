.PHONY: validate syntax lint yaml test collection sbom

PYTHON ?= python3
ANSIBLE_ROLES_PATH ?= roles

validate: yaml syntax lint test collection sbom

yaml:
	yamllint -f parsable .

syntax:
	ANSIBLE_ROLES_PATH=$(ANSIBLE_ROLES_PATH) ansible-playbook --syntax-check -i localhost, playbooks/site.yml

lint:
	ANSIBLE_ROLES_PATH=$(ANSIBLE_ROLES_PATH) ansible-lint playbooks/site.yml roles

test:
	$(PYTHON) tests/test_update_policy.py
	$(PYTHON) -m pytest -q tests/test_public_contract.py

collection:
	ansible-galaxy collection build --force

sbom: collection
	python3 scripts/build_collection_sbom.py "$$(find . -maxdepth 1 -name '*.tar.gz' -print -quit)" collection.cdx.json
