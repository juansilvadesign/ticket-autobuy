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


#: The one reason that is never worth a word: the market sitting above your ceiling is
#: the ordinary state of almost every run, on every night, forever.
ORDINARY = "over-ceiling"


def reject_reason(r: dict, cfg) -> str | None:
    """Why this reading cannot be bought, or None if it can.

    ⭐ ONE source of truth for the trigger's filters. `candidate_under_ceiling` keeps
    the rows this returns None for and `rejected_under_ceiling` reports the rest, so
    the two can never disagree about what was refused. A second copy of these
    conditions would drift, and the copy that drifts is the one deciding whether you
    are TOLD about a dip you did not buy.

    ⚠️ The checks are ordered so the reason is the most informative one, not the first
    one that happens to match. Which row is *accepted* does not depend on the order --
    it is a conjunction -- so this is free.
    """
    price = r.get("price_cents")
    if not isinstance(price, int) or price <= 0:
        return "no usable price in the reading"
    if price > cfg.max_price_cents:
        return ORDINARY
    if not r.get("available", True):
        return "the poller recorded it as no longer available"
    if cfg.min_price_cents and price < cfg.min_price_cents:
        return (f"below the anomaly floor of "
                f"{cfg.min_price_cents} centavos (buy.min_price_brl)")
    need = max(cfg.quantity, cfg.min_available)
    if (r.get("quantity") or 0) < need:
        # ⭐ same depth floor as `listing.choose`: triggering on a 1-unit row the
        # chooser will reject only spends a browser launch on a guaranteed NoMatch,
        # and every launch is an antifraud event.
        return (f"only {r.get('quantity') or 0} available and buy.min_available "
                f"demands {need}")
    extra = r.get("extra") or {}
    if cfg.sectors and (extra.get("sector") or "").lower() not in \
            {s.lower() for s in cfg.sectors}:
        return f"sector {extra.get('sector')!r} is not in buy.sector {cfg.sectors}"
    if cfg.entry_classes and (extra.get("entry_class") or "").lower() not in \
            {c.lower() for c in cfg.entry_classes}:
        return (f"class {extra.get('entry_class')!r} is not in buy.entry_class "
                f"{cfg.entry_classes}")
    return None


def candidate_under_ceiling(readings: list[dict], cfg) -> dict | None:
    """The cheapest reading matching the target's buy filters, or None.

    Mirrors `listing.choose` on history rows rather than live candidates. It is only a
    TRIGGER -- `cmd_buy` re-resolves live and re-applies the real chooser before it
    spends, so a disagreement between the two costs a no-op, never a wrong purchase.
    """
    pool = [r for r in readings if reject_reason(r, cfg) is None]
    return min(pool, key=lambda r: (r["price_cents"], -(r.get("quantity") or 0))) \
        if pool else None


def rejected_under_ceiling(readings: list[dict], cfg) -> list[tuple[dict, str]]:
    """Rows at or under the ceiling that a filter still refused, cheapest first.

    ⛔🔴 Exists because `candidate_under_ceiling`'s silence was AMBIGUOUS. It returns
    None both for "the market is above your ceiling" -- the ordinary outcome of
    almost every run -- and for "a ticket was under your ceiling and a filter said no".
    Those two produced byte-identical output: nothing, in any log, in any channel.

    Observed 2026-09-06 and 2026-09-07, on the 11/09 night at a R$200,00 ceiling:

        Gramado || Meia Jovem Baixa Renda  R$198,00  qty 12   7 polls
        Gramado || Meia PCD                R$198,00  qty 11  10 polls

    price-watcher sent `CRITICAL R$ 198,00 -- under R$ 200,00` for both. `autobuy.log`
    contains ZERO lines naming either listing: `min_available: 20` rejected them before
    anything was printed. From the outside the buyer was indistinguishable from a
    buyer watching a calm market -- and the ceiling was the number everyone was
    looking at, so nobody suspected a second filter existed.
    ⭐ `ORDINARY` is excluded on purpose: a signal that also fires on a normal market
    is a signal that gets muted, and the muted channel is the one the Pix code arrives
    on.
    """
    out = [(r, reason) for r in readings
           if (reason := reject_reason(r, cfg)) not in (None, ORDINARY)]
    return sorted(out, key=lambda rr: rr[0].get("price_cents") or 0)


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


def target_id_of(path: Path) -> str | None:
    """The target's `id`, read WITHOUT validating the buy block.

    ⛔🔴 Why this exists, and it is the whole 2026-09-11 bug: `disarm_target` and
    `record_fired` fire TOGETHER, so from the next cron minute onward `config.load`
    raises `ConfigError` (disabled) and `already_fired` is never reached. The ledger
    guard was structurally UNREACHABLE in production -- a declaration nothing asserted.
    Reading the id straight off the JSON is what lets the ledger be consulted before
    the buy block is validated, which is the only order in which a DISARMED night can
    still announce itself.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    tid = raw.get("id")
    return tid if isinstance(tid, str) and tid else None


def already_fired(state: dict, target_id: str) -> bool:
    return target_id in (state.get("fired") or {})


def fired_entry(state: dict, target_id: str) -> dict | None:
    """The ledger row for `target_id`, or None. Carries `order_url`, which is how an
    UNCONFIRMED reservation is told apart from a clean, finished buy."""
    e = (state.get("fired") or {}).get(target_id)
    return e if isinstance(e, dict) else None


def is_unconfirmed(entry: dict | None) -> bool:
    """True when the ledger row was written by the fail-CLOSED path.

    `order_url` is the discriminator: the happy path stores a real URL, and the
    `OrderMayExistError` path stores the sentence beginning `UNCONFIRMED`. An
    unconfirmed row means a reservation MAY be live and a human still has to look --
    it is the one ledger state that must keep nagging.
    """
    if not entry:
        return False
    url = entry.get("order_url")
    return not isinstance(url, str) or not url.startswith("http")


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

#: ⚠️ A STEADY state, not an event: a disarmed night stays disarmed, so 30 min would
#: send 48 messages a day about a condition that has not changed -- which is the same
#: mute-the-channel failure `ALERT_EVERY_S` exists to prevent, arriving from the other
#: direction. 6 h is four reminders a day: often enough that 25 silent hours cannot
#: happen again, rare enough to stay readable.
INERT_ALERT_EVERY_S = 6 * 3600


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
