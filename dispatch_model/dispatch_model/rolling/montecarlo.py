"""Parallel Monte-Carlo projection — N draws across CPU cores, each byte-identical to a serial run.

One projection trajectory is ~11 min (step-vii, `lp/highs_solver`); a weathergen Monte-Carlo needs many.
The draws are **embarrassingly parallel** and each stays exact, so this runs them across a process pool
and the ensemble wall-clock is (n_draws / n_cores) × one trajectory instead of n_draws × one.

Two invariants make parallel == serial:

  * the **reference preload is deterministic and draw-independent**, so each worker loads it once
    (`_preload`) and reuses it for every draw it handles — the ~250 s preload is paid once per core,
    not once per draw;
  * every draw's randomness comes from ``powersim_core.rng.draw_rng(master_seed, draw)`` — a
    ``SeedSequence`` child keyed by the draw id, **independent of process/order**. So draw *d* produces
    the identical trajectory whether it runs serially or on any worker, and the ensemble is reproducible.

The stochastic variation is pluggable: the #80 REMIT neighbour-availability spread (per-draw ``avail_rng``,
active when ``avail_years`` loads the REMIT stats) and/or the #77 weather-coherent net loads (pass a
picklable module-level ``weather_provider(config, year, draw) -> {zone: shape_df}``). With neither, all
draws equal the deterministic central path (a valid degenerate ensemble).
"""
from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

# per-worker process cache (populated once by the pool initializer); never pickled across the boundary
_WORKER: dict = {}


def _init_worker(config_path: str, ref_year: int, avail_years: list[int] | None) -> None:
    from ..config import load_config
    from .projection import _preload
    cfg = load_config(config_path)
    _WORKER["cfg"] = cfg
    _WORKER["ref"] = _preload(cfg, ref_year, avail_years=avail_years)


def _draw_summary(draw: int, spot: pd.DataFrame, year: int) -> pd.DataFrame:
    """Compact per-(year, zone) stats for one draw — what an ensemble needs, without shipping 8760×7 hours."""
    rows = []
    for z in spot.columns:
        s = spot[z].dropna().to_numpy(float)
        if not s.size:
            continue
        rows.append({"draw": draw, "year": year, "zone": z, "mean": s.mean(),
                     "p5": np.quantile(s, 0.05), "p50": np.quantile(s, 0.50), "p95": np.quantile(s, 0.95),
                     "min": s.min(), "max": s.max(), "neg_hours": int((s < 0).sum())})
    return pd.DataFrame(rows)


def _run_draw(cfg, ref, draw: int, years, master_seed: int, weather_provider, n_weeks, write_lake: bool):
    """Run one draw's trajectory over `years`; return its compact summary. Runs in-process (worker or serial)."""
    from powersim_core import rng as _rng

    from .projection import project_year
    avail_rng = _rng.draw_rng(master_seed, draw) if ref.get("avail_stats") else None
    parts, summ = [], []
    for y in years:
        shapes = weather_provider(cfg, y, draw) if weather_provider is not None else None
        _stats, spot = project_year(cfg, y, ref, n_weeks=n_weeks, avail_rng=avail_rng,
                                    weather_shapes=shapes, return_prices=True)
        summ.append(_draw_summary(draw, spot, y))
        if write_lake:
            parts.append(spot.assign(year=y))
    if write_lake:
        from powersim_core import lake
        lake.write_table(pd.concat(parts), "dispatch", "projection_prices", index=True, realization=draw)
    return pd.concat(summ, ignore_index=True) if summ else pd.DataFrame()


def _worker_entry(args):
    draw, years, master_seed, weather_provider, n_weeks, write_lake = args
    return _run_draw(_WORKER["cfg"], _WORKER["ref"], draw, years, master_seed,
                     weather_provider, n_weeks, write_lake)


def run_ensemble(config_path: str, years, draws, ref_year: int = 2019, master_seed: int = 0,
                 n_workers: int | None = None, avail_years: list[int] | None = None,
                 weather_provider=None, n_weeks: int | None = None, write_lake: bool = False,
                 parallel: bool = True) -> pd.DataFrame:
    """Run `draws` projection trajectories over `years` and return the stacked per-draw summary
    [draw, year, zone, mean, p5, p50, p95, min, max, neg_hours].

    `config_path` (a path, not a live Config) is what workers reload — the preload is redone once per
    worker, not shipped. `weather_provider` must be a top-level importable function for pickling.
    `parallel=False` runs in-process (for debugging / determinism checks). Full hourly trajectories are
    written to the lake per draw only when `write_lake=True`.
    """
    draws = list(draws)
    args = [(d, list(years), master_seed, weather_provider, n_weeks, write_lake) for d in draws]
    if not parallel or len(draws) == 1:
        from ..config import load_config
        from .projection import _preload
        cfg = load_config(config_path)
        ref = _preload(cfg, ref_year, avail_years=avail_years)
        frames = [_run_draw(cfg, ref, d, years, master_seed, weather_provider, n_weeks, write_lake)
                  for d in draws]
    else:
        n_workers = n_workers or min(len(draws), os.cpu_count() or 1)
        with ProcessPoolExecutor(max_workers=n_workers, initializer=_init_worker,
                                 initargs=(config_path, ref_year, avail_years)) as ex:
            frames = list(ex.map(_worker_entry, args))
    out = pd.concat([f for f in frames if not f.empty], ignore_index=True) if frames else pd.DataFrame()
    return out.sort_values(["draw", "year", "zone"]).reset_index(drop=True) if not out.empty else out


def ensemble_stats(per_draw: pd.DataFrame) -> pd.DataFrame:
    """Cross-draw ensemble statistics per (year, zone) from `run_ensemble` output: the spread of the
    per-draw mean spot price (central path + P5/P50/P95 across draws) and the draw count."""
    g = per_draw.groupby(["year", "zone"])["mean"]
    return pd.DataFrame({"n_draws": g.size(), "ens_mean": g.mean(),
                         "ens_p5": g.quantile(0.05), "ens_p50": g.quantile(0.50),
                         "ens_p95": g.quantile(0.95)}).reset_index()
