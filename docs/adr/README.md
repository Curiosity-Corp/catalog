# Decision records

_Last verified: 2026-11-11 against `main`._

This is the index of the decisions that shape this collection. The catalog
has no ADRs of its own yet. Its decisions are recorded in three places, listed
below.

## Catalog ADRs (this directory)

None yet. A new catalog decision goes in this directory as
`NNNN-short-title.md` (four digits, starting at `0001`) with `Status`, `Date`,
`Deciders`, Context, Decision and Consequences, and gets a row here in the
same merge request. A decision that changes the public collection interface
(variables, defaults, supported platforms) also updates
[`../compatibility.md`](../compatibility.md) or
[`../configuration.md`](../configuration.md).

## Suite decisions that bind the catalog

These are recorded in the Curiosity / DreamLimited suite decision log
(`DreamLimited/Overview`, `ecosystem/decisions/`). They are internal, so this
public repository names them without linking.

| ADR | Decision | What it means here |
| --- | --- | --- |
| 0312 | Suite runtime topology: hosted apps, thin laptop clients, everything over Ziti | Laptops are thin clients; the catalog provides the machine layer and the opt-in Ziti tunnel (`ziti_enabled`). The laptop package is tracked in catalog#142 |
| 0324 | Fleet free tier plus our own extensions is the single device-management system | The catalog stays the Linux machine layer and installs fleetd (`fleetd_enabled`); Fleet does not deploy software |

## Design records kept as docs

These decisions predate an ADR convention and are recorded in the docs they
govern:

| Decision | Where |
| --- | --- |
| `catalog` is the scrubbed public base; organization-specific operations live in the private `catalog-internal` downstream | [README](../../README.md) |
| No private hostnames, secret-store names or private CAs in the public surface, enforced by test | [architecture: trust boundaries](../architecture.md#trust-boundaries), `tests/test_public_contract.py` |
| StreamStack is vendored as a squashed `git subtree` release, never edited in place | [architecture: vendored StreamStack](../architecture.md#vendored-streamstack), [streamstack.md](../streamstack.md) |
| Workstations are disposable: the storage role takes no scheduled snapshots and makes no backups | [storage.md](../storage.md) |
| Local, stateful update transaction with health gates, rollback and quarantine | [architecture](../architecture.md) |

## StreamStack ADRs (vendored)

StreamStack keeps its own decision log. The copies under
`roles/developer_workstation/files/streamstack/docs/adr/` come with the
vendored release and are changed only upstream.

| ADR | Title | Status |
| --- | --- | --- |
| [0001](../../roles/developer_workstation/files/streamstack/docs/adr/0001-encoder-generations-and-stream-transports.md) | Encoder generations, a Matroska relay, and the transports | accepted 2026-10-23; section 5 superseded by 0002 |
| [0002](../../roles/developer_workstation/files/streamstack/docs/adr/0002-whip-publishing.md) | WHIP publishing with a native client on aiortc's transports | accepted 2026-10-24 |
| [0003](../../roles/developer_workstation/files/streamstack/docs/adr/0003-hardware-profiles-and-capability-probing.md) | Hardware profiles and capability probing | proposed 2026-10-24 |
| [0004](../../roles/developer_workstation/files/streamstack/docs/adr/0004-virtual-camera-and-mic.md) | The virtual camera is a v4l2loopback node, the virtual microphone a PipeWire source | accepted 2026-10-24 |
| [0005](../../roles/developer_workstation/files/streamstack/docs/adr/0005-voice-denoise-default.md) | DeepFilterNet3 stays the default voice denoiser | accepted 2026-10-27 |
| [0006](../../roles/developer_workstation/files/streamstack/docs/adr/0006-app-shell.md) | The Console stays a local web app with the GTK tray | accepted 2026-10-27 |
| [0007](../../roles/developer_workstation/files/streamstack/docs/adr/0007-seat-policy.md) | Two users on one desk: the seat user owns audio | accepted 2026-11-01 |
| [0008](../../roles/developer_workstation/files/streamstack/docs/adr/0008-opencl-on-va-api-surfaces.md) | OpenCL filters on VA-API surfaces are not adopted | accepted 2026-10-28 |
| [0009](../../roles/developer_workstation/files/streamstack/docs/adr/0009-wave-xlr-control-path.md) | Wave XLR controls go over vendor USB control transfers | accepted 2026-11-01 |
