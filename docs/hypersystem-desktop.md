# hypersystem-desktop: a suite's web apps on a laptop

`hypersystem-desktop` puts every web app of a suite that a person's roles allow on
a laptop, from one role run. It is a task group of the `developer_workstation`
role (`tasks/hypersystem-desktop.yml`, tag `hypersystem-desktop`) and is off by
default. The target is a BYOD laptop such as a ThinkPad T460 on Ubuntu 26.04 or
Debian 13, on the `desktop` or `thin-client` profile.

When `hypersystem_desktop_enabled` is true, a run:

1. reads the suite's app catalog: a compiled `hypersystem-catalog/v1` document
   from an https URL or a file on the host, or a list of apps given as variables;
2. keeps the apps whose status is `beta` or `ga` and whose `roles` share one with
   `hypersystem_desktop_roles` (an app with no roles is for everyone);
3. checks that the Ziti tunnel of the `ziti` task group is there and that every
   app host is inside `ziti_intercept_domains`;
4. installs one Chromium-family browser and force-installs every selected app as
   a web app (PWA) through managed browser policy (`WebAppInstallForceList`);
5. writes one menu launcher per app plus a launcher for the suite browser
   profile, and the `hypersystem-desktop` helper that opens them and checks the
   install;
6. optionally installs hyperharness for the primary user.

The role never ships a catalog address, an app host, a Ziti identity or a
credential. Those come from host or group variables, Ansible Vault, or a
private inventory.

## Quick start

```yaml
---
workstation_profile: desktop
hardware_profile: t460

# The tunnel belongs to the ziti task group; see docs/configuration.md.
ziti_enabled: true
ziti_oidc_provider: lab-oidc
ziti_intercept_domains:
  - suite.lab.example.edu
ziti_health_check_probe_hosts:
  - notebooks.suite.lab.example.edu

hypersystem_desktop_enabled: true
hypersystem_desktop_catalog_src: https://packages.lab.example.edu/suite/catalog.json
hypersystem_desktop_roles: [researchers]
```

Preview, then apply only this task group:

```bash
ansible-playbook -i localhost, -c local playbooks/site.yml --become \
  -e @laptop.yml --tags ziti,hypersystem-desktop --check --diff
ansible-playbook -i localhost, -c local playbooks/site.yml --become \
  -e @laptop.yml --tags ziti,hypersystem-desktop
```

Then, as the laptop's user:

```bash
hypersystem-desktop doctor        # browser, policy, tunnel, DNS and HTTPS per app
hypersystem-desktop list          # the apps this laptop installs
hypersystem-desktop open notebooks
```

Record the `doctor` output with the quickstart result for the machine.

## The catalog

The compiled catalog document is the one a suite's bar reads:

```json
{
  "schema": "hypersystem-catalog/v1",
  "apps": [
    {
      "id": "notebooks",
      "name": "Notebooks",
      "purpose": "Shared research notebooks.",
      "icon": "notebook",
      "owner_group": "research",
      "repo": "research/notebooks",
      "roles": ["researchers"],
      "urls": {"web": "https://notebooks.suite.lab.example.edu/"},
      "ssh": null,
      "status": "ga"
    }
  ]
}
```

A bare list of entries is accepted too. An entry needs `id` (lowercase letters,
digits and dashes, starting with a letter), `name`, `roles`, `urls.web` and
`status`; `purpose`, `icon`, `ssh` and `urls.local` are optional here.
`urls.web` must be https, because browsers install web apps only from a secure
origin. A malformed or duplicate entry fails the run and names the entry; with
`hypersystem_desktop_catalog_strict: false` it is skipped with a warning
instead.

| Variable | Default | Meaning |
| --- | --- | --- |
| `hypersystem_desktop_catalog_src` | `""` | https URL or absolute path on the host of the compiled catalog |
| `hypersystem_desktop_catalog_sha256` | `""` | Optional SHA-256 the fetched document must match |
| `hypersystem_desktop_catalog_headers` | `{}` | Extra request headers, for example a registry token from Vault; never logged |
| `hypersystem_desktop_apps` | `[]` | The apps as variables instead of a catalog (`roles` defaults to `[]`, `status` to `ga`) |
| `hypersystem_desktop_roles` | `[]` | The person's suite roles |
| `hypersystem_desktop_statuses` | `[beta, ga]` | Statuses that are installed; `planned` and `retired` are never usable |
| `hypersystem_desktop_allow_empty` | `false` | Allow a run that selects no app (otherwise it fails, usually a roles typo) |

Exactly one of `hypersystem_desktop_catalog_src` and `hypersystem_desktop_apps`
must be set. The URL is read in check mode too (a read-only GET), so a dry run
previews the real app list.

## Ziti

The `ziti` task group owns the tunnel: `ziti_enabled` installs and enrols it,
`ziti_intercept_domains` routes the suite domains through it (split DNS on the
thin-client profile), and the tunnel health guard restarts a half-up tunnel.
`hypersystem-desktop` duplicates none of it. With
`hypersystem_desktop_require_ziti: true` (the default) a run fails, with the
setting to change, when:

- `ziti_enabled` is false and no `ziti_service_name` unit exists (a hand-managed
  tunnel counts);
- an app host is not inside `ziti_intercept_domains` (`suite.example`,
  `.suite.example` and `*.suite.example` all cover `app.suite.example`).

It also suggests adding an app host to `ziti_health_check_probe_hosts`, so the
health guard notices when app names stop resolving. Set
`hypersystem_desktop_require_ziti: false` only for apps that are reachable
without Ziti.

`hypersystem-desktop doctor` checks the same chain on the running laptop: the
tunnel unit is active, every app host resolves into the tunnel's DNS range
(`hypersystem_desktop_ziti_dns_ip_range`, default `100.64.0.0/10`), and every
app answers over HTTPS (any HTTP status, a sign-in redirect included, proves the
path; a 5xx or a connection error fails). `--offline` skips DNS and HTTPS,
`--json` prints a machine-readable report, and the exit code is 1 when a check
fails.

## Browser and profile

| `hypersystem_desktop_browser` | Package | Managed policy directory | Suite profile (under `$HOME`) |
| --- | --- | --- | --- |
| `chromium` (auto on Debian) | `chromium` (apt) | `/etc/chromium/policies/managed` | `.config/hypersystem-desktop/chromium` |
| `chromium-snap` (auto on Ubuntu) | `chromium` (snap) | `/etc/chromium-browser/policies/managed` | `snap/chromium/common/hypersystem-desktop` |
| `google-chrome` | existing install only | `/etc/opt/chrome/policies/managed` | `.config/hypersystem-desktop/google-chrome` |

Ubuntu ships Chromium only as a snap (the `chromium-browser` package is a
transitional package that installs it). The snap reads managed policy from the
Ubuntu path `/etc/chromium-browser/policies/managed`, not `/etc/chromium`, and can
write only below `~/snap/chromium`, so its suite profile lives there.
`hypersystem_desktop_browser_targets` holds these values and can be overridden.

The policy file is `30-hypersystem-desktop.json` and sets only
`WebAppInstallForceList`, one entry per app (`default_launch_container: window`,
no desktop shortcut). It never replaces another policy file, such as the
Bitwarden extension policy. Chromium uninstalls a policy-installed app when it
leaves the list, so removing a role removes its apps at the next browser start.

Managed policy on Linux applies to every profile of that browser. The suite
profile (`--user-data-dir`) keeps the suite's cookies and sign-ins apart from a
personal Chromium profile, but the force-installed apps also appear in other
profiles of the same browser. Use Firefox, or a different Chromium-family
browser, as the personal browser on a laptop where that matters.

## Launchers and the helper

The role writes `/usr/local/share/applications/hypersystem-desktop.desktop`
(the suite browser profile, named by `hypersystem_desktop_suite_name`) and one
`hypersystem-desktop-<id>.desktop` per app, and removes the launcher of an app
that is no longer selected. Each runs `/usr/local/bin/hypersystem-desktop`:

| Command | Does |
| --- | --- |
| `hypersystem-desktop open <id>` | Opens the app's `urls.web` in an app window of the suite profile |
| `hypersystem-desktop open <id> --local` | Opens the app's per-machine `urls.local` instead |
| `hypersystem-desktop browser [URL]` | Opens the suite profile itself (the first start installs the web apps) |
| `hypersystem-desktop list [--json]` | Lists the selected apps |
| `hypersystem-desktop doctor [--json] [--offline]` | Checks browser, policy, tunnel, DNS and HTTPS |

Each user gets their own suite profile under their home, so a shared laptop
keeps people apart. The helper reads `/etc/hypersystem-desktop/config.json`,
which the role writes. Catalog `icon` values are symbolic names; map them to
icon-theme names or image paths with `hypersystem_desktop_icon_map` (unmapped
icons use `hypersystem_desktop_default_icon`, `web-browser`).

## Optional hyperharness

The public role knows no hyperharness installer, so the hook takes the command
from variables and runs it as `dev_user` only while the `creates` path is
missing:

```yaml
hypersystem_desktop_hyperharness_enabled: true
hypersystem_desktop_hyperharness_install_argv: [/opt/hyperharness/install.sh, --release]
hypersystem_desktop_hyperharness_creates: "{{ dev_user_home }}/.local/bin/hyperharness"
```

## Turning it off

With `hypersystem_desktop_enabled: false` a host that had the feature loses the
policy file (in every target's directory), the launchers, the helper and
`/etc/hypersystem-desktop`; the browser package and each user's suite profile
stay. A host that never had it is not touched.

## Limits

- CI checks the task group in `--check` mode as an unprivileged user
  (`tests/test_hypersystem_desktop.py`); a real install is verified on hardware.
- A web app installs at the browser's next start in that profile; until then
  the launchers already work, because they open the app's URL in an app window.
