#!/usr/bin/env python3
"""Disarm one night at a wall-clock deadline. Buys nothing, ever.

    python stop_night.py rockinrio2026-09-05 2026-09-05T16:00

⛔ The deadline is evaluated in America/Sao_Paulo via `runner.TZ`, NEVER in the cron
daemon's timezone. cron on this WSL box inherits whatever the daemon has -- frequently
UTC -- and a 16:00 cutoff read as UTC fires at 13:00 BRT: three hours early, on a night
that still had four hours of buying left, and invisibly. Same rule as the buy window in
`runner.py`.

⏱ Idempotent and self-limiting: it acts only inside [deadline, deadline + 6h), and
disarming an already-disarmed night is a no-op. So it is safe to run from a coarse
every-few-minutes cron line, and it stops mattering by itself once the window passes.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from autobuy import runner                                            # noqa: E402

WINDOW = timedelta(hours=6)
TARGETS = HERE.parent / "price-watcher" / "targets"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 1
    target_id, deadline_s = argv[1], argv[2]
    deadline = datetime.fromisoformat(deadline_s).replace(tzinfo=runner.TZ)
    now = runner.now_local()
    if not (deadline <= now < deadline + WINDOW):
        return 0                                   # outside the window: silent no-op

    path = TARGETS / f"{target_id}.json"
    if not path.exists():
        print(f"stop_night: no such target {path}", file=sys.stderr)
        return 1
    if json.loads(path.read_text(encoding="utf-8")).get("buy", {}).get("enabled") is False:
        return 0                                   # already disarmed (or already bought)

    # ⛔ Under the lock: `disarm_target` rewrites a file a live buy may be reading, and
    # the ledger/target pair is shared state. A read-modify-write outside this lock was
    # silently clobbered by a concurrent cron run on 2026-09-04.
    lock = None
    for _ in range(20):
        lock = runner.acquire_lock()
        if lock:
            break
        time.sleep(3)
    if lock is None:
        print("stop_night: could not take the lock; will retry next run", file=sys.stderr)
        return 0                                   # a later run inside the window retries
    try:
        runner.disarm_target(path)
        print(f"stop_night: {target_id} DISARMED at {now:%Y-%m-%d %H:%M} {now.tzname()} "
              f"(deadline {deadline:%H:%M}) -- nothing will be bought for this night.")
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
