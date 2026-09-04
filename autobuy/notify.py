"""Hand the Pix payload to a human, fast.

This is the single most consequential module in the tool. The bot reserves a ticket and
the site holds it for ⏰ ~10 MINUTES -- measured on the first real order, not the 30
this file assumed -- and if the Pix code does not reach Juan inside that window the
reservation lapses and the ticket returns to sale. A third of the assumed margin is the
difference between "he'll see it" and a lapsed hold. A notifier that fails
quietly here does not degrade the feature -- it deletes it, while leaving a log that
says the purchase succeeded.

So, two rules, both inherited from price-watcher's notifier:

 * The code is printed to STDOUT **before** any network call. Telegram is the
   convenient path, not the load-bearing one; if you are at the machine, the copia-e-
   cola is already on your screen whatever the network does.
 * A delivery failure is RAISED, never swallowed. Exit 3 has to keep meaning "you were
   not told".

Stdlib only, like price-watcher -- the browser is this project's one dependency and
there is no reason for the notifier to add a second.
"""

from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

TIMEOUT_S = 20
API_ROOT = "https://api.telegram.org"


class NotifyError(RuntimeError):
    """A recipient was not reached. Always raised; never logged-and-continued."""


def _post_multipart(url: str, fields: dict[str, str], files: dict[str, Path]) -> dict:
    """`sendPhoto` needs multipart and stdlib has no client for it, so: build it.

    Kept small on purpose -- one boundary, no streaming, no retry. A retry here could
    send the same Pix code twice, which is harmless, but it could also mask a token
    problem until the one night it matters.
    """
    boundary = "----autobuy" + uuid.uuid4().hex
    body = bytearray()
    for key, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        body += f"{value}\r\n".encode()
    for key, fpath in files.items():
        ctype = mimetypes.guess_type(fpath.name)[0] or "application/octet-stream"
        body += f"--{boundary}\r\n".encode()
        body += (f'Content-Disposition: form-data; name="{key}"; '
                 f'filename="{fpath.name}"\r\n').encode()
        body += f"Content-Type: {ctype}\r\n\r\n".encode()
        body += fpath.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        url, data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.load(resp)


def _call(token: str, method: str, params: dict) -> dict:
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{API_ROOT}/bot{token}/{method}", data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except Exception:                                  # noqa: BLE001
            raise NotifyError(f"{method}: HTTP {e.code} with no JSON body") from e
        raise NotifyError(f"{method}: telegram refused ({body.get('error_code')}) "
                          f"{body.get('description', '')}".strip()) from e


# `sendMessage` with parse_mode would need every ticket name escaped -- and a name with
# an underscore or an asterisk in it would fail the send for EVERY recipient at once.
# Plain text costs nothing here and cannot be broken by a third-party string.
def send_pix(token: str, chat_id: str, *, label: str, item: str, price_brl: str,
             pix_code: str, qr_png: Path | None = None,
             expires_note: str = "~10 min") -> None:
    """Print the Pix payload, then deliver it. Raises `NotifyError` if it did not land."""
    text = (
        f"🎟  RESERVED — pay to confirm\n"
        f"{label}\n"
        f"{item}  ·  {price_brl}\n"
        f"⏳ hold expires in {expires_note}; unpaid, the ticket returns to sale.\n\n"
        f"Pix copia-e-cola:\n{pix_code}"
    )
    # Before the network. See module docstring.
    print("\n" + "=" * 68, flush=True)
    print(text, flush=True)
    print("=" * 68 + "\n", flush=True)

    if not token or not chat_id:
        raise NotifyError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing -- the Pix code "
                          "was printed above and NOT delivered.")

    _call(token, "sendMessage", {"chat_id": chat_id, "text": text,
                                 "disable_web_page_preview": "true"})
    if qr_png and qr_png.exists():
        # The QR is a convenience on top of a code that has already been delivered, so
        # its failure must not raise: losing the picture after the copia-e-cola landed
        # would turn a complete success into exit 3.
        try:
            _post_multipart(f"{API_ROOT}/bot{token}/sendPhoto",
                            {"chat_id": chat_id, "caption": f"{label} — {price_brl}"},
                            {"photo": qr_png})
        except Exception as e:                              # noqa: BLE001
            print(f"⚠️  QR image not delivered ({e}); the copia-e-cola above did land.",
                  flush=True)

    # ⏰ Sent LAST, so it is the NEWEST message -- the one sitting above the keyboard
    # while the hold runs down. Like the QR, it rides on top of a code that already
    # landed in the message above, so its failure must not turn a complete success
    # into exit 3.
    try:
        send_code_alone(token, chat_id, pix_code)
    except Exception as e:                                  # noqa: BLE001
        print(f"⚠️  tap-to-copy message not delivered ({e}); the code above did land.",
              flush=True)


def _utf16_len(s: str) -> int:
    """Telegram entity offsets and lengths are counted in UTF-16 code units, not in
    Python characters. A Pix payload is ASCII today, so the two agree -- but the day
    one is not, a `len()` here would silently truncate the tap-to-copy region."""
    return len(s.encode("utf-16-le")) // 2


def send_code_alone(token: str, chat_id: str, pix_code: str) -> None:
    """The payload ALONE, as a tap-to-copy `code` entity.

    ⏰ Why this is a second message rather than better formatting on the first: on the
    first order that was actually paid (#6999ZUKS, 2026-09-04) the code arrived inside
    the reservation message and had to be hand-SELECTED to be copied, against a
    ~10-minute hold. A message holding nothing but the payload is one tap. The
    reservation message is unchanged -- this is added next to it, not instead of it.

    ⛔ Still no `parse_mode` -- see the note above `send_pix`. `entities` carries the
    formatting out of band, so the text goes VERBATIM and no character in the payload
    (a Pix EMV string is full of `*`, `.` and `/`) can break the send.
    """
    _call(token, "sendMessage", {
        "chat_id": chat_id, "text": pix_code,
        "entities": json.dumps([{"type": "code", "offset": 0,
                                 "length": _utf16_len(pix_code)}]),
        "disable_web_page_preview": "true"})


def send_text(token: str, chat_id: str, text: str) -> None:
    print(text, flush=True)
    if not token or not chat_id:
        raise NotifyError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing")
    _call(token, "sendMessage", {"chat_id": chat_id, "text": text,
                                 "disable_web_page_preview": "true"})
