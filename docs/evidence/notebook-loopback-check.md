# Notebook loopback check

Status: one manual run, recorded once. It is not part of CI, which has no
systemd user manager or JupyterLab.

Recorded: 2026-11-04

## Environment

- Host: Ubuntu 26.04.1 LTS, systemd 259, a normal user session (`systemctl --user` reported `running`)
- Python 3.14.4 in a throwaway virtual environment
- JupyterLab 4.6.5, installed with `pip install jupyterlab`

## What was run

A transient user unit (`systemd-run --user`) carrying the same settings as
`examples/notebooks/jupyter-loopback.service`: `Restart=on-failure`,
`RestartSec=5`, `UMask=0077`, `NoNewPrivileges=true`, and the same
`jupyter-lab --no-browser --ServerApp.ip=127.0.0.1 --ServerApp.port=8888
--ServerApp.port_retries=0 --ServerApp.root_dir=...` arguments.

## Results

| Step | Observed |
| --- | --- |
| Start | `ss -ltn` showed one listener, `127.0.0.1:8888`, and nothing on any other address |
| Request without a token | `GET /api/contents` returned HTTP 403 |
| Request to the machine's LAN address (192.168.4.68) on port 8888 | Connection refused (curl exit 7) |
| `kill -9` of the main process | systemd started a new main process after the 5 second delay; `NRestarts` was 1; the listener was back on `127.0.0.1:8888`; an unauthenticated request again returned 403 |
| Stop | `systemctl --user stop` released the port (no listener on 8888) |

## What this does not show

- The unit file itself was not installed. A transient unit with the same
  properties was used, so a typo in the `[Install]` section or in `%h` expansion
  would not have been caught.
- JupyterLab came from pip, not from the pipx install the role performs, so
  the "Python 3 (Science)" kernel was not present.
- The claim that a restart prints a new token is Jupyter's default behaviour;
  this run did not compare the two tokens.
- One host and one version only. Re-run it after a JupyterLab major upgrade.
