"""Unattended auto-buy: fire once per night when the market dips under the ceiling.

⛔ This deliberately extends `CLAUDE.md`'s "one reservation per run. No loops". The rule
it replaces it with is NARROWER, not looser, and every clause is load-bearing:

    one reservation per INVOCATION, at most one per night EVER, inside a time window,
    behind an exclusive lock, triggered by data price-watcher already fetched.

## Why it triggers off history instead of polling

price-watcher already reads all 7 nights every minute and writes `history/<id>.jsonl`.
A second poller would nearly double a request budget the README treats as a design
input (~5,472/day today, ~10,080 if every night ran at 1-min). So the trigger is FREE:
it reads the file price-watcher just wrote.

⚠️ The history is a TRIGGER, never the authority. `cmd_buy` re-resolves live before it
spends, so a dip that has already evaporated becomes a `NoMatch` and nothing happens.
Stale-by-a-minute is therefore safe in the only direction that matters.

⛔ **History is a CHANGE LOG, so its age means nothing.** price-watcher appends only
when a reading differs from the last one ("unchanged since the last recording -- nothing
written"). A perfectly healthy poller watching a stable market writes NOTHING, for
hours. An age check over it is therefore not a liveness test, and reading a quiet file
as "no data" is the mistake that bites hardest in exactly the wrong case: a price that
drops to R$150 and then HOLDS is written once and never again, so an age cutoff would
skip the one dip this tool exists to catch. The last reading is carried forward,
whatever its age.

⛔ **Liveness comes from the POLLER, not from the data.** `assert_poller_alive` reads the
one artifact price-watcher touches on every single run whether or not anything changed --
its cron log -- plus the newest history write. If both are old, the cron is genuinely
dead and we are blind; that raises rather than reporting a calm market.
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, listing
from .errors import AutobuyError, ConfigError

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
STATE_PATH = STATE_DIR / "autobuy.json"
LOCK_PATH = STATE_DIR / "autobuy.lock"

#: ⛔ Juan's window is in HIS timezone, and it is pinned here rather than read from the
#: machine. cron inherits whatever TZ the daemon has -- on WSL that is frequently UTC --
#: and a window silently evaluated in UTC would run 07:00-01:00 BRT: it would refuse to
#: buy for the three morning hours he asked for and happily fire at 02:00 while he is
#: asleep. Both halves of the mistake are invisible in a log.
#: ⚠️ History timestamps are UTC and stay UTC; only the WINDOW is local.
TZ = ZoneInfo("America/Sao_Paulo")

#: Juan's call 2026-09-02, confirmed as 10:00->04:00 BRT. Crosses midnight: active
#: 10:00->23:59 and 00:00->04:00, dark 04:00->10:00.
#: ⏰ The Pix hold is ~10 MINUTES and needs a human to pay it, so firing at 06:00
#: reserves a ticket that lapses before anyone sees the alert -- an unpaid hold that
#: bought nothing and still spent antifraud goodwill.
WINDOW_START = time(10, 0)
WINDOW_END = time(4, 0)

#: How stale the POLLER itself may be. price-watcher runs at most every 5 min (the slow
#: lane) and appends to its cron log on every run, so 15 min is a late-cron allowance,
#: not a data-freshness claim. ⛔ Never apply this to a history FILE -- see the module
#: docstring: those are change logs and a quiet one is a stable market, not a dead one.
POLLER_MAX_AGE_S = 15 * 60


def now_local() -> datetime:
    """Wall-clock time in Juan's timezone, whatever the daemon's TZ happens to be."""
    return datetime.now(TZ)


def within_window(now: datetime, start: time = WINDOW_START,
                  end: time = WINDOW_END) -> bool:
    """True inside the active window. Handles a window that crosses midnight, which
    10:00->04:00 does -- a naive `start <= t < end` is False for every hour of it.

    ⚠️ `now` is converted to `TZ` first. Passing a UTC datetime and comparing its naive
    clock face against a local window is the exact bug the TZ constant exists to stop.
    """
    now = now.astimezone(TZ) if now.tzinfo else now.replace(tzinfo=TZ)
    t = now.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def _read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"fired": {}}
    except json.JSONDecodeError as e:
        # ⛔ Absent means "nothing has fired yet" (where every install starts).
        # Unparseable means we do NOT know what has already fired -- and guessing "none"
        # is how one night reserves a second ticket. Same call price-watcher makes for a
        # corrupt subscribers.json.
        raise ConfigError(f"{STATE_PATH}: unreadable state -- {e}. Refusing to run: "
                          f"a wrong answer here reserves a duplicate ticket.") from e


def _write_state(state: dict) -> None:
    """Atomic. A torn write here is a torn ledger, and a torn ledger re-fires."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def assert_poller_alive(history_dir: Path, *, now: datetime | None = None,
                        max_age_s: int = POLLER_MAX_AGE_S) -> None:
    """Raise unless price-watcher's cron has run recently.

    ⭐ Reads `cron.log`, which price-watcher appends to on EVERY run -- including the
    runs where it decides nothing changed and writes no reading. That is what makes it a
    liveness signal when the `.jsonl` files are not: on a stable market they are silent
    by design and the log is not.
    """
    now = now or datetime.now(timezone.utc)
    candidates = []
    log = Path(history_dir) / "cron.log"
    if log.exists():
        candidates.append(log.stat().st_mtime)
    candidates += [f.stat().st_mtime for f in Path(history_dir).glob("*.jsonl")]
    if not candidates:
        raise AutobuyError(f"{history_dir}: no cron log and no history at all -- "
                           f"price-watcher has never run here.")
    age = now.timestamp() - max(candidates)
    if age > max_age_s:
        raise AutobuyError(
            f"price-watcher looks DEAD: nothing in {history_dir} has been touched for "
            f"{age/60:.1f} min (limit {max_age_s/60:.0f}). Refusing to read a stopped "
            f"poller as 'nothing under the ceiling'.")


def latest_readings(history_path: Path, *, now: datetime | None = None) -> list[dict]:
    """The most recent run's readings from a price-watcher history file.

    ⛔ Deliberately has NO age limit. The file is a change log: a stable market produces
    no writes, so age measures volatility, not freshness. The last recorded state is
    carried forward as the current state, which is what "unchanged since the last
    recording" actually means. Liveness is `assert_poller_alive`'s job, once per run.
    """
    now = now or datetime.now(timezone.utc)
    try:
        with history_path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 200_000))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except FileNotFoundError as e:
        raise AutobuyError(f"{history_path}: no history yet -- is price-watcher's cron "
                           f"running? Refusing to treat an absent file as 'no dip'.") from e

    rows = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue                      # a torn last line mid-append; the rest is fine
    if not rows:
        raise AutobuyError(f"{history_path}: no parseable readings.")

    newest = max(r.get("captured_at", "") for r in rows)
    return [r for r in rows if r.get("captured_at") == newest]


def candidate_under_ceiling(readings: list[dict], cfg) -> dict | None:
    """The cheapest reading matching the target's buy filters, or None.

    Mirrors `listing.choose` on history rows rather than live candidates. It is only a
    TRIGGER -- `cmd_buy` re-resolves live and re-applies the real chooser before it
    spends, so a disagreement between the two costs a no-op, never a wrong purchase.
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
        if (r.get("quantity") or 0) < cfg.quantity:
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


def acquire_lock():
    """Exclusive, non-blocking. Returns the fh, or None if another run holds it.

    ⛔ Load-bearing. A buy takes ~40 s and cron fires every minute, so invocations WILL
    overlap. Two overlapping runs both seeing the same dip is precisely "one intended
    ticket becomes two" -- the failure `CLAUDE.md` bans, arriving by schedule instead of
    by retry.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fh = LOCK_PATH.open("w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def already_fired(state: dict, target_id: str) -> bool:
    return target_id in (state.get("fired") or {})


def record_fired(state: dict, target_id: str, *, price_cents: int,
                 order_url: str | None, item: str) -> dict:
    state.setdefault("fired", {})[target_id] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "price_cents": price_cents, "item": item, "order_url": order_url,
    }
    return state


def disarm_target(path: Path) -> None:
    """Belt and braces: flip `buy.enabled` false in the target itself.

    The ledger is the fast check; this is the durable one. If the state file is ever
    lost, a target that has already bought must not silently become eligible again.
    ⚠️ Written atomically -- price-watcher parses this same file every minute and a torn
    write would turn a successful purchase into an AdapterError on the watcher.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw.get("buy"), dict):
        return
    raw["buy"]["enabled"] = False
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


#: How often a recurring problem may re-alert. ⛔ Without a throttle a dead session on a
#: 1-minute cron sends 1,440 identical Telegram messages a day, which trains the reader
#: to mute the channel -- and the muted channel is the one the Pix code arrives on.
ALERT_EVERY_S = 30 * 60


def should_alert(state: dict, key: str, *, every_s: int = ALERT_EVERY_S,
                 now: datetime | None = None) -> bool:
    """True if `key` has not alerted within `every_s`. Records the decision."""
    now = now or datetime.now(timezone.utc)
    last = (state.get("alerts") or {}).get(key)
    if last:
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() < every_s:
                return False
        except ValueError:
            pass                          # unparseable timestamp: alert, then fix it
    state.setdefault("alerts", {})[key] = now.isoformat(timespec="seconds")
    return True
