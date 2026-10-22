"""Audio-interface support: detect the interface on the desk by USB id and drive what its hardware offers.

`usb` finds the device from /sys, `profiles` maps a VID:PID to a driver, `scarlett` drives Focusrite Scarlett
interfaces through the in-kernel scarlett2 ALSA controls (via `alsa`), and `wavexlr` speaks the Elgato Wave XLR's
vendor USB control protocol. Nothing here is imported until the Console asks for it, and every probe is read-only.
"""
