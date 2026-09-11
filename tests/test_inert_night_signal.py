"""A night that is switched OFF must not look like a night where nothing dipped.

⛔🔴 The gap these tests pin cost the whole 11/09 event night, at a R$250,00 ceiling,
with the session alive and the poller healthy the entire time:

    2026-09-10 14:24 BRT  an OrderMayExistError disarmed the night (fail-closed, correct)
    2026-09-11 15:39 BRT  a human finally looked -- 25 hours and ~1,500 cron runs later

`state/autobuy.log` gained **0 bytes** across that entire span. `keepalive.log` shows
`✅ alive` every 20 minutes throughout, and price-watcher kept writing readings every
minute. **67 in-window polls on 11/09 sat at or under the R$250 ceiling**, among them
four consecutive minutes at R$220,00 with qty 178, and 2 polls at R$220,00 qty 194.

Two bare `continue`s produced that silence, and the second one was never even reached:

    if runner.already_fired(state, cfg.target_id):   # ← structurally UNREACHABLE:
        continue                                     #   config.load raises first,
                                                     #   because the disarm already
                                                     #   set buy.enabled: false

⭐ Every test here fails on the pre-patch tree. The defect was ABSENCE of output, and
only a test that demands the output can pin it. Same shape, one gate higher up, as
`test_rejected_signal.py` -- and that is the lesson: fixing ONE silent gate does not
generalise to the gates above it.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import buy as buy_mod                                                 # noqa: E402
from autobuy import runner                                            # noqa: E402


def _world(tmp_path, monkeypatch, *, buy_enabled: bool, ledger: dict | None,
           label: str = "Night N"):
    """One target plus a dip under its ceiling. `buy_enabled` and `ledger` are the two
    knobs the production failure turned: disarmed, with an UNCONFIRMED row."""
    targets = tmp_path / "targets"; targets.mkdir()
    hist = tmp_path / "history"; hist.mkdir()
    state = tmp_path / "state"; state.mkdir()

    (targets / "n.json").write_text(json.dumps({
        "id": "n", "label": label,
        "params": {"event_slug": "e", "data_millis": 1, "evento_local": "l"},
        "buy": {"enabled": buy_enabled, "max_price_brl": 250.0, "quantity": 1,
                "sector": ["Gramado"], "entry_class": None}}), encoding="utf-8")
    # R$220,00 qty 178 -- the row that really sat there for four straight minutes
    # at 11:05-11:13 BRT on 11/09 while the tool was disarmed.
    (hist / "n.jsonl").write_text(json.dumps({
        "target_id": "n", "item": "Gramado || Inteira", "price_cents": 22000,
        "quantity": 178, "available": True,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "extra": {"sector": "Gramado", "entry_class": "Inteira"}}) + "\n",
        encoding="utf-8")
    (hist / "cron.log").write_text("alive\n", encoding="utf-8")

    monkeypatch.setattr(runner, "STATE_DIR", state)
    monkeypatch.setattr(runner, "STATE_PATH", state / "autobuy.json")
    monkeypatch.setattr(runner, "LOCK_PATH", state / "autobuy.lock")
    # ⛔ Pinned, never `datetime.now`. The buy window is 10:00-04:00 BRT, so a test
    # that borrowed the real clock would pass all day and fail between 04:00 and
    # 10:00 -- a suite that is green depending on when you run it is not a gate.
    # 15:00 on 11/09 is the hour the real miss was found.
    monkeypatch.setattr(runner, "now_local",
                        lambda: datetime(2026, 9, 11, 15, 0, tzinfo=runner.TZ))
    if ledger is not None:
        (state / "autobuy.json").write_text(json.dumps(ledger), encoding="utf-8")

    sent: list[str] = []
    monkeypatch.setattr(buy_mod.notify, "send_text",
                        lambda tok, chat, text: sent.append(text))
    monkeypatch.setattr(buy_mod.session, "require", lambda *a, **k: tmp_path / "s.json")

    def _must_not_buy(*a, **k):
        raise AssertionError("a closed night must never reach the checkout")
    monkeypatch.setattr(buy_mod, "_execute_buy", _must_not_buy)

    args = SimpleNamespace(targets=str(targets), history=str(hist),
                           fields=str(tmp_path / "f.json"), verbose=False)
    return args, sent


UNCONFIRMED = ("UNCONFIRMED -- the checkout raised after the final click. "
               "Verify at /ingressos -> Comprados before re-arming.")


def _row(order_url, *, at="2026-09-10T17:24:25+00:00", price=22000):
    return {"fired": {"n": {"at": at, "price_cents": price,
                            "item": "Gramado || Inteira", "order_url": order_url}}}


# ── the 25-hour silence itself ─────────────────────────────────────────────────

def test_a_disarmed_night_with_a_ledger_row_SAYS_SO(tmp_path, monkeypatch, capsys):
    """⛔🔴 THE regression. Pre-patch this run printed nothing at all: `config.load`
    raised ConfigError on `buy.enabled: false` and the bare `continue` swallowed it."""
    args, _ = _world(tmp_path, monkeypatch, buy_enabled=False,
                     ledger=_row(UNCONFIRMED))
    assert buy_mod.cmd_autobuy(args) == 0
    out = capsys.readouterr().out
    assert out.strip(), "a closed night must not be byte-identical to a calm market"
    assert "Night N" in out, "the message must name the night"
    assert "CLOSED" in out, "it must say the night is closed"
    assert "UNCONFIRMED" in out, "it must distinguish a maybe-order from a real buy"


def test_the_ledger_is_read_even_though_config_load_would_raise_first(
        tmp_path, monkeypatch, capsys):
    """⛔🔴 The structural bug under the silence: `already_fired` sat BELOW
    `config.load`, and `disarm_target` + `record_fired` fire together -- so on every
    run after a disarm, `config.load` raised and the ledger check never executed.
    A guard that cannot be reached is a declaration nothing asserts."""
    args, _ = _world(tmp_path, monkeypatch, buy_enabled=False,
                     ledger=_row(UNCONFIRMED))
    buy_mod.cmd_autobuy(args)
    assert "CLOSED" in capsys.readouterr().out, (
        "the ledger must be consulted BEFORE the buy block is validated")


def test_an_unconfirmed_row_reaches_telegram(tmp_path, monkeypatch):
    """The log makes a run diagnosable afterwards; only the message makes it noticed
    at the time. 25 hours is how long the log-only version went unread."""
    args, sent = _world(tmp_path, monkeypatch, buy_enabled=False,
                        ledger=_row(UNCONFIRMED))
    buy_mod.cmd_autobuy(args)
    assert len(sent) == 1, "an unresolved reservation must reach the human"
    assert "DISARMED" in sent[0]
    assert "ingressos" in sent[0], "it must point at the order"
    assert "buy.enabled" in sent[0], "it must say how to resume"


# ── and the quiet that must STAY quiet ─────────────────────────────────────────

def test_a_night_that_really_bought_does_not_nag(tmp_path, monkeypatch, capsys):
    """⭐ A finished night is FINISHED. `order_url` is the discriminator. A reminder
    about a job well done is precisely the traffic that teaches a reader to mute the
    channel the Pix code arrives on -- the same reasoning that excludes ORDINARY from
    the rejected-row alert."""
    args, sent = _world(tmp_path, monkeypatch, buy_enabled=False,
                        ledger=_row("https://buyticketbrasil.com/ingressos/abc123"))
    buy_mod.cmd_autobuy(args)
    assert not sent, "a completed purchase must not send a recurring alert"
    assert "CLOSED" in capsys.readouterr().out, "but the log still explains the skip"


def test_a_parked_night_with_NO_ledger_row_stays_completely_silent(
        tmp_path, monkeypatch, capsys):
    """⛔ The six other nights (04, 05, 06, 07, 12, 13) are deliberately disarmed and
    must not become six daily messages. The ledger row -- not `buy.enabled` -- is what
    separates 'this closed itself on a failure' from 'a human parked this'."""
    args, sent = _world(tmp_path, monkeypatch, buy_enabled=False, ledger=None)
    assert buy_mod.cmd_autobuy(args) == 0
    assert not sent, "a deliberately parked night must not alert"
    assert not capsys.readouterr().out.strip(), "nor print"


def test_the_inert_alert_is_throttled(tmp_path, monkeypatch):
    """⚠️ On a 1-minute cron an unthrottled reminder is 1,440 messages a day. The
    throttle carries its OWN key so a chatty night cannot eat the slot `session_dead`
    or a `rejected:` row needs."""
    args, sent = _world(tmp_path, monkeypatch, buy_enabled=False,
                        ledger=_row(UNCONFIRMED))
    for _ in range(5):
        buy_mod.cmd_autobuy(args)
    assert len(sent) == 1, f"5 runs must send once, not {len(sent)}"
    key = (runner._read_state().get("alerts") or {})
    assert "inert:n" in key, "and on a key of its own"


def test_the_reminder_returns_after_the_window(tmp_path, monkeypatch):
    """⛔ Throttled is not muted. The condition persists, so the reminder must come
    back -- that is the difference between this and the 25 hours of nothing."""
    args, sent = _world(tmp_path, monkeypatch, buy_enabled=False,
                        ledger=_row(UNCONFIRMED))
    buy_mod.cmd_autobuy(args)
    assert len(sent) == 1
    st = runner._read_state()
    stale = datetime.now(timezone.utc) - timedelta(
        seconds=runner.INERT_ALERT_EVERY_S + 60)
    st["alerts"]["inert:n"] = stale.isoformat(timespec="seconds")
    runner._write_state(st)
    buy_mod.cmd_autobuy(args)
    assert len(sent) == 2, "the nag must return once the window has passed"


# ── the two discriminators, as units ───────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    (UNCONFIRMED, True),
    ("https://buyticketbrasil.com/ingressos/abc", False),
    (None, True),
    ("", True),
])
def test_is_unconfirmed_reads_order_url(url, expected):
    assert runner.is_unconfirmed({"order_url": url}) is expected


def test_is_unconfirmed_on_a_missing_row():
    assert runner.is_unconfirmed(None) is False


def test_target_id_of_reads_a_disarmed_file(tmp_path):
    """It must work on exactly the files `config.load` refuses."""
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"id": "n", "buy": {"enabled": False}}), encoding="utf-8")
    assert runner.target_id_of(p) == "n"


def test_target_id_of_survives_garbage(tmp_path):
    """⚠️ It runs before validation, so it must never be the thing that raises."""
    p = tmp_path / "t.json"
    p.write_text("{not json", encoding="utf-8")
    assert runner.target_id_of(p) is None
    assert runner.target_id_of(tmp_path / "missing.json") is None


# ── the instrument whose absence cost the night ────────────────────────────────

def _status_args(tmp_path):
    return SimpleNamespace(targets=str(tmp_path / "targets"),
                           history=str(tmp_path / "history"))


def test_status_tells_CLOSED_apart_from_PARKED(tmp_path, monkeypatch, capsys):
    """⭐ THREE outcomes, never two. Both are `buy.enabled: false` and they mean
    opposite things: one is a failure nobody saw, the other is a deliberate choice.
    Collapsing them is what let a dead night read as a parked one."""
    _world(tmp_path, monkeypatch, buy_enabled=False, ledger=_row(UNCONFIRMED))
    buy_mod.cmd_status(_status_args(tmp_path))
    out = capsys.readouterr().out
    assert "CLOSED" in out and "UNCONFIRMED" in out
    assert "PARKED" not in out, "a failure-disarmed night is not a parked one"


def test_status_says_PARKED_when_no_ledger_row(tmp_path, monkeypatch, capsys):
    _world(tmp_path, monkeypatch, buy_enabled=False, ledger=None)
    buy_mod.cmd_status(_status_args(tmp_path))
    out = capsys.readouterr().out
    assert "PARKED" in out and "CLOSED" not in out


def test_status_says_ARMED_and_drops_the_banner(tmp_path, monkeypatch, capsys):
    _world(tmp_path, monkeypatch, buy_enabled=True, ledger=None)
    buy_mod.cmd_status(_status_args(tmp_path))
    out = capsys.readouterr().out
    assert "ARMED" in out
    assert "NOTHING IS ARMED" not in out, "the banner must not cry wolf"


def test_status_shouts_when_nothing_is_armed(tmp_path, monkeypatch, capsys):
    """⛔ The one-line answer to the question that went unasked for 25 hours."""
    _world(tmp_path, monkeypatch, buy_enabled=False, ledger=_row(UNCONFIRMED))
    buy_mod.cmd_status(_status_args(tmp_path))
    assert "NOTHING IS ARMED" in capsys.readouterr().out


def test_status_flags_a_dip_that_is_being_ignored(tmp_path, monkeypatch, capsys):
    """⛔🔴 The R$220,00 qty 178 row, under a R$250,00 ceiling, on a closed night --
    the exact shape of all 67 in-window polls missed on 11/09."""
    _world(tmp_path, monkeypatch, buy_enabled=False, ledger=_row(UNCONFIRMED))
    buy_mod.cmd_status(_status_args(tmp_path))
    out = capsys.readouterr().out
    assert "UNDER THE CEILING" in out
    assert "NOTHING WILL BE BOUGHT" in out, "it must say the dip is being ignored"


def test_status_writes_nothing(tmp_path, monkeypatch):
    """⚠️ Read-only by construction -- it must be safe to run mid-buy. An instrument
    that mutates what it measures is not an instrument."""
    _world(tmp_path, monkeypatch, buy_enabled=False, ledger=_row(UNCONFIRMED))
    before = (tmp_path / "state" / "autobuy.json").read_text(encoding="utf-8")
    buy_mod.cmd_status(_status_args(tmp_path))
    assert (tmp_path / "state" / "autobuy.json").read_text(encoding="utf-8") == before
    assert not (tmp_path / "state" / "autobuy.lock").exists(), "it must take no lock"
