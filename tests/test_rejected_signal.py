"""A ticket UNDER the ceiling that a filter refused must not be silent.

⛔🔴 The gap these tests pin cost two real windows on the 11/09 night, at a R$200,00
ceiling, with the session alive and the poller healthy the whole time:

    2026-09-06 10:43-11:10  Gramado || Meia Jovem Baixa Renda  R$198,00  qty 12   7 polls
    2026-09-07 14:44-15:02  Gramado || Meia PCD                R$198,00  qty 11  10 polls

price-watcher sent `!! [critical_price] CRITICAL R$ 198,00 — under R$ 200,00` for both.
`grep -c "Meia PCD\\|Jovem Baixa Renda" state/autobuy.log` returns **0**: `min_available:
20` rejected the rows inside `candidate_under_ceiling`, which returns None for "the
market is above your ceiling" and for "a filter said no" alike. The two outcomes were
byte-identical -- nothing, in any log, in any channel -- so for three days the buyer was
indistinguishable from a buyer watching a calm market, while Telegram said otherwise.

⭐ Every test here fails on the pre-patch tree. That is the point: the defect was
ABSENCE of output, and only a test that demands the output can pin it.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import buy                                                            # noqa: E402
from autobuy import config, runner                                    # noqa: E402


def _row(price, *, qty=5, sector="Gramado", cls="Inteira", available=True):
    return {"target_id": "t", "item": f"{sector} || {cls}", "price_cents": price,
            "quantity": qty, "available": available,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "extra": {"sector": sector, "entry_class": cls}}


def _cfg(tmp_path, *, ceiling=200.0, floor=None, sector=("Gramado",), qty=1,
         min_available=None, entry_class=None):
    buy_block = {"enabled": True, "max_price_brl": ceiling, "quantity": qty,
                 "sector": list(sector), "entry_class": entry_class}
    if floor is not None:
        buy_block["min_price_brl"] = floor
    if min_available is not None:
        buy_block["min_available"] = min_available
    t = tmp_path / "x.json"
    t.write_text(json.dumps({
        "id": "rockinrio2026-09-11", "label": "Rock in Rio 2026 — 11/09 (sex) · Gramado",
        "params": {"event_slug": "e", "data_millis": 1, "evento_local": "l"},
        "buy": buy_block}), encoding="utf-8")
    return config.load(t), t


# ── the depth floor, which is what actually fired ───────────────────────────────

def test_the_REAL_2026_09_07_row_is_reported_not_swallowed(tmp_path):
    """`Gramado || Meia PCD` R$198,00 qty 11 against ceiling R$200 / min_available 20.

    Replayed from `history/rockinrio2026-09-11.jsonl`. The buy correctly declines it;
    what was broken is that declining produced no trace anywhere.
    """
    cfg, _ = _cfg(tmp_path, ceiling=200.0, min_available=20)
    readings = [_row(19800, qty=11, cls="Meia PCD"),
                _row(32450, qty=6, cls="Meia Estudante")]

    assert runner.candidate_under_ceiling(readings, cfg) is None, "still declined"

    rejected = runner.rejected_under_ceiling(readings, cfg)
    assert len(rejected) == 1, "exactly the sub-ceiling row, not the market above it"
    row, reason = rejected[0]
    assert row["price_cents"] == 19800
    assert "11" in reason and "20" in reason, f"must name both numbers, got {reason!r}"
    assert "min_available" in reason, "must name the KNOB, so it can be changed"


def test_an_ordinary_over_ceiling_market_reports_NOTHING(tmp_path):
    """⛔ The muting hazard. A market above the ceiling is the state of almost every
    run on every night; a signal that also fires there trains the reader to mute the
    channel the Pix code arrives on."""
    cfg, _ = _cfg(tmp_path, ceiling=200.0, min_available=20)
    assert runner.rejected_under_ceiling(
        [_row(32450, qty=6), _row(33000, qty=185), _row(36300, qty=1)], cfg) == []


def test_a_row_that_IS_bought_is_never_also_reported(tmp_path):
    cfg, _ = _cfg(tmp_path, ceiling=200.0, min_available=20)
    readings = [_row(16500, qty=26, cls="Meia Até 21")]
    assert runner.candidate_under_ceiling(readings, cfg)["price_cents"] == 16500
    assert runner.rejected_under_ceiling(readings, cfg) == []


@pytest.mark.parametrize("row,fragment", [
    (_row(15000, qty=0), "min_available"),
    (_row(15000, sector="Comfort Zone"), "sector"),
    (_row(15000, available=False), "no longer available"),
])
def test_every_refusal_reason_names_itself(tmp_path, row, fragment):
    """⛔ "not bought" is not a diagnosis. The reason has to name the filter, or the
    next person re-derives it from the source at 03:00 on event night."""
    cfg, _ = _cfg(tmp_path, ceiling=200.0)
    rejected = runner.rejected_under_ceiling([row], cfg)
    assert len(rejected) == 1 and fragment in rejected[0][1]


def test_the_anomaly_floor_reports_itself_too(tmp_path):
    cfg, _ = _cfg(tmp_path, ceiling=200.0, floor=100.0)
    rejected = runner.rejected_under_ceiling([_row(6600)], cfg)
    assert len(rejected) == 1 and "min_price_brl" in rejected[0][1]


# ── the refactor must not have moved the buy decision ───────────────────────────

def _pre_patch_choice(readings, cfg):
    """The chooser EXACTLY as it read before `reject_reason` was extracted.

    ⛔ The refactor put one predicate behind two callers. If it drifted, the drift would
    land on the code that decides whether to spend money -- so the old body is kept here
    as an independent oracle rather than trusting that "it looks the same".
    """
    pool = []
    for r in readings:
        if not r.get("available", True):
            continue
        price = r.get("price_cents")
        if not isinstance(price, int) or price <= 0 or price > cfg.max_price_cents:
            continue
        if cfg.min_price_cents and price < cfg.min_price_cents:
            continue
        if (r.get("quantity") or 0) < max(cfg.quantity, cfg.min_available):
            continue
        extra = r.get("extra") or {}
        if cfg.sectors and (extra.get("sector") or "").lower() not in \
                {s.lower() for s in cfg.sectors}:
            continue
        if cfg.entry_classes and (extra.get("entry_class") or "").lower() not in \
                {c.lower() for c in cfg.entry_classes}:
            continue
        pool.append(r)
    return min(pool, key=lambda r: (r["price_cents"], -(r.get("quantity") or 0))) \
        if pool else None


def test_the_refactor_did_not_change_WHICH_row_is_bought(tmp_path):
    """Every shape the two windows and the live market actually produced."""
    matrix = [
        [_row(19800, qty=11, cls="Meia PCD")],
        [_row(19800, qty=12, cls="Meia Jovem Baixa Renda")],
        [_row(16500, qty=26, cls="Meia Até 21"), _row(19800, qty=12)],
        [_row(5500, qty=288), _row(32450, qty=6)],
        [_row(20000, qty=50)],                      # ceiling is inclusive
        [_row(20001, qty=50)],
        [_row(15000, qty=50, sector="Comfort Zone")],
        [_row(15000, qty=50, available=False)],
        [_row(15000, qty=50), _row(15000, qty=99)],  # tie -> deeper stock
        [_row(0, qty=50)], [{"item": "junk"}], [],
    ]
    for min_av in (1, 12, 20):
        for ceiling in (200.0, 250.0, 300.0):
            cfg, _ = _cfg(tmp_path, ceiling=ceiling, min_available=min_av)
            for readings in matrix:
                assert runner.candidate_under_ceiling(readings, cfg) == \
                    _pre_patch_choice(readings, cfg), \
                    f"drift at ceiling={ceiling} min_available={min_av}: {readings}"


# ── the reporting call site ─────────────────────────────────────────────────────

def _capture(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(runner, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(runner, "STATE_DIR", tmp_path)
    monkeypatch.setattr(buy.notify, "send_text",
                        lambda tok, chat, text: sent.append(text))
    return sent


def test_it_LOGS_and_TELEGRAMS_the_sub_ceiling_row(monkeypatch, tmp_path, capsys):
    sent = _capture(monkeypatch, tmp_path)
    cfg, _ = _cfg(tmp_path, ceiling=200.0, min_available=20)
    buy._report_rejected({}, cfg, [_row(19800, qty=11, cls="Meia PCD")])

    out = capsys.readouterr().out
    assert "Meia PCD" in out and "198,00" in out, f"log line missing: {out!r}"
    assert len(sent) == 1 and "198,00" in sent[0]
    assert "Meia PCD" in sent[0]


def test_a_calm_market_neither_logs_nor_telegrams(monkeypatch, tmp_path, capsys):
    sent = _capture(monkeypatch, tmp_path)
    cfg, _ = _cfg(tmp_path, ceiling=200.0, min_available=20)
    buy._report_rejected({}, cfg, [_row(32450, qty=6)])
    assert capsys.readouterr().out == "" and sent == []


def test_the_telegram_is_throttled_but_the_LOG_never_is(monkeypatch, tmp_path, capsys):
    """⛔ A 1-minute cron over a 19-minute window is 19 identical messages. The log is
    the diagnosable record and stays unconditional; only the message is rationed."""
    sent = _capture(monkeypatch, tmp_path)
    cfg, _ = _cfg(tmp_path, ceiling=200.0, min_available=20)
    state: dict = {}
    for _ in range(5):
        buy._report_rejected(state, cfg, [_row(19800, qty=11)])
    assert len(sent) == 1, "throttled to one per window"
    assert capsys.readouterr().out.count("was NOT bought") == 5, "log stays every run"


def test_the_rejected_alert_has_its_OWN_throttle_key(monkeypatch, tmp_path):
    """⛔ Same discipline as `session_dead_probe`: a routine alert must not eat the slot
    that a different message needs. A shared key means the first dead-session warning of
    the evening silently swallows the first missed-dip warning."""
    sent = _capture(monkeypatch, tmp_path)
    cfg, _ = _cfg(tmp_path, ceiling=200.0, min_available=20)
    state = {"alerts": {"session_dead": datetime.now(timezone.utc).isoformat()}}
    buy._report_rejected(state, cfg, [_row(19800, qty=11)])
    assert len(sent) == 1, "session_dead's throttle must not silence this"
    assert "rejected:rockinrio2026-09-11" in state["alerts"]


def test_a_TELEGRAM_FAILURE_does_not_abort_the_run(monkeypatch, tmp_path, capsys):
    """⚠️ The carve-out CLAUDE.md grants the QR image, for the same reason: this rides
    on a log line that already landed. Turning an advisory into exit 3 would abort the
    loop before the remaining nights are even looked at."""
    monkeypatch.setattr(runner, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(runner, "STATE_DIR", tmp_path)

    def _boom(tok, chat, text):
        raise RuntimeError("telegram 502")
    monkeypatch.setattr(buy.notify, "send_text", _boom)

    cfg, _ = _cfg(tmp_path, ceiling=200.0, min_available=20)
    buy._report_rejected({}, cfg, [_row(19800, qty=11)])      # must NOT raise
    assert "telegram 502" in capsys.readouterr().out
