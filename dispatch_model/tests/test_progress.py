"""`powersim_core.progress` — progress that stays readable in a redirected log.

Every long run in this repo is launched with `> run.log 2>&1`, so the property that matters is not the
bar: it is that a non-TTY stream gets ORDINARY LINES at a bounded rate. A carriage-return bar written to a
file turns a four-hour run's log into one unreadable line, and that log is the only record of the run.
"""
from __future__ import annotations

import io
import time

from powersim_core.progress import Progress


class _Tty(io.StringIO):
    def isatty(self):
        return True


def _plain():
    return io.StringIO()          # StringIO.isatty() is False -> the log path


def test_non_tty_writes_plain_lines_and_never_carriage_returns():
    s = _plain()
    with Progress(4, "job", stream=s, min_interval=0.0) as p:
        for _ in range(4):
            p.update()
    out = s.getvalue()
    assert "\r" not in out, "a redirected log must never receive carriage returns"
    assert out.count("\n") == 5              # the initial 0/4 plus one per item
    assert "4/4 100%" in out


def test_non_tty_rate_limits_but_always_shows_the_last_item():
    """A four-hour run must not emit 175 000 lines, and must not stop at 19/20 either."""
    s = _plain()
    with Progress(50, "job", stream=s, min_interval=999.0) as p:
        for _ in range(50):
            p.update()
    lines = [x for x in s.getvalue().splitlines() if x.strip()]
    assert len(lines) == 2, f"expected first + last only, got {len(lines)}"
    assert "50/50 100%" in lines[-1]


def test_tty_renders_in_place():
    s = _Tty()
    with Progress(4, "job", stream=s) as p:
        for _ in range(4):
            p.update()
    out = s.getvalue()
    assert "\r" in out and "|" in out        # bar only on a terminal
    assert out.endswith("\n"), "close() must leave the cursor on a fresh line"


def test_eta_and_rate_appear_once_progress_exists():
    s = _plain()
    with Progress(10, "job", stream=s, min_interval=0.0) as p:
        time.sleep(0.05)
        p.update()
    last = [x for x in s.getvalue().splitlines() if x.strip()][-1]
    assert "eta" in last and "/it" in last
    assert "--:--:--" not in last            # a real estimate once one item is done


def test_unknown_total_degrades_to_a_counter():
    s = _plain()
    with Progress(0, "stream", stream=s, min_interval=0.0) as p:
        p.update(); p.update()
    out = s.getvalue()
    assert "2 done" in out and "%" not in out


def test_close_is_idempotent_and_does_not_repeat_the_final_line():
    s = _plain()
    p = Progress(2, "job", stream=s, min_interval=0.0)
    p.__enter__()
    p.update(); p.update()
    p.close(); p.close()
    assert s.getvalue().count("2/2") == 1


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("POWERSIM_NO_PROGRESS", "1")
    s = _plain()
    with Progress(3, "job", stream=s, min_interval=0.0) as p:
        p.update()
    assert s.getvalue() == ""


def test_a_broken_stream_never_breaks_the_run():
    """Telemetry must not be able to kill a four-hour projection."""
    class Hostile(io.StringIO):
        def write(self, *_a, **_k):
            raise OSError("pipe closed")
    p = Progress(2, "job", stream=Hostile(), min_interval=0.0)
    p.__enter__()
    p.update(); p.close()                    # must not raise
