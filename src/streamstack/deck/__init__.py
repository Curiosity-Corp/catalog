"""Elgato Stream Deck support: profiles, the action catalog, key images and the device service.

Everything here except `render` and `service` is plain Python, so the Console can edit and preview a deck on a machine
with no Stream Deck and no extra installed. `render` needs Pillow and `service` needs the `streamdeck` library
(`pip install 'streamstack[deck]'`). See docs/deck.md.
"""
