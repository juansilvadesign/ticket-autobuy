"""Tests for the 11/09 plan: the price ladder (`set_ceiling.py`) and the session
keep-alive (`keepalive.py`).

⛔ Both of these run unattended on cron beside a buy that spends money, and every
failure they can have is SILENT. A ladder step that no-ops leaves the ceiling low and
looks like a market that never dipped; a step that fires on a finished night rebuilds
the 2026-09-05 bug where 04/09 sat armed at R$1.000 after its event ended. A keep-alive
that writes a logged-out context over a good session file destroys the exact thing it
exists to protect, and reports success while doing it.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import keepalive                                                      # noqa: E402
import set_ceiling                                                    # noqa: E402
from autobuy import runner, session                                   # noqa: E402

BRT = runner.TZ
NIGHT = "rockinrio2026-09-11"


# ------------------------------------------------------------------ ladder helpers

def _target(tmp_path, *, enabled=True, ceiling=200.00) -> Path:
    d = tmp_path / "targets"
    d.mkdir(exist_ok=True)
    p = d / f"{NIGHT}.json"
    p.write_text(json.dumps({
        "id": NIGHT,
        "enabled": True,
        "buy": {"enabled": enabled, "max_price_brl": ceiling, "quantity": 1,
                "sector": ["Gramado"], "entry_class": None, "min_available": 20},
    }, indent=2) + "\n", encoding="utf-8")
    return p


def _run(monkeypatch, tmp_path, target, *, now, deadline, brl):
    """Drive the real `set_ceiling.main` with the clock and target dir redirected."""
    monkeypatch.setattr(set_ceiling, "TARGETS", target.parent)
    monkeypatch.setattr(runner, "now_local", lambda: now)
    monkeypatch.setattr(runner, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runner, "LOCK_PATH", tmp_path / "autobuy.lock")
    rc = set_ceiling.main(["set_ceiling.py", NIGHT, deadline, brl])
    return rc, json.loads(target.read_text())["buy"]["max_price_brl"]


# ------------------------------------------------------------------ the ladder

def test_a_step_does_nothing_before_its_deadline(monkeypatch, tmp_path):
    """R$250 is due at 00:00 on 10/09. On the 9th at 23:59 the ceiling is still R$200 --
    the whole point of a dated ladder is that it does not arrive early."""
    t = _target(tmp_path)
    rc, ceiling = _run(monkeypatch, tmp_path, t,
                       now=datetime(2026, 9, 9, 23, 59, tzinfo=BRT),
                       deadline="2026-09-10T00:00", brl="250")
    assert rc == 0
    assert ceiling == 200.00


def test_a_step_raises_the_ceiling_once_its_deadline_passes(monkeypatch, tmp_path):
    t = _target(tmp_path)
    rc, ceiling = _run(monkeypatch, tmp_path, t,
                       now=datetime(2026, 9, 10, 0, 5, tzinfo=BRT),
                       deadline="2026-09-10T00:00", brl="250")
    assert rc == 0
    assert ceiling == 250.00


def test_a_step_is_MONOTONE_and_never_lowers_a_ceiling(monkeypatch, tmp_path):
    """⛔ THE property that makes it safe to leave every step on cron forever. On event
    day both the R$250 and the R$300 lines are past their deadline and both run every
    5 minutes. If the R$250 step could write, the ceiling would oscillate 300 -> 250 ->
    300 all day and which value was live when a dip landed would be pure luck."""
    t = _target(tmp_path, ceiling=300.00)
    rc, ceiling = _run(monkeypatch, tmp_path, t,
                       now=datetime(2026, 9, 11, 12, 0, tzinfo=BRT),
                       deadline="2026-09-10T00:00", brl="250")
    assert rc == 0
    assert ceiling == 300.00


def test_a_step_REFUSES_a_disarmed_night(monkeypatch, tmp_path):
    """⛔ The self-terminating clause, and it carries the 2026-09-05 lesson: 04/09 was
    left armed at a temporary R$1.000 ceiling after its event had ended, and every cron
    minute was a chance to reserve a R$1.000 ticket for a night that was already over.
    Both endings here -- the buy, and the 16:00 hard stop -- set `enabled: false`, so
    this is what stops the ladder climbing over a finished night."""
    t = _target(tmp_path, enabled=False)
    rc, ceiling = _run(monkeypatch, tmp_path, t,
                       now=datetime(2026, 9, 11, 18, 0, tzinfo=BRT),
                       deadline="2026-09-11T00:00", brl="300")
    assert rc == 0
    assert ceiling == 200.00
    assert json.loads(t.read_text())["buy"]["enabled"] is False


def test_a_night_disarmed_while_we_WAITED_for_the_lock_is_not_re_armed(monkeypatch,
                                                                       tmp_path):
    """⛔🔴 The race the in-lock re-read exists for, and the reason it re-reads rather
    than reusing the dict it already parsed.

    `set_ceiling` checks `enabled` OUTSIDE the lock, then may block up to 60 s waiting
    for it -- and a buy takes ~40 s. So the sequence is real: we read an armed night,
    a concurrent buy reserves a ticket and disarms it, and we finally get the lock
    holding a stale copy that still says `"enabled": true`. Writing that copy back does
    not merely set a ceiling; it **re-arms a night that has already spent money**, and
    the next cron minute buys a second ticket for it.

    Same clobber shape as the read-modify-write that was silently lost on 2026-09-04.
    """
    t = _target(tmp_path)
    real_acquire = runner.acquire_lock

    def _disarm_then_lock():
        # the concurrent buy, landing in the gap between the check and the lock
        raw = json.loads(t.read_text())
        raw["buy"]["enabled"] = False
        t.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        return real_acquire()

    monkeypatch.setattr(runner, "acquire_lock", _disarm_then_lock)
    rc, ceiling = _run(monkeypatch, tmp_path, t,
                       now=datetime(2026, 9, 11, 0, 5, tzinfo=BRT),
                       deadline="2026-09-11T00:00", brl="300")
    assert rc == 0
    assert ceiling == 200.00
    assert json.loads(t.read_text())["buy"]["enabled"] is False    # still disarmed


def test_the_deadline_is_read_in_SAO_PAULO_not_the_daemon_timezone(monkeypatch, tmp_path):
    """⛔ cron on this WSL box frequently runs UTC. 2026-09-10T00:00 read as UTC is
    21:00 BRT on the 9th -- the ladder would spend the whole of Wednesday evening at
    R$250 when the plan says R$200, and nothing in a log would show it."""
    t = _target(tmp_path)
    # 2026-09-09 22:00 BRT == 2026-09-10 01:00 UTC. A UTC reading calls this "past the
    # midnight step"; a BRT reading -- the correct one -- calls it two hours early.
    rc, ceiling = _run(monkeypatch, tmp_path, t,
                       now=datetime(2026, 9, 9, 22, 0, tzinfo=BRT),
                       deadline="2026-09-10T00:00", brl="250")
    assert rc == 0
    assert ceiling == 200.00


def test_the_ceiling_round_trips_through_DECIMAL_not_float(monkeypatch, tmp_path):
    """`CLAUDE.md`'s money invariant. `int(220.30 * 100)` is 22029 -- a ceiling one
    centavo low that simply never fires, silently, and only on binary-inexact values.
    The written value must reload as exactly the centavos we asked for."""
    from autobuy import config
    t = _target(tmp_path)
    rc, ceiling = _run(monkeypatch, tmp_path, t,
                       now=datetime(2026, 9, 11, 0, 1, tzinfo=BRT),
                       deadline="2026-09-11T00:00", brl="220.30")
    assert rc == 0
    assert config.brl_to_cents(ceiling, "reloaded") == 22030


def test_a_missing_target_is_a_config_error_not_a_silent_pass(monkeypatch, tmp_path):
    t = _target(tmp_path)
    monkeypatch.setattr(set_ceiling, "TARGETS", t.parent)
    monkeypatch.setattr(runner, "now_local",
                        lambda: datetime(2026, 9, 11, 0, 1, tzinfo=BRT))
    assert set_ceiling.main(
        ["set_ceiling.py", "rockinrio2026-09-99", "2026-09-11T00:00", "300"]) == 1


# ------------------------------------------------------------------ keep-alive fakes

class _FakePage:
    """Just enough page for `session.auth_signals`. `entrar_links=None` makes the
    locator raise, which is how a page that never rendered actually presents."""

    def __init__(self, url="https://buyticketbrasil.com/", entrar_links=0, cookies=49):
        self.url = url
        self._links = entrar_links
        self.context = self
        self._cookies = cookies

    def locator(self, _sel):
        page = self

        class _Loc:
            def count(self):
                if page._links is None:
                    raise RuntimeError("page never rendered")
                return page._links
        return _Loc()

    def cookies(self):
        return [{}] * self._cookies


class _FakeContext:
    def __init__(self):
        self.saved_to = []

    def storage_state(self, path=None):
        self.saved_to.append(path)


@pytest.fixture
def no_telegram(monkeypatch):
    sent = []
    monkeypatch.setattr(keepalive.notify, "send_text",
                        lambda tok, chat, text: sent.append(text))
    return sent


@pytest.fixture
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runner, "STATE_PATH", tmp_path / "autobuy.json")
    return tmp_path


# ------------------------------------------------------------------ the keep-alive

def test_a_live_session_is_WRITTEN_BACK(monkeypatch, tmp_path, no_telegram,
                                        isolated_state):
    """⭐ The warming mechanism itself. Probing without persisting the refreshed
    `storage_state` would re-read the same ageing cookies every 20 minutes and extend
    nothing -- a keep-alive that only watches."""
    monkeypatch.setattr(session, "save", lambda ctx, path=None: ctx.storage_state())
    ctx = _FakeContext()
    ok, sig, alerted = keepalive.run_probe(ctx, _FakePage(), {})
    assert ok is True
    assert ctx.saved_to == [None]          # saved exactly once
    assert alerted is False
    assert no_telegram == []


def test_a_LOGGED_OUT_probe_never_overwrites_the_session_file(monkeypatch, tmp_path,
                                                              no_telegram,
                                                              isolated_state):
    """⛔ The write that must never happen. A logged-out context's `storage_state`
    written over a good file destroys a working session -- and the next `buy.py login`
    is a human, at a keyboard, who may be asleep."""
    monkeypatch.setattr(session, "save", lambda ctx, path=None: ctx.storage_state())
    ctx = _FakeContext()
    ok, sig, alerted = keepalive.run_probe(ctx, _FakePage(entrar_links=3), {})
    assert ok is False
    assert ctx.saved_to == []
    assert alerted is True


def test_an_UNREADABLE_probe_is_not_health(monkeypatch, tmp_path, no_telegram,
                                           isolated_state):
    """⛔ `entrar_links: -1` means "could not be read", never "zero". A page that never
    rendered must not be saved over a live session, and must not be reported as alive --
    the absence of an error proves nothing here, since an expired session still returns
    a perfectly healthy 200."""
    monkeypatch.setattr(session, "save", lambda ctx, path=None: ctx.storage_state())
    ctx = _FakeContext()
    ok, sig, alerted = keepalive.run_probe(ctx, _FakePage(entrar_links=None), {})
    assert sig["entrar_links"] == -1
    assert ok is False
    assert ctx.saved_to == []


def test_the_login_wall_is_reported_by_NAME(monkeypatch, isolated_state, no_telegram):
    """The exact shape of 2026-09-06's four failed buys, so the alert says which signal
    fired rather than collapsing every cause into one boolean."""
    page = _FakePage(url="https://buyticketbrasil.com/entrar?event=rockinrio2026")
    ok, sig, alerted = keepalive.run_probe(_FakeContext(), page, {})
    assert ok is False
    assert sig["on_login_wall"] is True
    assert "login wall" in no_telegram[0]
    assert "buy.py login" in no_telegram[0]


def test_the_probe_alert_is_throttled(monkeypatch, isolated_state, no_telegram):
    """A `*/20` cron that alerted every run would send 72 identical messages a day and
    train the reader to mute the channel the Pix code arrives on."""
    state = {}
    page = _FakePage(entrar_links=3)
    assert keepalive.run_probe(_FakeContext(), page, state)[2] is True
    assert keepalive.run_probe(_FakeContext(), page, state)[2] is False
    assert len(no_telegram) == 1


def test_the_probe_does_NOT_eat_the_buy_paths_alert_slot(monkeypatch, isolated_state,
                                                         no_telegram):
    """⛔ Separate throttle keys, deliberately. The probe fires every 20 minutes; the
    buy path fires only when a real dip was lost. If they shared a key, a quiet probe
    would silence the one message that names a missed R$165 ticket."""
    state = {}
    keepalive.run_probe(_FakeContext(), _FakePage(entrar_links=3), state)
    assert keepalive.ALERT_KEY in state["alerts"]
    assert "session_dead" not in state["alerts"]
    # and the buy path's own slot is still free
    assert runner.should_alert(state, "session_dead") is True
