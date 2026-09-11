"""Phase timing.

This tool's entire value proposition is being faster than the human doing it by hand.
Juan does the manual flow in ~60 s and the listing is frequently gone inside that, so
the number that decides whether this is worth running is not "did it work" but "how
long from the alert to a payable Pix code".

⛔ A total alone is useless for that. When the answer is "too slow", the only actionable
form is a per-phase breakdown -- the Bubble checkout's first paint was MEASURED at 37.8 s
headless, so a tool that reports only "94 s" invites optimising the 3 s of parsing while
the 38 s wall sits untouched.
"""

from __future__ import annotations

import time


class Clock:
    """Monotonic phase clock. `mark()` closes the current phase and opens the next."""

    def __init__(self, label: str = "") -> None:
        self.label = label
        self.t0 = time.monotonic()
        self.marks: list[tuple[str, float]] = []

    def mark(self, name: str) -> float:
        prev = self.t0 + sum(d for _, d in self.marks)
        delta = time.monotonic() - prev
        self.marks.append((name, delta))
        return delta

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    def report(self, *, budget_s: float | None = None,
               walk_budget_s: float | None = None,
               walk_mark: str = "ORDER CREATED (final click)") -> str:
        w = max((len(n) for n, _ in self.marks), default=12)
        out = [f"⏱  {self.label}"]
        for name, delta in self.marks:
            bar = "█" * min(int(delta), 40)
            out.append(f"   {delta:7.2f}s  {name:<{w}}  {bar}")
        out.append(f"   {'-' * (9 + w)}")
        out.append(f"   {self.elapsed:7.2f}s  TOTAL")
        if budget_s is not None:
            slack = budget_s - self.elapsed
            verdict = "✅ inside" if slack >= 0 else "❌ OVER"
            out.append(f"   total  {budget_s:.0f}s (report only) -> {verdict} "
                       f"by {abs(slack):.2f}s")
        if walk_budget_s is not None:
            # ⛔🔴 The ENFORCED number, and it must be printed next to the reported one
            # or the reader keeps optimising against the wrong span. On 2026-09-10 the
            # printed verdict said "❌ OVER by 22.79s" while the walk -- the only part
            # that could still be aborted -- was comfortably inside. The total was the
            # loud number and the walk was the load-bearing one.
            walk = self.walk_elapsed(walk_mark)
            if walk is not None:
                wslack = walk_budget_s - walk
                wverdict = "✅ inside" if wslack >= 0 else "❌ OVER"
                out.append(f"   walk   {walk_budget_s:.0f}s (ENFORCED, resolve->click) "
                           f"= {walk:.2f}s -> {wverdict} by {abs(wslack):.2f}s")
        return "\n".join(out)

    def walk_elapsed(self, mark: str) -> float | None:
        """Seconds from t0 up to and including `mark`, or None if it never happened.

        ⭐ The exposure window: everything before the order-creating click. A run that
        never reached the click has no walk to report -- and saying nothing is correct,
        because a fabricated number here would be indistinguishable from a real one.
        """
        total = 0.0
        for name, delta in self.marks:
            total += delta
            if name == mark:
                return total
        return None
