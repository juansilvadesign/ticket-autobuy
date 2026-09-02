"""The logged-in browser session.

🔴 Recon finding that made this module necessary: navigating to a listing URL
(`/r?event=...&c_anuncio=...`) while logged out redirects to `/entrar`. The checkout is
NOT a guest flow. Without a session there is no purchase, only a login page.

The session is stored as Playwright `storage_state` (cookies + localStorage), not as a
`cookies.txt`. Two reasons, and the second is the load-bearing one:

 1. `cookies.txt` is a curl/wget format and carries no localStorage, which a Next.js
    app may well use to hold auth state. A session restored from it can look logged in
    and fail at the first authenticated fetch.
 2. It keeps the sensitive artifact under one obvious name that `.gitignore` covers in
    THIS tree. A sibling project inherits no parent ignore rules -- that is exactly how
    a live cookies.txt once reached a public repo in this workspace.

⛔ Possession of this file IS possession of the account. It skips the password gate
entirely, so it is worth strictly more than the password.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from .errors import SessionError

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "session" / "buyticket.storage_state.json"

LOGIN_URL = "https://buyticketbrasil.com/entrar"

#: Where login is VERIFIED. The homepage, because it is a URL that has actually been
#: confirmed to exist (200) and to carry the signal below.
#: ⛔ An earlier version probed "/minhas-compras", inferred from the phrase "Minhas
#: Compras" in the site's own Terms. It is a 404 -- so a real, successful login was
#: reported as a failure and the session was discarded. A path read out of prose is a
#: guess; probe a URL you have watched respond.
HOME_URL = "https://buyticketbrasil.com/"


def path_from_env() -> Path:
    return Path(os.environ.get("AUTOBUY_SESSION", DEFAULT_PATH))


def save(context, path: Path | None = None) -> Path:
    """Persist the browser context's storage state with owner-only permissions."""
    path = Path(path or path_from_env())
    path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(path))
    # 0600 before anyone else on the box can read it. Best-effort: on some filesystems
    # (a Windows mount under WSL) chmod is a no-op, so this is a reduction of exposure,
    # never a guarantee -- do not treat the file as protected because this line ran.
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return path


def require(path: Path | None = None) -> Path:
    """The session file, or a refusal explaining how to make one.

    Checked at STARTUP, deliberately. The alternative -- discovering it at the moment
    the browser is redirected to /entrar -- is discovering it after the listing you
    wanted has been read, chosen and lost.
    """
    path = Path(path or path_from_env())
    if not path.exists():
        raise SessionError(
            f"no session at {path}.\n"
            f"Run:  python buy.py login\n"
            f"It opens a real browser; you log in by hand once, and the session is saved."
        )
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        # Corrupt is not the same as absent. Absent means "you have not logged in yet";
        # corrupt means we cannot tell what is in it, and silently re-logging-in would
        # hide a file that something else is mangling.
        raise SessionError(f"{path} is not valid JSON -- {e}. Delete it and re-run login.") from e
    if not state.get("cookies"):
        raise SessionError(f"{path} carries no cookies -- the login did not take. Re-run login.")
    return path


def auth_signals(page) -> dict:
    """Every signal that bears on "is this session logged in", named individually.

    Returns each one rather than a bare bool so a caller can say WHICH fired. A single
    collapsed boolean is how "the session expired" and "the page had not finished
    rendering" become the same message.

    ⭐ Only `entrar_links` actually measures authentication. Observed logged-OUT on
    2026-09-02: the homepage carries 2-3 `a[href*="/entrar"]` links, and its absence is
    what a logged-in header looks like. That is positive evidence -- not the absence of
    an error, which proves nothing here, since an expired session still returns a
    perfectly healthy 200.

    ⛔ `cookies` is NOT an auth signal, and must never be read as one. A logged-OUT
    context measured 0 cookies immediately after `goto` and **20** a few seconds later,
    once the seven analytics origins had fired. It is kept only as a "the browser really
    loaded a page" sanity check; anything that gates on a cookie COUNT is measuring
    Google Analytics.
    """
    url = page.url or ""
    try:
        entrar_links = page.locator('a[href*="/entrar"]').count()
    except Exception:                                      # noqa: BLE001
        entrar_links = -1                                  # could not read; not "zero"
    try:
        cookies = len(page.context.cookies())
    except Exception:                                      # noqa: BLE001
        cookies = -1
    return {
        "on_login_wall": "/entrar" in url or "/cadastrar" in url,
        "entrar_links": entrar_links,
        "cookies": cookies,
    }


def is_logged_in(page) -> bool:
    """True only when every signal agrees. `-1` means "unreadable", never "good"."""
    sig = auth_signals(page)
    return (not sig["on_login_wall"]
            and sig["entrar_links"] == 0
            and sig["cookies"] > 0)


def describe(sig: dict) -> str:
    """Human-readable reason, naming the signal that decided it."""
    if sig["on_login_wall"]:
        return "the browser is sitting on the login wall"
    if sig["entrar_links"] == -1 or sig["cookies"] == -1:
        return "the page could not be probed (still loading?)"
    if sig["entrar_links"] > 0:
        return f"the page still shows {sig['entrar_links']} 'Entrar' link(s)"
    if sig["cookies"] == 0:
        # Reached only when entrar_links is already 0, so this is "the page never
        # loaded", not "you are logged out".
        return "the browser loaded nothing at all (no cookies, no links)"
    return "logged in"
