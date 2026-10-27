# Security policy

Report vulnerabilities privately to the maintainers through the project's
issue tracker with the confidential flag set, not in a public issue. Include
the version (`streamstack --version`) and steps to reproduce.

StreamStack runs as the desktop user and never needs root at run time. The
security-relevant parts are:

- **Claude passes** (auto mode) send one camera still and statistics; see
  [docs/auto-mode.md](docs/auto-mode.md#backends-and-privacy). The Claude Code
  CLI runs with `--restricted --tools Read` in a directory holding only that
  still.
- **Secrets**: an Anthropic API key is read from the environment or a mode-600
  `~/.config/streamstack/auto.env`; a file readable by others is refused.
- **Local control**: the Console listens on 127.0.0.1 only, and every request carries a per-run token kept in a
  mode-600 runtime file; the auto director reaches the video engine through that same authenticated API.

Please do not include real camera stills, logs with device serial numbers or
API keys in reports.
