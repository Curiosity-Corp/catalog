# Third-party notices

The Ansible automation and documentation in this repository are released
under the Apache License 2.0. A small number of optional chat integrations are
separate works and retain their upstream licenses:

- `roles/developer_workstation/files/wee_most.py` carries the upstream GPLv3
  notice and is based on `wee-most`.
- `roles/developer_workstation/files/teams.py` is marked GPLv3.
- `roles/developer_workstation/tasks/weechat.yml` installs `wee-slack` from
  its upstream repository at runtime.

The optional chat profile is disabled by default. Check each upstream project
for its current license and attribution requirements before redistributing a
built collection containing those files.
