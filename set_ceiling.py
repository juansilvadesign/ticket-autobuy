#!/usr/bin/env python3
"""Raise one night's price ceiling at a wall-clock deadline. Buys nothing, ever.

    python set_ceiling.py rockinrio2026-09-11 2026-09-10T00:00 250

Sibling of `stop_night.py`, and deliberately its mirror image: that one takes a night
away at a deadline, this one widens it. Juan's 2026-09-06 call for the 11/09 night is a
three-step ladder -- R$200 now, R$250 the day before, R$300 on event day -- and the
value of a ticket rises as the event approaches while the market does not care.

⛔ The deadline is evaluated in America/Sao_Paulo via `runner.TZ`, NEVER in the cron
daemon's timezone. cron on this WSL box inherits whatever the daemon has -- frequently
UTC -- and a midnight step read as UTC applies at 21:00 BRT the day before: a whole
evening bought at the wrong ceiling, invisibly. Same rule as the buy window in
`runner.py`.

⭐ **The step is MONOTONE: it only ever raises, never lowers.** That is what makes it
safe to leave on a coarse cron forever, and it is why this script has no expiry window
where `stop_night.py` has one. The two failure modes are not symmetric:

  - `stop_night` failing to run leaves a night ARMED past its deadline, so it must act
    inside a bounded window and is written to stop mattering by itself.
  - `set_ceiling` failing to run leaves the ceiling LOW, which spends nothing. But a
    WSL box that was asleep from 23:00 to 07:00 -- an ordinary night here -- would miss
    a 6-hour window entirely and quietly hold R$200 through the day we meant to pay
    R$250. So it keeps re-asserting instead, and a re-run after the value is already
    set is a no-op rather than a second write.

Because it only raises, the ladder's steps are also order-independent: once the R$300
step has applied, the R$250 step reads the file, sees 300 >= 250, and does nothing.

⛔ It refuses to touch a DISARMED night. That is the self-terminating clause, and it
carries the lesson of 2026-09-05: 04/09 was left armed at a temporary R$1.000 ceiling
after its event ended, and every cron minute was a chance to reserve a R$1.000 ticket
for a night that was already over. Here, both endings -- the buy itself and the 16:00
hard stop -- set `buy.enabled: false`, and this script goes inert the moment either
happens. A ladder that kept climbing over a finished night would rebuild that exact bug.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from autobuy import config, runner                                   # noqa: E402

TARGETS = HERE.parent / "price-watcher" / "targets"


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 1
    target_id, deadline_s, brl = argv[1], argv[2], argv[3]

    # ⛔ Parsed through `config.brl_to_cents`, not `float(brl) * 100`. Same Decimal path
    # the ceiling itself is loaded by, so the comparison below cannot disagree with the
    # one `runner.candidate_under_ceiling` makes an hour later on the same number.
    want_cents = config.brl_to_cents(brl, "set_ceiling: <brl>")

    deadline = datetime.fromisoformat(deadline_s).replace(tzinfo=runner.TZ)
    now = runner.now_local()
    if now < deadline:
        return 0                                   # not yet: silent no-op

    path = TARGETS / f"{target_id}.json"
    if not path.exists():
        print(f"set_ceiling: no such target {path}", file=sys.stderr)
        return 1

    raw = json.loads(path.read_text(encoding="utf-8"))
    buy = raw.get("buy")
    if not isinstance(buy, dict):
        print(f"set_ceiling: {target_id} has no 'buy' block", file=sys.stderr)
        return 1
    if buy.get("enabled") is False:
        return 0                                   # bought, or hard-stopped: inert
    if "max_price_brl" in buy and config.brl_to_cents(
            buy["max_price_brl"], f"{path}: buy.max_price_brl") >= want_cents:
        return 0                                   # already at or above: no-op

    # ⛔ Under the lock: this rewrites a file a live buy may be reading, and the
    # ledger/target pair is shared state. A read-modify-write outside this lock was
    # silently clobbered by a concurrent cron run on 2026-09-04.
    lock = None
    for _ in range(20):
        lock = runner.acquire_lock()
        if lock:
            break
        time.sleep(3)
    if lock is None:
        print("set_ceiling: could not take the lock; will retry next run", file=sys.stderr)
        return 0                                   # a later run re-asserts; it is monotone
    try:
        # ⚠️ Re-read INSIDE the lock. The check above ran outside it, and a buy that
        # completed in between will have disarmed the night and written the file; acting
        # on the stale copy would re-arm a night that has already spent money.
        raw = json.loads(path.read_text(encoding="utf-8"))
        buy = raw.get("buy")
        if not isinstance(buy, dict) or buy.get("enabled") is False:
            return 0
        old = buy.get("max_price_brl")
        if old is not None and config.brl_to_cents(
                old, f"{path}: buy.max_price_brl") >= want_cents:
            return 0
        buy["max_price_brl"] = float(want_cents) / 100

        # Atomic, exactly as `runner.disarm_target` is: price-watcher parses this same
        # file every minute and a torn write would surface there as an AdapterError.
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, path)
        print(f"set_ceiling: {target_id} ceiling {old} -> {buy['max_price_brl']:.2f} "
              f"at {now:%Y-%m-%d %H:%M} {now.tzname()} (step due {deadline:%d/%m %H:%M})")
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
