# Third-party notices

The Ansible automation and documentation in this repository are released
under the Apache License 2.0. A small number of files are separate works or
derivatives and keep their upstream licences. The authoritative record is
[`THIRD_PARTY_INVENTORY.yml`](THIRD_PARTY_INVENTORY.yml): for every vendored,
derived or pinned item it lists the source, version or commit, licence,
redistribution conditions and whether it is shipped, excluded or fetched at
run time. Notice text that has to travel with redistributed files is in
[`NOTICE`](NOTICE), and the licence texts are in [`LICENSES/`](LICENSES).

## Shipped in the built collection

- `roles/developer_workstation/files/wee_most.py`: wee-most 0.3.0, GPL-3.0-only,
  locally patched (optional WeeChat chat profile, off by default).
- `roles/developer_workstation/files/teams.py`: written for this collection,
  marked GPL-3.0, follows the patterns of `wee_most.py`. Provenance needs review.
- `roles/developer_workstation/files/minimal-desktop/onyx-large-themerc`: the
  Openbox Onyx theme with larger padding, GPL-2.0-or-later.
- `roles/developer_workstation/files/minimal-desktop/rc.xml`: may derive from
  Openbox's stock `rc.xml`; treated as GPL-2.0-or-later. Provenance needs review.
- `roles/developer_workstation/files/i915-pcon-frl-444/i915-pcon-frl-444.patch`:
  a patch to the Linux kernel i915 driver; upstream's licence applies to the
  patched code. Provenance needs review.
- `roles/developer_workstation/files/streamstack/`: the StreamStack project
  0.0.26, Apache-2.0, vendored as a squashed git subtree. It ships no fonts and
  no third-party models; its icons, look thumbnails and LUTs are generated.

## Left out of the built collection

Files whose provenance is unresolved stay in the repository but are listed in
`build_ignore` in `galaxy.yml`, so the artifact does not redistribute them:

- `roles/developer_workstation/files/streamstack/docs/evidence/`: screenshots of
  Zoom, Edge, Chromium and Firefox and a live camera frame. Permission to
  republish them is not documented.

## Fetched at run time, not redistributed

The roles download these on the managed host; check each upstream project for
its current licence: wee-slack, WeeChat `go.py`, DeepFilterNet, OpenVINO, the
YuNet model, tmux, Terraform, Vault, Packer, the PIA installer, the GitHub
release tools, vendor CLIs and the vendor apt repositories.

## Keeping the inventory honest

`scripts/check_third_party_inventory.py` builds the collection into a temporary
directory (`ansible-galaxy collection build`) and compares the tarball with the
inventory: it fails on an excluded file that is in the artifact, a third-party
looking file (licence or copyright header, font, installer, archive, image, key)
that no entry covers, missing NOTICE text or licence texts, and version drift in
pinned items. Run it with `make licenses`; CI runs it in the
`third_party_inventory` job. When you vendor something new, add an entry first.
Review the inventory before every release (`docs/release-checklist.md`).
