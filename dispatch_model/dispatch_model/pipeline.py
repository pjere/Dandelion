"""Orchestration entry points behind the CLI — thin wrappers over the rolling engines."""
from __future__ import annotations

from .config import Config


def build_inputs(config: Config, year: int = 2019):
    """Assemble one week of LP inputs as a smoke check of the input chain (commodities, stacks, net loads)."""
    from .rolling.assemble import assemble_window
    times, zones_data, borders, ntc = assemble_window(config, f"{year}-01-07", f"{year}-01-14")
    print(f"inputs OK: {len(times)} h, zones {list(zones_data)}, {len(borders)} borders")
    return times, zones_data, borders, ntc


def run(config: Config, years: list[int] | None = None, ref_year: int = 2019, **kw):
    """Projection run → per-zone annual price stats (rolling.projection.project_trajectory)."""
    from .rolling.projection import project_trajectory
    stats = project_trajectory(config, years or [2030], ref_year=ref_year, **kw)
    print(stats.to_string(index=False))
    return stats


def backtest(config: Config, year: int = 2019, **kw):
    """Historical-year backtest → §8 price metrics (rolling.backtest.run_backtest)."""
    from .rolling.backtest import run_backtest
    out = run_backtest(config, year, **kw)
    print(out["metrics"].to_string(index=False))
    return out


def run_validation(config: Config, year: int = 2019, **kw):
    """Validation = the scored backtest (the §8 metrics table is the acceptance gate)."""
    return backtest(config, year, **kw)
