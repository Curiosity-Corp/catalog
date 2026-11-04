"""Meet rung (a): one sign-in attempt to Google with the CEO's own account, to reach Meet's prejoin page (#87).

Usage: G_USER=... G_PW=... python -I meetsignin.py <chrome-binary>
The two values come from the Bitwarden CLI into the environment of this one process (never a file, never printed; the
account name is replaced by <acct> in the output). No screenshot is kept because Google's pages show the account.
It tries the password once and stops: a second try on a stale password risks a lockout.
"""

import os
import sys

from playwright.sync_api import sync_playwright

chrome = sys.argv[1]
user = os.environ["G_USER"]
pw = os.environ["G_PW"]


def t(pg):
    s = " | ".join(x.strip() for x in pg.inner_text("body").splitlines() if x.strip())
    return s.replace(user, "<acct>")[:300]


with sync_playwright() as p:
    b = p.chromium.launch(
        executable_path=chrome,
        headless=True,
        args=["--use-fake-ui-for-media-stream", "--disable-blink-features=AutomationControlled"],
    )
    try:
        ctx = b.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent=b.new_page().evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome"),
        )
        pg = ctx.new_page()
        pg.goto("https://meet.google.com/landing", wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(3000)
        pg.fill("input[name=identifier], input#identifierId", user, timeout=8000)
        pg.keyboard.press("Enter")
        pg.wait_for_timeout(5000)
        print("after email:", pg.url[:90], "|", t(pg))
        if pg.locator("input[type=password]").count():
            pg.fill("input[type=password]", pw)
            pg.keyboard.press("Enter")
            pg.wait_for_timeout(7000)
            print("after pw:", pg.url[:90], "|", t(pg))
    finally:
        b.close()
