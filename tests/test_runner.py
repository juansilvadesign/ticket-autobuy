"""Tests for the unattended auto-buy runner.

⛔ This is the only code in the repo that spends money with nobody watching. Every test
here pins a failure that would be INVISIBLE in production: a window evaluated in the
wrong timezone, a dead poller read as a calm market, a ledger that forgets what already
fired. None of them raise; they all just quietly do the wrong thing forever.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autobuy import config, runner                                    # noqa: E402
from autobuy.errors import AutobuyError, ConfigError                  # noqa: E402

BRT = runner.TZ


def _t(h, m=0, day=3):
    return datetime(2026, 9, day, h, m, tzinfo=BRT)


# ------------------------------------------------------------------ window

@pytest.mark.parametrize("h,active", [
    (9, False), (10, True), (15, True), (23, True),
    (0, True), (3, True), (4, False), (5, False), (7, False),
])
def test_window_crosses_midnight(h, active):
    """10:00->04:00 wraps. A naive `start <= t < end` is False for EVERY hour of it,
    which would silently disable the whole feature while looking configured."""
    assert runner.within_window(_t(h, 30)) is active


def test_window_is_evaluated_in_sao_paulo_not_the_daemon_timezone():
    """⛔ THE bug the TZ pin exists to stop. cron inherits the daemon's TZ, frequently
    UTC on WSL. 10:30 UTC is 07:30 BRT -- outside Juan's window -- but a naive reading
    of its clock face says 10:30, i.e. 'just opened'. The mistake refuses to buy for
    three morning hours AND fires at 02:00 while he sleeps, invisibly in both directions.
    """
    utc_1030 = datetime(2026, 9, 3, 10, 30, tzinfo=timezone.utc)   # = 07:30 BRT
    assert runner.within_window(utc_1030) is False
    utc_0300 = datetime(2026, 9, 3, 3, 0, tzinfo=timezone.utc)     # = 00:00 BRT
    assert runner.within_window(utc_0300) is True


# ------------------------------------------------------------------ history

def _hist(tmp_path, rows, name="t.jsonl"):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def _row(price, *, qty=5, sector="Gramado", cls="Inteira", when=None, available=True):
    return {"target_id": "t", "item": f"{sector} || {cls}", "price_cents": price,
            "quantity": qty, "available": available,
            "captured_at": (when or datetime.now(timezone.utc)).isoformat(),
            "extra": {"sector": sector, "entry_class": cls}}


def test_an_old_history_file_is_NOT_treated_as_blindness(tmp_path):
    """⛔ REVERSED 2026-09-02, and the reversal is the point. This test used to assert
    that a 30-minute-old history file raised "DEAD" -- reasoning that a stopped cron
    must not read as a calm market. That reasoning was right and the instrument was
    wrong: price-watcher appends ONLY on change, so file age measures VOLATILITY, not
    freshness, and the old rule would have skipped a dip that dropped and then held.

    Blindness is now `assert_poller_alive`'s job, from the cron log. See
    `test_a_dead_poller_raises_rather_than_reporting_a_calm_market`.
    """
    old = datetime.now(timezone.utc) - timedelta(minutes=30)
    p = _hist(tmp_path, [_row(19000, when=old)])
    assert runner.latest_readings(p)[0]["price_cents"] == 19000


def test_fresh_history_returns_only_the_newest_run(tmp_path):
    now = datetime.now(timezone.utc)
    old = now - timedelta(minutes=2)
    p = _hist(tmp_path, [_row(50000, when=old), _row(19000, when=now),
                         _row(21000, when=now)])
    got = runner.latest_readings(p)
    assert len(got) == 2 and {r["price_cents"] for r in got} == {19000, 21000}


def test_absent_history_is_blindness_not_an_empty_market(tmp_path):
    with pytest.raises(AutobuyError, match="no history"):
        runner.latest_readings(tmp_path / "nope.jsonl")


def test_a_torn_last_line_does_not_lose_the_run(tmp_path):
    """cron appends while we read. A half-written final line must not discard the file."""
    now = datetime.now(timezone.utc)
    p = _hist(tmp_path, [_row(19000, when=now)])
    with p.open("a", encoding="utf-8") as fh:
        fh.write('{"target_id": "t", "price_ce')          # torn append in progress
    assert runner.latest_readings(p)[0]["price_cents"] == 19000


# ------------------------------------------------------------------ selection

def _cfg(tmp_path, *, ceiling=200.0, floor=None, sector=("Gramado",), qty=1):
    buy = {"enabled": True, "max_price_brl": ceiling, "quantity": qty,
           "sector": list(sector), "entry_class": None}
    if floor is not None:
        buy["min_price_brl"] = floor
    t = tmp_path / "x.json"
    t.write_text(json.dumps({
        "id": "x", "label": "x",
        "params": {"event_slug": "e", "data_millis": 1, "evento_local": "l"},
        "buy": buy}), encoding="utf-8")
    return config.load(t), t


def test_picks_the_cheapest_row_under_the_ceiling(tmp_path):
    cfg, _ = _cfg(tmp_path)
    hit = runner.candidate_under_ceiling(
        [_row(19900), _row(15000), _row(25000)], cfg)
    assert hit["price_cents"] == 15000


def test_a_row_over_the_ceiling_never_fires(tmp_path):
    cfg, _ = _cfg(tmp_path)
    assert runner.candidate_under_ceiling([_row(20001), _row(30000)], cfg) is None


def test_the_ceiling_is_inclusive(tmp_path):
    cfg, _ = _cfg(tmp_path)
    assert runner.candidate_under_ceiling([_row(20000)], cfg)["price_cents"] == 20000


def test_wrong_sector_and_sold_out_rows_are_skipped(tmp_path):
    cfg, _ = _cfg(tmp_path)
    assert runner.candidate_under_ceiling([_row(10000, sector="Comfort Zone")], cfg) is None
    assert runner.candidate_under_ceiling([_row(10000, available=False)], cfg) is None
    assert runner.candidate_under_ceiling([_row(10000, qty=0)], cfg) is None


def test_no_floor_means_a_suspiciously_cheap_row_still_fires(tmp_path):
    """Juan's call 2026-09-02: buy it. An unpaid hold on a mispriced row costs nothing,
    and refusing it is how you miss the one dip the tool exists for."""
    cfg, _ = _cfg(tmp_path, floor=None)
    assert runner.candidate_under_ceiling([_row(6600)], cfg)["price_cents"] == 6600


def test_a_floor_still_works_when_someone_sets_one(tmp_path):
    cfg, _ = _cfg(tmp_path, floor=100.0)
    assert runner.candidate_under_ceiling([_row(6600)], cfg) is None
    assert runner.candidate_under_ceiling([_row(15000)], cfg)["price_cents"] == 15000


# ------------------------------------------------------------------ ledger + lock

def test_a_corrupt_ledger_refuses_to_run(tmp_path, monkeypatch):
    """⛔ Absent means 'nothing fired yet'. Unparseable means we do not KNOW what fired,
    and guessing 'none' reserves a duplicate ticket. Never collapse the two."""
    monkeypatch.setattr(runner, "STATE_PATH", tmp_path / "s.json")
    (tmp_path / "s.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="duplicate"):
        runner._read_state()


def test_an_absent_ledger_is_a_clean_start(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "STATE_PATH", tmp_path / "none.json")
    assert runner._read_state() == {"fired": {}}


def test_a_fired_night_is_never_bought_twice(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runner, "STATE_PATH", tmp_path / "s.json")
    st = runner.record_fired({"fired": {}}, "night-1", price_cents=19000,
                             order_url="u", item="i")
    runner._write_state(st)
    assert runner.already_fired(runner._read_state(), "night-1")
    assert not runner.already_fired(runner._read_state(), "night-2")


def test_disarm_target_flips_enabled_and_keeps_the_file_parseable(tmp_path):
    """⚠️ price-watcher parses this same file every minute; a torn write turns a
    successful purchase into an AdapterError on the watcher."""
    _, t = _cfg(tmp_path)
    runner.disarm_target(t)
    raw = json.loads(t.read_text(encoding="utf-8"))
    assert raw["buy"]["enabled"] is False
    with pytest.raises(ConfigError, match="not armed"):
        config.load(t)


def test_the_lock_is_exclusive(tmp_path, monkeypatch):
    """⛔ A buy takes ~40s and cron fires every minute, so invocations WILL overlap.
    Two runs seeing one dip is 'one intended ticket becomes two' arriving by schedule."""
    monkeypatch.setattr(runner, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runner, "LOCK_PATH", tmp_path / "l.lock")
    first = runner.acquire_lock()
    assert first is not None
    assert runner.acquire_lock() is None, "a second concurrent run must be refused"
    first.close()
    again = runner.acquire_lock()
    assert again is not None, "the lock must be released when the run ends"
    again.close()


# ------------------------------------------------------------------ liveness vs change

def test_a_quiet_history_file_is_a_stable_market_not_a_dead_poller(tmp_path):
    """⛔ THE bug the rehearsal caught. price-watcher appends ONLY when a reading
    changes -- 'unchanged since the last recording, nothing written'. So a price that
    drops to R$150 and then HOLDS is written once and never again. An age cutoff over
    the history file would call that blind and skip the one dip the tool exists for.
    The last reading is carried forward, whatever its age."""
    ancient = datetime.now(timezone.utc) - timedelta(hours=9)
    p = _hist(tmp_path, [_row(15000, when=ancient)])
    got = runner.latest_readings(p)                    # must NOT raise
    assert got[0]["price_cents"] == 15000


def test_poller_liveness_comes_from_the_cron_log_not_the_data(tmp_path):
    """cron.log is touched on EVERY run, including runs that write no reading. That is
    exactly what makes it a liveness signal when the .jsonl files are not."""
    (tmp_path / "cron.log").write_text("ran", encoding="utf-8")
    runner.assert_poller_alive(tmp_path)               # fresh log => alive


def test_a_dead_poller_raises_rather_than_reporting_a_calm_market(tmp_path):
    import os
    log = tmp_path / "cron.log"; log.write_text("ran", encoding="utf-8")
    old = datetime.now(timezone.utc).timestamp() - 3600
    os.utime(log, (old, old))
    with pytest.raises(AutobuyError, match="DEAD"):
        runner.assert_poller_alive(tmp_path)


def test_no_poller_artifacts_at_all_is_blindness(tmp_path):
    with pytest.raises(AutobuyError, match="never run"):
        runner.assert_poller_alive(tmp_path)


def test_a_recurring_alert_is_throttled(tmp_path):
    """⛔ Without a throttle a dead session on a 1-minute cron sends 1,440 identical
    messages a day, and the muted channel is the one the Pix code arrives on."""
    st = {}
    now = datetime.now(timezone.utc)
    assert runner.should_alert(st, "session_dead", now=now) is True
    assert runner.should_alert(st, "session_dead", now=now) is False
    later = now + timedelta(minutes=31)
    assert runner.should_alert(st, "session_dead", now=later) is True


def test_a_different_problem_alerts_independently():
    st = {}
    now = datetime.now(timezone.utc)
    assert runner.should_alert(st, "session_dead", now=now) is True
    assert runner.should_alert(st, "poller_dead", now=now) is True


# ------------------------------------------------------------------ the alert path

def _autobuy_world(tmp_path, monkeypatch):
    """The minimum world in which `cmd_autobuy` actually reaches the buy: one armed
    target, a dip in history, a live poller, an empty ledger, inside the window."""
    from types import SimpleNamespace
    import buy as buy_mod

    targets = tmp_path / "targets"; targets.mkdir()
    hist = tmp_path / "history"; hist.mkdir()
    state = tmp_path / "state"; state.mkdir()

    (targets / "n.json").write_text(json.dumps({
        "id": "n", "label": "Night N",
        "params": {"event_slug": "e", "data_millis": 1, "evento_local": "l"},
        "buy": {"enabled": True, "max_price_brl": 200.0, "quantity": 1,
                "sector": ["Gramado"], "entry_class": None}}), encoding="utf-8")
    (hist / "n.jsonl").write_text(json.dumps({
        "target_id": "n", "item": "Gramado || Inteira", "price_cents": 19800,
        "quantity": 1, "available": True,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "extra": {"sector": "Gramado", "entry_class": "Inteira"}}) + "\n",
        encoding="utf-8")
    (hist / "cron.log").write_text("alive\n", encoding="utf-8")

    monkeypatch.setattr(runner, "STATE_DIR", state)
    monkeypatch.setattr(runner, "STATE_PATH", state / "autobuy.json")
    monkeypatch.setattr(runner, "LOCK_PATH", state / "autobuy.lock")
    monkeypatch.setattr(runner, "now_local", lambda: _t(15))       # inside 10:00-04:00

    sent: list[str] = []
    monkeypatch.setattr(buy_mod.notify, "send_text",
                        lambda tok, chat, text: sent.append(text))
    # ⭐ The FILE check PASSES. That is the whole point: a logged-out session still has
    # a file and still has cookies, so `require()` is not where it is discovered.
    monkeypatch.setattr(buy_mod.session, "require", lambda *a, **k: tmp_path / "s.json")

    args = SimpleNamespace(targets=str(targets), history=str(hist),
                           fields=str(tmp_path / "f.json"), verbose=False)
    return buy_mod, args, sent


def test_a_session_that_dies_at_the_BROWSER_still_alerts(tmp_path, monkeypatch):
    """⛔ THE regression, and it was silent. `session.require()` is a FILE check, so a
    session that exists, carries cookies and is thoroughly logged out sails straight
    through it. The real probe is inside `open_listing`, behind the browser, and its
    `SessionError` used to travel past the alert branch to exit 3.

    Measured 2026-09-04: a 36 h-old session against an observed lifetime under 4 h,
    3 `Entrar` links on the homepage, ZERO 'not authenticated' lines in autobuy.log and
    no state file at all -- the guard CLAUDE.md calls load-bearing had never fired once.
    """
    from autobuy.errors import SessionError
    buy_mod, args, sent = _autobuy_world(tmp_path, monkeypatch)

    def _dies_at_the_browser(*a, **k):
        raise SessionError("not authenticated -- the page still shows 3 'Entrar' link(s)")
    monkeypatch.setattr(buy_mod, "_execute_buy", _dies_at_the_browser)

    with pytest.raises(SessionError):
        buy_mod.cmd_autobuy(args)

    assert sent, "a session that dies at the BROWSER must still SHOUT"
    assert "BLIND" in sent[0] and "buy.py login" in sent[0]


def test_an_absent_session_file_still_alerts(tmp_path, monkeypatch):
    """The original call site must keep working -- the fix ADDS a second one."""
    from autobuy.errors import SessionError
    buy_mod, args, sent = _autobuy_world(tmp_path, monkeypatch)

    def _no_file(*a, **k):
        raise SessionError("no session at ...")
    monkeypatch.setattr(buy_mod.session, "require", _no_file)

    with pytest.raises(SessionError):
        buy_mod.cmd_autobuy(args)
    assert sent and "BLIND" in sent[0]


def test_a_dip_that_evaporated_is_NOT_a_dead_session(tmp_path, monkeypatch):
    """⚠️ The PERMIT leg. `NoMatch` is the ordinary outcome on a fast market; alerting
    on it would send the 1,440-a-day flood the throttle exists to prevent, on a channel
    that has to stay readable because the Pix code arrives there."""
    from autobuy.errors import NoMatch
    buy_mod, args, sent = _autobuy_world(tmp_path, monkeypatch)

    def _evaporated(*a, **k):
        raise NoMatch("gone before we got there")
    monkeypatch.setattr(buy_mod, "_execute_buy", _evaporated)

    assert buy_mod.cmd_autobuy(args) == 0
    assert not sent, "an evaporated dip must never fire the dead-session alert"
