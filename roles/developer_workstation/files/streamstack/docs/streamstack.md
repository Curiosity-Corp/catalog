# Host setup

What the desktop user needs from the host beyond the Python package. The
workstation role (Ansible) owns the machine; these files are what StreamStack
ships so the role, `make install` and a manual install all agree.

## Logitech Litra lights (hidraw)

`packaging/udev/60-streamstack-litra.rules` lets the logged-in user open the
Litra hidraw nodes without root. It matches exact `046d` vendor and product
IDs, taken from [timrogers/litra-rs](https://github.com/timrogers/litra-rs/blob/main/src/lib.rs):

| Product ID | Device |
| --- | --- |
| `c900` | Litra Glow |
| `c901` | Litra Beam |
| `b901` | Litra Beam (second product ID) |
| `c903` | Litra Beam LX |

Every rule carries `TAG+="uaccess"`, `GROUP="plugdev"`, `MODE="0660"`, the
same pattern as the role's `60-logitech-litra.rules` (which today lists only
`c901`). Other Logitech devices, notably the Lightspeed receiver `046d:c539`,
are not matched and keep their default permissions.

`make install` puts the file in `$(PREFIX)/lib/udev/rules.d`; the role should
copy the same file to `/etc/udev/rules.d` and run
`udevadm control --reload && udevadm trigger --subsystem-match=hidraw` (a
handler, only when the file changed). `tests/test_udev.py` pins the ID list.
The Lighting page itself is issue #21.

## Audio interfaces

This host uses the Elgato Wave XLR; the other properties use a Focusrite
Scarlett 2i2. Both are first-class rigs (issues #18 and #20).
