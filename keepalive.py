#!/usr/bin/env python3
"""Keep the BuyTicket session warm, and SHOUT the moment it is not. Buys nothing, ever.

    python keepalive.py

## Why this exists

2026-09-06, 10:34-11:10 BRT: `Gramado || Meia Até 21` sat at **R$ 165,00** on the 11/09
night -- R$35 under the ceiling, qty 26, for 36 minutes. `autobuy` fired on it four
times and every one died the same way:

    stopped: not authenticated at https://buyticketbrasil.com/entrar?... --
    the browser is sitting on the login wall.

The tool worked perfectly. The session was dead, and nothing said so until a dip arrived
to discover it. That is the shape of the problem: **`autobuy` can only notice a dead
session at buy time**, which is the one moment the discovery is worthless. Between dips
it is structurally blind, and the observed session lifetime is ~2 h against a plan that
has to cover five days.

This script closes both halves on a `*/20` cron:

  1. **Warm.** It loads the homepage with the saved session and writes the refreshed
     `storage_state` back. ⚠️ This is a HYPOTHESIS under test, not a known fix: it works
     only if BuyTicket's session TTL is IDLE-based. If the TTL is absolute, nothing here
     extends anything and leg 2 is what earns the file. Deaths were observed at ~1 h 56 m
     and ~2 h 20 m of a session that no one was touching, which is consistent with both.
  2. **Detect.** A dead session becomes a Telegram alert within ~20 minutes whether or
     not the market dips -- which is the part `autobuy` cannot do at all.

⛔ It is a probe, not a login. Establishing a session still means a human at
`buy.py login` typing a password into a real browser; automating that would mean storing
the password, and a scripted login is the least ordinary-looking thing this tool could
do at the moment it most wants to look ordinary (RECON §6, antifraud).

Exit codes, matching `buy.py`'s grammar:
    0  session is alive, or a buy holds the lock (skipped)
    3  the session is DEAD (or could not be read) -- alerted, throttled
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from autobuy import checkout, notify, runner, session                # noqa: E402

#: ⛔ Its OWN throttle key, deliberately not `autobuy`'s `session_dead`. Sharing one
#: would let a 20-minute probe eat the throttle slot that the buy path needs for the
#: message that actually names a lost dip -- the two failures deserve separate voices.
ALERT_KEY = "session_dead_probe"

#: How long a page gets to render before the signals are read. `open_listing` uses the
#: same 2 s for the same reason: the app re-renders after navigation, and reading the
#: header too early counts `Entrar` links that a logged-in session is about to remove.
RENDER_MS = 2000


def probe(page) -> tuple[bool, dict]:
    """`(logged_in, signals)` for a page already sitting on the homepage.

    ⭐ Returns the signals, not just the verdict, so the log can say WHICH one decided.
    `entrar_links: -1` means "could not be read" and is treated as NOT logged in -- an
    unreadable probe is never evidence of health.
    """
    sig = session.auth_signals(page)
    return session.is_logged_in(page), sig


def alert_dead(state: dict, sig: dict) -> bool:
    """Telegram that the session is dead. True if a message was actually sent.

    ⚠️ The throttle stamp is written BEFORE the send, matching `buy._alert_session_dead`:
    a delivery failure therefore consumes the slot. Kept identical rather than improved,
    because two alert paths that throttle differently is how you end up unable to say
    why a message did not arrive.
    """
    if not runner.should_alert(state, ALERT_KEY):
        return False
    runner._write_state(state)
    notify.send_text(
        os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        os.environ.get("TELEGRAM_CHAT_ID", ""),
        f"\U0001f534 BuyTicket session is DEAD\n\n"
        f"{session.describe(sig)}.\n\n"
        f"Nothing can be bought for 11/09 until you re-login.\n"
        f"Run:  python buy.py login\n\n"
        f"(Found by the keep-alive probe, not by a dip -- so nothing has been "
        f"missed yet.)")
    return True


def run_probe(context, page, state: dict) -> tuple[bool, dict, bool]:
    """The whole decision, with no browser plumbing around it: probe, save only on a
    positive login, alert only when dead. Returns `(ok, signals, alerted)`.

    Split out from `main` deliberately -- everything that can be WRONG here is in these
    ten lines, and a guard reachable only through a live Chromium is a guard nothing
    asserts.
    """
    ok, sig = probe(page)
    if ok:
        # ⛔ Saved ONLY on a positive login. `is_logged_in` is False both for a dead
        # session and for a page that never rendered (`entrar_links: -1`), and writing
        # a half-loaded context's state over a good file would destroy a WORKING
        # session to record a network hiccup. The one write that must never happen in
        # this script is the one that makes things worse than not running it.
        session.save(context)
        return True, sig, False
    return False, sig, alert_dead(state, sig)


def main() -> int:
    # `.env` is PARSED, never sourced -- it holds a bot token, and a value carrying a
    # backtick becomes shell execution the moment it is sourced.
    import buy                                                       # noqa: PLC0415
    buy._load_env(HERE / ".env")

    # ⛔ Same exclusive lock as the buy. Two Playwright contexts writing the same
    # `storage_state` file is a corrupted session, and a buy in flight is itself the
    # strongest possible proof the session is being exercised -- so a held lock means
    # skip, never wait.
    lock = runner.acquire_lock()
    if lock is None:
        return 0

    now = runner.now_local()
    try:
        from playwright.sync_api import sync_playwright                # noqa: PLC0415

        state_path = session.require()          # absent/corrupt session: raises, exit 3
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(storage_state=str(state_path))
                page = context.new_page()
                # ⛔ The project's own navigator: it retries once and waits on
                # `domcontentloaded`, never `load` -- this site carries seven analytics
                # origins and one hanging tracker would hold a navigation that has been
                # usable for ten seconds.
                checkout._goto(page, session.HOME_URL)
                page.wait_for_timeout(RENDER_MS)
                ok, sig, sent = run_probe(context, page, runner._read_state())
            finally:
                browser.close()

        if ok:
            print(f"{now:%m-%d %H:%M} {now.tzname()}  ✅ alive  {sig}  "
                  f"(session rewritten)", flush=True)
            return 0

        print(f"{now:%m-%d %H:%M} {now.tzname()}  🔴 DEAD  {session.describe(sig)}  "
              f"{sig}  alerted={sent}", flush=True)
        return 3
    except Exception as e:                                            # noqa: BLE001
        # ⚠️ Fails LOUD but never fatally: this runs every 20 minutes beside an armed
        # buy cron, and a keep-alive that crashes silently is a keep-alive that has
        # stopped keeping anything alive. The message names the class, because "which
        # thing broke" is the whole question when a probe stops reporting.
        print(f"{now:%m-%d %H:%M} {now.tzname()}  ⚠️  probe failed: "
              f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return 3
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
