"""Tests for the notifier.

⛔ This module is the one whose quiet failure DELETES the feature: a reservation whose
Pix code does not reach a human inside a ~10-minute hold bought nothing. So what is
pinned here is not formatting -- it is which failures are allowed to be survivable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autobuy import notify                                            # noqa: E402
from autobuy.notify import NotifyError                                # noqa: E402

PIX = ("00020101021226910014br.gov.bcb.pix2569api.developer.btgpactual.com/pc/p/v2/"
       "8d59fb6d88824c34a026430ae2bb49d85204000053039865802BR5925BUYTICKET "
       "DESENVOLVIMENTO6008Brasilia62070503***6304B079")


@pytest.fixture
def sent(monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(notify, "_call",
                        lambda tok, method, params: calls.append((method, params)) or {})
    return calls


def test_the_reservation_message_is_unchanged_and_still_carries_the_code(sent):
    """⚠️ The tap-to-copy message is ADDED next to the reservation notice, never
    instead of it. If the new one is ever lost, the code must still be readable in the
    message that has always carried it."""
    notify.send_pix("t", "c", label="Night", item="Gramado || Inteira",
                    price_brl="R$ 220,00", pix_code=PIX)
    first = sent[0][1]["text"]
    assert "RESERVED" in first and "Night" in first and PIX in first


def test_the_code_is_also_sent_ALONE_as_a_tap_to_copy_entity(sent):
    """⏰ #6999ZUKS, 2026-09-04: the first order actually paid. The code arrived inside
    the reservation message and had to be hand-selected to copy, on a ~10-min hold."""
    notify.send_pix("t", "c", label="Night", item="i", price_brl="R$ 220,00",
                    pix_code=PIX)
    assert len(sent) == 2, "the payload must also arrive in a message of its own"
    method, params = sent[-1]
    assert method == "sendMessage"
    assert params["text"] == PIX, "the message must hold NOTHING but the payload"
    ent = json.loads(params["entities"])
    assert ent == [{"type": "code", "offset": 0, "length": len(PIX)}]


def test_it_is_sent_LAST_so_it_is_the_newest_message(sent):
    """The message above the keyboard while the hold runs down."""
    notify.send_pix("t", "c", label="Night", item="i", price_brl="R$ 220,00",
                    pix_code=PIX)
    assert sent[-1][1]["text"] == PIX


def test_no_parse_mode_anywhere(sent):
    """⛔ A Pix payload is full of `*`, `.` and `/`, and a ticket name can hold `_`.
    parse_mode would make a third-party string able to fail the send outright; the
    formatting rides in `entities` instead, out of band, with the text VERBATIM."""
    notify.send_pix("t", "c", label="Night_A *deal*", item="i", price_brl="R$ 1,00",
                    pix_code=PIX)
    assert all("parse_mode" not in params for _, params in sent)


def test_a_lost_tap_to_copy_message_does_NOT_delete_a_landed_reservation(monkeypatch):
    """⚠️ Same rule as the QR image: it rides on top of a code that already landed, so
    its failure must not turn a complete success into exit 3."""
    calls = []
    def _flaky(tok, method, params):
        calls.append(params)
        if params.get("text") == PIX:
            raise NotifyError("telegram refused")
        return {}
    monkeypatch.setattr(notify, "_call", _flaky)
    notify.send_pix("t", "c", label="Night", item="i", price_brl="R$ 1,00",
                    pix_code=PIX)                          # must NOT raise
    assert len(calls) == 2, "it must still have TRIED the second message"


def test_a_lost_RESERVATION_message_still_raises(monkeypatch):
    """⛔ The load-bearing delivery is unchanged: exit 3 keeps meaning 'you were not
    told'. Only the extras are survivable."""
    def _dead(tok, method, params):
        raise NotifyError("telegram refused")
    monkeypatch.setattr(notify, "_call", _dead)
    with pytest.raises(NotifyError):
        notify.send_pix("t", "c", label="Night", item="i", price_brl="R$ 1,00",
                        pix_code=PIX)


def test_missing_credentials_still_raise_before_any_send(sent):
    with pytest.raises(NotifyError, match="missing"):
        notify.send_pix("", "", label="Night", item="i", price_brl="R$ 1,00",
                        pix_code=PIX)
    assert not sent


def test_entity_length_is_utf16_not_python_characters():
    """A non-BMP character is ONE Python char and TWO UTF-16 units. If a payload ever
    carries one, `len()` would end the tap-to-copy region early -- silently."""
    assert notify._utf16_len("abc") == 3
    assert notify._utf16_len("a\U0001F600b") == 4
