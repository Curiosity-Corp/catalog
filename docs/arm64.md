# ARM64 status and validation

ARM64 Debian-family devices are **experimental** (see [compatibility.md](compatibility.md)).
Nothing here has been run on ARM hardware; this page says what is checked without
a device, what fails by design on ARM, and what a person with an ARM64 machine
must do to promote it.

## Checked without hardware

`tests/test_arm64_readiness.py`, in CI on every merge request:

* apt source lines use the Debian architecture name. Before this change twenty
  repository lines were written `[arch={{ ansible_architecture | replace('x86_64', 'amd64') }}]`,
  which is `arch=aarch64` on ARM64 and which apt rejects. They now use
  `workstation_deb_arch` (`x86_64` becomes `amd64`, `aarch64` becomes `arm64`, anything else is
  passed through). A test fails if a new line derives the name any other way. x86_64 output is unchanged.
* the existing asset-selection tests cover the ARM64 name mapping for the Litra CLI and Sunshine
  (`tests/test_litra_cli.py`, `tests/test_sunshine_asset_selection.py`).

Installers that publish no ARM64 artifact fail with a message naming the tool and the
architecture, for example "Litra CLI has no verifiable current Linux asset for aarch64" and
"Mattermost mmctl has no managed Linux artifact for aarch64".

## x86_64 only by design

These are skipped or refused on ARM64 and are not bugs: GRUB autoboot and console video
tuning (`main.yml`), the Bitwarden desktop package (`managed-updates.yml`), the FocusPass
screen-lock package (asserts amd64) and the T460 console tasks.

## What a person with ARM64 hardware does

1. Use a disposable Debian 13 or Ubuntu 24.04 arm64 machine (a Raspberry Pi 5, an ARM cloud VM) with
   the `thin-client` profile and an unrelated hardware profile.
2. `ansible-playbook playbooks/site.yml --syntax-check`, then `--check --diff`, then a real run, then a
   second run that must report `changed=0`.
3. Record failures in a dated file under `docs/evidence/` (what ran, the commit, the distribution,
   the board) and list each optional tool without an ARM64 build.
4. Repeat `python scripts/probe_platform_packages.py probe debian:13 --platform linux/arm64 --output ...`
   on a host with QEMU user emulation to record package availability for arm64.

Until step 2 passes twice on real hardware, ARM64 stays experimental.
