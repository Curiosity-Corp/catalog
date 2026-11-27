.PHONY: validate syntax lint yaml test research-profile accessibility-summary collection sbom third-party tools reference reference-check packages-check register-check register-alerts

PYTHON ?= python3
ANSIBLE_ROLES_PATH ?= roles
SOURCE_DATE_EPOCH ?= 0
export SOURCE_DATE_EPOCH

validate: yaml syntax lint test tools reference-check packages-check register-check collection sbom third-party

yaml:
	yamllint -f parsable .

syntax:
	ANSIBLE_ROLES_PATH=$(ANSIBLE_ROLES_PATH) ansible-playbook --syntax-check -i localhost, playbooks/site.yml

lint:
	ANSIBLE_ROLES_PATH=$(ANSIBLE_ROLES_PATH) ansible-lint playbooks/site.yml roles

governance:
	$(PYTHON) -m pytest -q tests/test_governance_docs.py tests/test_telemetry_policy.py

quickstarts:
	$(PYTHON) -m pytest -q tests/test_quickstarts.py tests/test_restricted_network.py

accessibility-summary:
	$(PYTHON) -m pytest -q tests/test_accessibility_defaults.py tests/test_update_summary.py

test:
	$(PYTHON) tests/test_update_policy.py
	$(PYTHON) -m pytest -q tests/test_public_contract.py tests/test_console_tuning.py tests/test_tmp_hygiene.py tests/test_workstation_posture.py tests/test_hardware_profiles.py tests/test_desktop_multiuser.py tests/test_apt_keyring_policy.py tests/test_apt_keyring_check_mode.py tests/test_user_systemd_policy.py tests/test_system_retirement.py tests/test_pipx_repair.py tests/test_science_venv_repair.py tests/test_m365_cli_entrypoints.py tests/test_sunshine_asset_selection.py tests/test_update_rollback_symlinks.py tests/test_healthcheck_disabled_apps.py tests/test_litra_cli.py tests/test_minimal_desktop_panel.py tests/test_bitwarden_desktop.py tests/test_slack_desktop.py tests/test_jellyfin_desktop.py tests/test_media_folder.py tests/test_deskmeet_8k_display.py tests/test_display_guard.py tests/test_logitech_g604.py tests/test_authd_broker_conf.py tests/test_samsung_tv_cli.py tests/test_pro_mimic.py tests/test_firewall_posture.py tests/test_streamstack.py tests/test_streamstack_low_latency.py tests/test_streamstack_ai_deps.py tests/test_hhtest.py tests/test_workstation_storage.py tests/test_cpu_thermal_guard.py tests/test_cpu_thermal_log.py tests/test_crash_diagnostics.py tests/test_browser_video_decode.py tests/test_mouse_debounce.py tests/test_pia_tunnel.py tests/test_fleetd.py tests/test_python_research.py tests/test_zram_swap.py tests/test_monitor_sleep_pointer.py tests/test_no_log_policy.py tests/test_ziti_health.py tests/test_ziti_pins.py tests/test_include_tags_apply.py tests/test_agent_cpu_policy.py

# The Rust tool crate (tools/README.md). CI runs the same in the `tools` job.
CARGO ?= cargo
CATALOG_TOOLS = $(CARGO) run --quiet --locked --manifest-path tools/Cargo.toml --

tools:
	cd tools && $(CARGO) fmt --all --check && $(CARGO) clippy --locked --all-targets -- -D warnings && $(CARGO) test --locked

# Regenerate docs/reference.md from the role defaults, tasks and docs/reference-annotations.yml.
reference:
	$(CATALOG_TOOLS) reference

reference-check:
	$(CATALOG_TOOLS) reference --check

# The dependency register (docs/dependency-register.md); CI runs the same in the `tools` job.
register-check:
	$(CATALOG_TOOLS) register check

register-alerts:
	$(CATALOG_TOOLS) register alerts

# Fail when docs/evidence/platform-packages-*.json no longer matches the role's packages.
packages-check:
	$(CATALOG_TOOLS) packages check

# Real install of examples/python-research.yml in a temp dir (needs PyPI, uv).
research-profile:
	$(PYTHON) scripts/validate_python_research.py

collection:
	ansible-galaxy collection build --force
	$(PYTHON) scripts/normalize_collection_artifact.py "$$(find . -maxdepth 1 -name '*.tar.gz' -print -quit)"

# Licence scan of the built collection (THIRD_PARTY.md); CI runs the same in
# the `third_party` job.
third-party: collection
	scripts/check-third-party.sh "$$(find . -maxdepth 1 -name '*.tar.gz' -print -quit)"

# The release SBOM (docs/sbom.md); CI runs the same in the `sbom` job.
SBOM_SCHEMA = ci/schemas/cyclonedx-1.6/bom-1.6.schema.json
sbom: collection
	archive="$$(find . -maxdepth 1 -name '*.tar.gz' -print -quit)" && \
	$(CATALOG_TOOLS) sbom --archive "$$archive" --output collection.cdx.json && \
	check-jsonschema --base-uri "file://$(CURDIR)/$(SBOM_SCHEMA)" --schemafile $(SBOM_SCHEMA) collection.cdx.json && \
	$(CATALOG_TOOLS) sbom --archive "$$archive" --output collection.cdx.json --check
