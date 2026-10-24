"""Regression checks for the opt-in encrypted btrfs /home volume role.

The role must be inert by default, must never touch the live fstab, crypttab,
mounts or sessions before the cutover marker exists, and the cutover script
must only switch fstab after a verified copy.
"""

import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/workstation_storage"
FILES = ROLE / "files"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
MAIN_TEXT = (ROLE / "tasks/main.yml").read_text()
AFTER_TEXT = (ROLE / "tasks/after_cutover.yml").read_text()
SITE = yaml.safe_load((ROOT / "playbooks/site.yml").read_text())
SCRIPTS = sorted(p for p in FILES.iterdir() if p.name.startswith(("hh-storage-", "hh-btrfs")) and "." not in p.name)


def test_role_is_off_by_default_and_runs_after_the_workstation_role() -> None:
    assert DEFAULTS["workstation_storage_enabled"] is False
    assert DEFAULTS["workstation_storage_provision"] is False
    assert DEFAULTS["workstation_storage_users"] == []
    assert DEFAULTS["workstation_storage_luks_uuid"] == ""
    roles = SITE[0]["roles"]
    assert roles.index("workstation_storage") > roles.index("developer_workstation")
    assert "when: workstation_storage_enabled | bool" in MAIN_TEXT


def test_main_tasks_never_write_the_live_mount_files() -> None:
    for live in ("path: /etc/fstab", "path: /etc/crypttab", "mount:", "ansible.posix.mount"):
        assert live not in MAIN_TEXT
    assert "/var/lib/hh-storage/staged/" in MAIN_TEXT


def test_live_files_and_timers_only_after_the_cutover_marker() -> None:
    assert "cutover-complete" in MAIN_TEXT
    assert "when: workstation_storage_cutover.stat.exists" in MAIN_TEXT
    assert "path: /etc/fstab" in AFTER_TEXT
    assert "RequiresMountsFor=/home" in AFTER_TEXT
    assert "hh-btrfs-scrub.timer" not in MAIN_TEXT.replace("hh-btrfs-scrub.timer\n", "")


def test_scripts_are_valid_bash() -> None:
    assert len(SCRIPTS) >= 6
    for script in SCRIPTS:
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_cutover_switches_fstab_only_after_verified_copy() -> None:
    text = (FILES / "hh-storage-cutover").read_text()
    assert "--confirm" in text
    final = text.index("hh-storage-sync final")
    verify = text.index("hh-storage-sync verify")
    snapshot = text.index("snapshot -r")
    switch = text.index("mv /etc/fstab.new /etc/fstab")
    reboot = text.index("systemctl reboot")
    assert final < verify < snapshot < switch < reboot
    assert "/etc/fstab.pre-cutover" in text


def test_sync_never_deletes_in_seed_mode_and_never_writes_home() -> None:
    text = (FILES / "hh-storage-sync").read_text()
    seed_line = next(l for l in text.splitlines() if l.strip().startswith("seed)"))
    assert "--delete" not in seed_line
    assert '"$src" "$dst"' in text


def test_provision_refuses_a_device_with_signatures() -> None:
    text = (FILES / "hh-storage-provision").read_text()
    assert "wipefs -n" in text
    assert text.index("refusing") < text.index("sgdisk")


def test_units_do_nothing_before_the_cutover() -> None:
    for unit in ("hh-btrfs-scrub",):
        text = (FILES / f"{unit}.service").read_text()
        assert "ConditionPathExists=/var/lib/hh-storage/cutover-complete" in text


def test_storage_units_are_memory_capped() -> None:
    for unit in ("hh-btrfs-scrub",):
        text = (FILES / f"{unit}.service").read_text()
        assert "MemoryHigh=" in text and "MemoryMax=" in text


def test_sync_has_a_stable_verify_that_reads_from_disk() -> None:
    text = (FILES / "hh-storage-sync").read_text()
    assert "verify-stable" in text
    assert "drop_caches" in text
    assert text.index("drop_caches") < text.index('"$0" seed')


def test_postreboot_is_idempotent_and_documents_the_rollback() -> None:
    text = (FILES / "hh-storage-postreboot").read_text()
    assert ".done" in text and "skip" in text
    assert "fstab.pre-cutover" in text and "crypttab.pre-cutover" in text
    assert "hh-storage-postboot" in text and "scrub" in text and "restic" not in text
    assert "hh-storage-postreboot" in (ROLE / "tasks" / "main.yml").read_text()


def test_no_snapshot_or_backup_machinery_ships() -> None:
    for name in ("hh-backup", "hh-btrbk.service", "hh-btrbk.timer", "hh-backup.timer"):
        assert not (FILES / name).exists()
    assert not (ROLE / "templates/btrbk.conf.j2").exists()
    for text in (MAIN_TEXT, AFTER_TEXT):
        assert "restic init" not in text and "btrbk.conf" not in text


def test_crypttab_never_blocks_boot_on_the_data_volume() -> None:
    entry = (ROLE / "templates/crypttab-entry.j2").read_text()
    assert "nofail" in entry and "headless=true" in entry


def test_hot_paths_are_subvolumes_mounted_after_the_unlock() -> None:
    paths = {hp["path"] for hp in DEFAULTS["workstation_storage_hot_paths"]}
    assert paths == {"/var/tmp", "/var/lib/containers"}
    fstab = (ROLE / "templates/fstab-entries.j2").read_text()
    assert "workstation_storage_hot_paths" in fstab
    assert "nofail" in fstab and "x-systemd.requires=systemd-cryptsetup@" in fstab
    assert "btrfs subvolume create" in AFTER_TEXT
    assert "fstrim.timer" in AFTER_TEXT and "hh-btrfs-scrub.timer" in AFTER_TEXT
