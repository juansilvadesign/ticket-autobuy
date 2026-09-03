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

    def report(self, *, budget_s: float | None = None) -> str:
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
            out.append(f"   budget {budget_s:.0f}s -> {verdict} by {abs(slack):.2f}s")
        return "\n".join(out)
