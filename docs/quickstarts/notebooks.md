# Notebook and remote-kernel workflows

How to use Jupyter on a research VM or workstation without exposing a notebook
server to a network by accident. The role installs the tools; it deliberately
does **not** start a notebook server or open a port.

## What the role provides

- JupyterLab and IPython, installed with `pipx` for `dev_user`
  (`~/.local/bin/jupyter-lab`), by the `python` tag on `desktop` and
  `workspace` profiles.
- A scientific virtual environment, `/opt/science-venv`, and a system-wide
  Jupyter kernel named **Python 3 (Science)** registered under
  `/usr/local/share/jupyter/kernels/science`, by the `science` and
  `data-science` tags. Any JupyterLab that reads the system kernel path finds
  it, including the `pipx` one.
- No notebook service, no reverse proxy and no firewall opening. A machine
  running the role listens for notebooks only if you start a server yourself.

## Recommended pattern: loopback server plus an SSH tunnel

Run the server bound to `127.0.0.1` and reach it from your laptop through SSH.
The tunnel provides encryption and authentication, so the server needs neither
TLS nor an open port.

On the VM:

```bash
mkdir -p ~/notebooks
install -d -m 0700 ~/.config/systemd/user
cp examples/notebooks/jupyter-loopback.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now jupyter-loopback.service
journalctl --user -u jupyter-loopback.service -n 20 --no-pager   # shows the tokenized URL
```

On your laptop (the local port and the remote loopback port can differ):

```bash
ssh -N -L 8888:127.0.0.1:8888 researcher@research-vm.example.edu
```

Open the URL from the journal in your browser; it already points at
`127.0.0.1:8888`, which the tunnel forwards. For a Coder-style workspace, use the platform's own
port forwarding to reach the loopback port, never a public application URL.

To keep the server running after you log out, enable lingering once:
`sudo loginctl enable-linger researcher`. To run it only while you are
connected, start it by hand with `jupyter-lab --no-browser --ServerApp.ip=127.0.0.1`.

## Authentication, TLS, ports and ownership

| Topic | Rule |
| --- | --- |
| Authentication | Keep the token. Never start a server with an empty token or password, and never disable the XSRF check. The token is printed to the journal; treat it like a password and rotate it by restarting the service |
| TLS | Not needed on loopback behind an SSH tunnel. If a lab must serve notebooks over a network, terminate TLS in a reviewed reverse proxy with its own authentication, bind the server to that proxy's interface only, and write the decision down. Do not use `--ip=0.0.0.0` |
| Port binding | `--ServerApp.ip=127.0.0.1` and `--ServerApp.port_retries=0` so a busy port fails loudly instead of binding somewhere else. Check with `ss -ltnp \| grep 8888`: the local address must be `127.0.0.1:8888` |
| Data-directory ownership | The unit runs as the user, with `UMask=0077`, from `~/notebooks`. Keep notebooks and data in directories owned by that user (mode `0700`); do not run a notebook server as root or from a shared directory |
| Firewall | The role opens no notebook port. On profiles where UFW is on, inbound stays default-deny |
| Shared machines | One server per person, each on a loopback port of their own, each started by that person's own user manager. Never share one token between people |

## Start, stop and recovery

| Action | Command | Expected result |
| --- | --- | --- |
| Start | `systemctl --user start jupyter-loopback` | Active; port 8888 listens on `127.0.0.1` only |
| Stop | `systemctl --user stop jupyter-loopback` | Inactive; the port is released |
| Crash recovery | A killed server (`kill -9` of the main process) | `Restart=on-failure` starts it again after 5 seconds with a **new token**, so reopen the URL from the journal |
| Busy port | Another program holds 8888 | The unit fails instead of picking another port; change `--ServerApp.port` and your tunnel together |
| Unsaved work | Server restart | Notebook files on disk are intact; the running kernels and unsaved cell state are lost. Use the notebook's autosave and checkpoints |

`docs/evidence/notebook-loopback-check.md` records one manual run of these
actions against a real JupyterLab, including the version used.

## Research-data warning

A notebook server can read and write every file its user can, run arbitrary
code, and show data in the browser. Before you open data under a data-use
agreement, patient or student privacy rules, or an export-control regime:

- keep the server on loopback and use a per-person account;
- do not paste data, tokens or file contents into public AI tools, issue
  trackers or chat from a notebook;
- clear outputs before you commit or share a notebook (`jupyter nbconvert
  --clear-output --inplace notebook.ipynb`), because outputs often contain data;
- keep notebooks that touch restricted data out of cloud-synced folders;
- remember that results, caches and `~/.local/share/jupyter` stay on the VM
  disk and in any VM snapshot or backup.
