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
   app host (web and ssh) is inside `ziti_intercept_domains`, enrols the
   laptop's Ziti device identity from a one-time token when it is not enrolled
   yet, and checks that the tunnel offers the suite's Ziti services;
4. installs one Chromium-family browser and force-installs every selected app as
   a web app (PWA) through managed browser policy (`WebAppInstallForceList`);
5. writes one menu launcher per app plus a launcher for the suite browser
   profile, the `hypersystem-desktop` helper that opens them and checks the
   install, and `ssh <app>` entries for the apps with a terminal UI;
6. optionally installs hyperharness for the primary user.

The role never ships a catalog address, an app host, a Ziti identity, a Ziti
service name, an enrollment token or a credential. Those come from host or
group variables, Ansible Vault, or a private inventory.

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

# The laptop's device identity and the services it must reach (see "Ziti").
hypersystem_desktop_ziti_identity_name: laptop-0042
hypersystem_desktop_ziti_services: [notebooks]
# hypersystem_desktop_ziti_enrollment_jwt comes from Vault, only for the first run:
#   ansible-playbook ... -e @~/secrets/laptop-0042-enrollment.yml
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
`status`; `purpose`, `icon`, `ssh` and `urls.local` are optional here. Fields
the desktop does not use (`owner_group`, `repo`, `urls.brand`) are ignored. A
malformed or duplicate entry fails the run and names the entry; with
`hypersystem_desktop_catalog_strict: false` it is skipped with a warning
instead.

`urls.web` is an absolute http or https URL, as in the suite schema. What the
laptop does with it depends on the address:

| `urls.web` | Web app install | Launcher | Ziti |
| --- | --- | --- | --- |
| `https://...` | yes | yes | host must be intercepted |
| `http://` on the loopback (`127.0.0.1`, `::1`, `localhost`): a per-machine app such as hyperharness | yes (the loopback is a secure origin) | yes | not used |
| `http://` anywhere else | no: browsers install web apps only from a secure origin, so the run reports it | yes, opens in an app window | host must be intercepted |

A per-machine app is installed only when its id is in
`hypersystem_desktop_local_apps`, because its address is a service on the
laptop that may not be there. The default lists `hyperharness` when
`hypersystem_desktop_hyperharness_enabled` is true and nothing otherwise; the
run names the per-machine apps it skips.

The compiled document published next to the suite's catalog source holds every
app and is filtered here by `hypersystem_desktop_roles`. HyperSystem Home's
`GET /suite/v1/catalog` already filters by the caller's realm roles; when the
source is that endpoint (with the person's token in
`hypersystem_desktop_catalog_headers`), set
`hypersystem_desktop_catalog_prefiltered: true` so the role test is skipped,
as the bar does.

| Variable | Default | Meaning |
| --- | --- | --- |
| `hypersystem_desktop_catalog_src` | `""` | https URL or absolute path on the host of the compiled catalog |
| `hypersystem_desktop_catalog_sha256` | `""` | Optional SHA-256 the fetched document must match |
| `hypersystem_desktop_catalog_headers` | `{}` | Extra request headers, for example a registry token from Vault; never logged |
| `hypersystem_desktop_apps` | `[]` | The apps as variables instead of a catalog (`roles` defaults to `[]`, `status` to `ga`) |
| `hypersystem_desktop_roles` | `[]` | The person's suite roles |
| `hypersystem_desktop_statuses` | `[beta, ga]` | Statuses that are installed; `planned` and `retired` are never usable |
| `hypersystem_desktop_allow_empty` | `false` | Allow a run that selects no app (otherwise it fails, usually a roles typo) |
| `hypersystem_desktop_catalog_prefiltered` | `false` | The source already filtered the apps by the person's roles; skip the role test |
| `hypersystem_desktop_local_apps` | `[hyperharness]` when hyperharness is installed, else `[]` | Ids of per-machine (loopback) apps to install |

Exactly one of `hypersystem_desktop_catalog_src` and `hypersystem_desktop_apps`
must be set. The URL is read in check mode too (a read-only GET), so a dry run
previews the real app list.

## Ziti

The `ziti` task group owns the tunnel: `ziti_enabled` installs the
`ziti-edge-tunnel` package and its OIDC session hooks, `ziti_intercept_domains`
routes the suite domains through it (split DNS on the thin-client profile), and
the tunnel health guard restarts a half-up tunnel. `hypersystem-desktop`
installs none of that and never changes the tunnel's DNS. With
`hypersystem_desktop_require_ziti: true` (the default) a run fails, with the
setting to change, when:

- `ziti_enabled` is false and no `ziti_service_name` unit exists (a hand-managed
  tunnel counts);
- an app host, or the `ssh` host of an app with a terminal UI, is not inside
  `ziti_intercept_domains` (`suite.example`, `.suite.example` and
  `*.suite.example` all cover `app.suite.example`). Per-machine apps on the
  loopback are exempt.

It also suggests adding an app host to `ziti_health_check_probe_hosts`, so the
health guard notices when app names stop resolving. Set
`hypersystem_desktop_require_ziti: false` only for apps that are reachable
without Ziti.

### Device identity

A laptop reaches the suite with a device identity of its own, which the Ziti
administrator creates and grants dial policies to. Name it with
`hypersystem_desktop_ziti_identity_name`; the identity file is
`hypersystem_desktop_ziti_identity_dir/<name>.json`
(`/opt/openziti/etc/identities` by default, where the tunnel loads it).

When that file is missing, the run enrols it from
`hypersystem_desktop_ziti_enrollment_jwt`, the one-time enrollment token the
administrator issued for this laptop:

1. it checks that `ziti-edge-tunnel` is installed (`ziti_enabled`, tag `ziti`);
2. it writes the token to a root-only temporary file (never logged), runs
   `ziti-edge-tunnel enroll --jwt <file> --identity <identity file>`, and always
   removes the token file, also when enrollment fails (a failed enrollment
   leaves no partial identity file);
3. it gives the identity file to `root:ziti` with mode `0660`, so the tunnel can
   renew its certificate, and asks for the guarded tunnel restart (the
   `Restart ziti-edge-tunnel` handler, which waits while an ssh session over the
   tunnel is live).

A run with the identity already enrolled needs no token, so the token is passed
once, from Ansible Vault or a file outside Git:

```bash
ansible-playbook ... -e @laptop.yml -e @~/secrets/laptop-0042-enrollment.yml \
  --tags ziti,hypersystem-desktop
```

Without an identity name, the tunnel's identities are managed elsewhere (the
OIDC identities of the `ziti` task group, or by hand) and nothing is enrolled.
Turning hypersystem-desktop off keeps the identity: it belongs to the tunnel.

### Suite services

`hypersystem_desktop_ziti_services` lists the Ziti service names the laptop must
reach: the suite catalog's `ziti_services` of the selected apps. The compiled
catalog document does not carry them, so they are an input. When the tunnel is
running and the identity was not enrolled in this run, the run asks the tunnel
(`ziti-edge-tunnel tunnel_status`, read-only, also in check mode), waits up to
`hypersystem_desktop_ziti_service_wait` seconds (30) for them, and fails naming
the missing services. Set `hypersystem_desktop_ziti_require_services: false` to
install the apps while the services are still being set up; `doctor` keeps
reporting them. After a new enrollment the tunnel loads the identity only after
its restart, so the run leaves the check to `doctor`.

### doctor

`hypersystem-desktop doctor` checks the same chain on the running laptop: the
tunnel unit is active; the device identity is loaded and offers every service in
`hypersystem_desktop_ziti_services`; every app host resolves into the tunnel's
DNS range (`hypersystem_desktop_ziti_dns_ip_range`, default `100.64.0.0/10`);
and every app answers (any HTTP status, a sign-in redirect included, proves the
path; a 5xx or a connection error fails, except for a per-machine app, whose
service may simply not be running, which is a warning). `--offline` skips DNS
and HTTP, `--json` prints a machine-readable report, and the exit code is 1
when a check fails.

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
`WebAppInstallForceList`, one entry per app on a secure origin
(`default_launch_container: window`, no desktop shortcut). It never replaces another policy file, such as the
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
| `hypersystem-desktop doctor [--json] [--offline]` | Checks browser, policy, tunnel, identity and services, DNS and HTTP(S) |

Each user gets their own suite profile under their home, so a shared laptop
keeps people apart. The helper reads `/etc/hypersystem-desktop/config.json`,
which the role writes. Catalog `icon` values are symbolic names; map them to
icon-theme names or image paths with `hypersystem_desktop_icon_map` (unmapped
icons use `hypersystem_desktop_default_icon`, `web-browser`).

## Terminal UIs over ssh

For every selected app with an `ssh` host, the role writes a `Host <app id>`
entry to `hypersystem_desktop_ssh_config_path`
(`/etc/ssh/ssh_config.d/30-hypersystem-desktop.conf`, read by the Debian and
Ubuntu `ssh_config`), so `ssh <app>` opens the app's terminal UI through the
tunnel. The file is removed when no selected app has one; set the path to `""`
to leave ssh alone. Sign-in to those endpoints with a suite SSH certificate
needs the suite's certificate sign-in helper, which this package does not
install yet.

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
policy file (in every target's directory), the launchers, the ssh entries, the
helper and `/etc/hypersystem-desktop`; the browser package, the Ziti device
identity and each user's suite profile stay. A host that never had it is not
touched.

## Limits

- CI checks the task group in `--check` mode as an unprivileged user
  (`tests/test_hypersystem_desktop.py`), with stub `ziti-edge-tunnel`,
  `systemctl` and `getent` commands; a real enrollment and install are verified
  on hardware.
- The suite services and their dial policies, and each app's sign-in client, are
  set up on the Ziti controller and the identity provider, not by this role.
  Until they exist, enrollment works but the services check fails (or, with
  `hypersystem_desktop_ziti_require_services: false`, `doctor` reports them).
- A web app installs at the browser's next start in that profile; until then
  the launchers already work, because they open the app's URL in an app window.
