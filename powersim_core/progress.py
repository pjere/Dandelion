"""Progress + ETA for the long-running steps, readable both on a terminal and in a redirected log.

Every long run in this repo is launched as `python ... > run.log 2>&1`, so a carriage-return bar would
write one unreadable mega-line into the file that matters most. This renders two ways:

  * **TTY** — an in-place bar refreshed at most every 0.5 s;
  * **not a TTY** — the same content as ordinary lines, at most one every `min_interval` seconds (default
    30) plus always the first and the last. A four-hour projection then leaves ~500 legible lines instead
    of 175 000 control sequences.

Nesting is handled rather than forbidden: a 20-year projection has a year loop around a weekly-window
loop. Only the INNERMOST bar renders in place (it has the finer granularity and the better ETA); outer
levels fall back to plain lines so the two never fight over the same terminal row.

ETA is elapsed/done x remaining — a mean rate, not a fit. For heterogeneous items (a 2046 window solves
slower than a 2027 one) it drifts early and settles; the rate is printed alongside so a drifting estimate
is visible as such rather than merely wrong.

    with Progress(len(years), "projection") as p:
        for y in years:
            ...
            p.update(note=f"{y} done")

Set `POWERSIM_NO_PROGRESS=1` to silence it everywhere.
"""
from __future__ import annotations

import os
import sys
import time

_DEPTH = [0]                     # active bars, so only the innermost renders in place
_BLOCKS = "▏▎▍▌▋▊▉█"


def _hms(sec: float) -> str:
    if sec != sec or sec in (float("inf"), float("-inf")) or sec < 0:
        return "--:--:--"
    sec = int(sec)
    return f"{sec // 3600:d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def _rate(sec_per_it: float) -> str:
    if sec_per_it <= 0 or sec_per_it != sec_per_it:
        return "?"
    if sec_per_it >= 90:
        return f"{sec_per_it / 60:.1f} min/it"
    return f"{sec_per_it:.1f} s/it"


def _bar(frac: float, width: int = 24) -> str:
    frac = min(max(frac, 0.0), 1.0)
    full, rem = divmod(int(frac * width * 8), 8)
    return ("█" * full + (_BLOCKS[rem - 1] if rem else "")).ljust(width, "·")


class Progress:
    """Counter with an ETA. `total<=0` degrades to a plain counter (no bar, no ETA)."""

    def __init__(self, total: int, label: str = "", *, stream=None, min_interval: float = 30.0,
                 enabled: bool | None = None):
        self.total = int(total or 0)
        self.label = label
        self.stream = stream or sys.stdout
        self.min_interval = float(min_interval)
        self.enabled = (os.environ.get("POWERSIM_NO_PROGRESS", "") in ("", "0", "false", "False")
                        if enabled is None else bool(enabled))
        self.done = 0
        self.t0 = time.monotonic()
        self._last = 0.0
        self._shown = -1          # `done` at the last emit, so close() cannot repeat a finished line
        self._inplace = False
        self._dirty = False              # an in-place line is on screen and needs a newline before output

    def __enter__(self):
        if self.enabled:
            _DEPTH[0] += 1
            # only the innermost bar animates; a TTY is required for the cursor to return
            self._inplace = bool(getattr(self.stream, "isatty", lambda: False)()) and _DEPTH[0] >= 1
            self._emit(force=True)
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def _line(self) -> str:
        el = time.monotonic() - self.t0
        spi = el / self.done if self.done else float("nan")
        head = f"[{self.label}] " if self.label else ""
        if self.total > 0:
            frac = self.done / self.total
            eta = spi * (self.total - self.done) if self.done else float("nan")
            return (f"{head}{self._bartext(frac)}{self.done}/{self.total} {frac * 100:3.0f}%  "
                    f"elapsed {_hms(el)}  eta {_hms(eta)}  {_rate(spi)}")
        return f"{head}{self.done} done  elapsed {_hms(el)}  {_rate(spi)}"

    def _bartext(self, frac: float) -> str:
        return f"|{_bar(frac)}| " if self._inplace else ""

    def _emit(self, force: bool = False, note: str = "") -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        interval = 0.5 if self._inplace else self.min_interval
        if not force and (now - self._last) < interval:
            return
        self._last = now
        self._shown = self.done
        txt = self._line() + (f"  {note}" if note else "")
        try:
            if self._inplace:
                self.stream.write("\r" + txt + "\033[K")
                self._dirty = True
            else:
                self.stream.write(txt + "\n")
            self.stream.flush()
        except Exception:                                  # noqa: BLE001 — telemetry must never break a run
            self.enabled = False

    def update(self, n: int = 1, note: str = "") -> None:
        self.done += int(n)
        # the last item always prints: a run must end on a complete line, not at 19/20
        self._emit(force=(self.total > 0 and self.done >= self.total), note=note)

    def write(self, msg: str) -> None:
        """Print a message without leaving a half-drawn bar behind it."""
        if self._dirty:
            try:
                self.stream.write("\n")
            except Exception:                              # noqa: BLE001
                pass
            self._dirty = False
        print(msg, file=self.stream, flush=True)

    def close(self, note: str = "") -> None:
        if not self.enabled:
            return
        if self._shown != self.done or note:      # already ended on a complete line -> do not repeat it
            self._emit(force=True, note=note)
        if self._dirty:
            try:
                self.stream.write("\n")
                self.stream.flush()
            except Exception:                              # noqa: BLE001
                pass
            self._dirty = False
        _DEPTH[0] = max(0, _DEPTH[0] - 1)
        self.enabled = False                               # idempotent: __exit__ after an explicit close
