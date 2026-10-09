.PHONY: validate syntax lint yaml test collection sbom

PYTHON ?= python3
ANSIBLE_ROLES_PATH ?= roles
SOURCE_DATE_EPOCH ?= 0
export SOURCE_DATE_EPOCH

validate: yaml syntax lint test collection sbom

yaml:
	yamllint -f parsable .

syntax:
	ANSIBLE_ROLES_PATH=$(ANSIBLE_ROLES_PATH) ansible-playbook --syntax-check -i localhost, playbooks/site.yml

lint:
	ANSIBLE_ROLES_PATH=$(ANSIBLE_ROLES_PATH) ansible-lint playbooks/site.yml roles

test:
	$(PYTHON) tests/test_update_policy.py
	$(PYTHON) -m pytest -q tests/test_public_contract.py tests/test_console_tuning.py tests/test_tmp_hygiene.py tests/test_workstation_posture.py tests/test_hardware_profiles.py tests/test_desktop_multiuser.py tests/test_apt_keyring_policy.py tests/test_user_systemd_policy.py tests/test_pipx_repair.py tests/test_science_venv_repair.py tests/test_m365_cli_entrypoints.py tests/test_sunshine_asset_selection.py tests/test_update_rollback_symlinks.py tests/test_healthcheck_disabled_apps.py tests/test_litra_cli.py tests/test_minimal_desktop_panel.py

collection:
	ansible-galaxy collection build --force
	$(PYTHON) scripts/normalize_collection_artifact.py "$$(find . -maxdepth 1 -name '*.tar.gz' -print -quit)"

sbom: collection
	python3 scripts/build_collection_sbom.py "$$(find . -maxdepth 1 -name '*.tar.gz' -print -quit)" collection.cdx.json
