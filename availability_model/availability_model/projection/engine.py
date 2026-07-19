"""Phase 6 — projection engine: assemble one coherent availability series per draw.

For each draw it stitches the four processes into a single per-unit daily available-capacity series and
aggregates to available capacity by technology:

    available = capacity, then × derating factor on hot days, then 0 on any offline day,
    where offline = planned ∪ forced ∪ common-mode   (UNION, never summed — a unit offline in two
    categories is still just offline, so capacity is subtracted once).

Weather (temperature for derating, wetness for the reservoir budget) is the shared cube — identical
across draws; only the outages vary. Scenario knobs (closures / new builds, ±10 % forced correction)
come from the workbook. Outputs partitioned Parquet + a reproducibility metadata stamp.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from powersim_core import lake

from ..calibration.model import CalibratedAvailability
from ..config import Config
from ..meta import run_metadata
from .common_mode import simulate_common_mode
from .derating import thermal_derating
from .forced import simulate_forced
from .hydro import reservoir_energy_budget
from .interconnectors import interconnector_availability
from .planned_scheduler import schedule_planned

_STATE = {0: "available", 1: "planned", 2: "forced", 3: "common_mode", 4: "derated"}


def _horizon(config: Config):
    proj = config.section("projection")
    y0, y1 = proj["horizon"]["start_year"], proj["horizon"]["end_year"]
    hstart = pd.Timestamp(year=y0, month=1, day=1, tz="UTC")
    days = pd.date_range(hstart, pd.Timestamp(year=y1, month=12, day=31, tz="UTC"), freq="D", tz="UTC")
    return hstart, days, len(days)


def load_scenario_registry(config: Config):
    """Registry with scenario overrides: prefer the workbook fleet_registry (user closures / new builds),
    else the DB-built registry."""
    from ..io.assumptions import load_assumptions
    from ..io.fleet import build_fleet_registry
    wb = config.resolve(config.section("assumptions")["workbook"])
    if wb and wb.exists():
        try:
            reg = load_assumptions(wb)["fleet_registry"].copy()
            for c in ("commissioning_year", "closure_year"):
                reg[c] = pd.to_numeric(reg.get(c), errors="coerce").astype("Int64")
            return reg
        except (KeyError, ValueError):
            pass
    return build_fleet_registry(config)


def _user_corrections(config: Config) -> dict:
    from ..io.assumptions import load_assumptions
    wb = config.resolve(config.section("assumptions")["workbook"])
    if not (wb and wb.exists()):
        return {}
    try:
        fp = load_assumptions(wb)["forced_outage_params"]
    except (KeyError, ValueError):
        return {}
    uc = fp[fp["variable"] == "user_correction_pct"]
    return {str(k): float(v) for k, v in zip(uc["key"], uc["value"]) if pd.notna(v)}


def _set_ranges(mat: np.ndarray, urow: dict, events: pd.DataFrame, hstart, n_days, value, state=None,
                state_mat=None, state_code=None):
    """Zero (or mark) day-ranges for each event, mapped by unit row index."""
    if events is None or events.empty:
        return
    for r in events.itertuples(index=False):
        ui = urow.get(r.unit_id)
        if ui is None:
            continue
        i0 = max(0, (r.start.normalize() - hstart).days)
        i1 = min(n_days, (r.end.normalize() - hstart).days + 1)
        if i1 > i0:
            mat[ui, i0:i1] = value
            if state_mat is not None:
                state_mat[ui, i0:i1] = state_code


def _assemble_draw(config, model, registry, draw, temp_daily, hstart, days, n_days):
    urow = {u: i for i, u in enumerate(registry["unit_id"])}
    cap = registry["capacity_mw"].to_numpy(float)
    avail = np.repeat(cap[:, None], n_days, axis=1)                 # start at full capacity
    state = np.zeros((len(registry), n_days), dtype=np.int8)

    # operating window: zero outside [commissioning, closure]
    yrs = days.year.to_numpy()
    comm = pd.to_numeric(registry["commissioning_year"], errors="coerce").to_numpy()
    clos = pd.to_numeric(registry["closure_year"], errors="coerce").to_numpy()
    for ui in range(len(registry)):
        if np.isfinite(comm[ui]):
            avail[ui, yrs < comm[ui]] = 0.0
        if np.isfinite(clos[ui]):
            avail[ui, yrs >= clos[ui]] = 0.0

    planned = schedule_planned(config, model, registry, draw=draw)
    forced = simulate_forced(config, model, registry, draw=draw, planned=planned,
                             user_corrections=_user_corrections(config))
    common = simulate_common_mode(config, model, registry, draw=draw)

    # 1) derating (multiplicative) on hot days — apply before offline zeros
    der = thermal_derating(config, model, registry, temp_daily)
    for r in der.itertuples(index=False):
        ui = urow.get(r.unit_id)
        di = (pd.Timestamp(r.day).normalize() - hstart).days
        if ui is not None and 0 <= di < n_days and avail[ui, di] > 0:
            avail[ui, di] *= r.avail_frac
            state[ui, di] = 4
    # 2) offline unions (order sets state precedence: forced < planned < common)
    _set_ranges(avail, urow, forced, hstart, n_days, 0.0, state_mat=state, state_code=2)
    _set_ranges(avail, urow, planned, hstart, n_days, 0.0, state_mat=state, state_code=1)
    _set_ranges(avail, urow, common, hstart, n_days, 0.0, state_mat=state, state_code=3)
    return avail, state, urow


def project(config: Config, n_draws: int | None = None):
    model = CalibratedAvailability.load(config.models_dir / "calibrated_availability.json")
    registry = load_scenario_registry(config)
    from ..io.weather import load_national_weather
    temp_daily, wetness = load_national_weather(config)
    hstart, days, n_days = _horizon(config)
    nd = int(n_draws or config.section("projection")["n_draws"])
    outdir = config.output_dir
    outdir.mkdir(parents=True, exist_ok=True)

    tech = registry["technology"].to_numpy()
    techs = sorted(set(tech))
    nuc_mask = registry["technology"].to_numpy() == "nuclear"

    tech_frames, nuc_frames, ic_frames = [], [], []
    for d in range(nd):
        avail, state, urow = _assemble_draw(config, model, registry, d, temp_daily, hstart, days, n_days)
        # aggregate available capacity by technology (daily)
        for t in techs:
            m = tech == t
            tech_frames.append(pd.DataFrame({"draw": d, "day": days, "technology": t,
                                             "available_mw": avail[m].sum(axis=0).round(1)}))
        # per-nuclear-unit daily detail (state + available)
        ni = np.where(nuc_mask)[0]
        for ui in ni:
            nuc_frames.append(pd.DataFrame({
                "draw": d, "day": days, "unit_id": registry["unit_id"].iloc[ui],
                "available_mw": avail[ui].round(1),
                "state": pd.Categorical.from_codes(state[ui], categories=list(_STATE.values()))}))
        ic = interconnector_availability(config, _interconnectors(config), draw=d)
        ic["draw"] = d
        ic_frames.append(ic)

    by_tech = pd.concat(tech_frames, ignore_index=True)
    nuc = pd.concat(nuc_frames, ignore_index=True)
    lake.write_table(by_tech, "availability", "availability_by_tech", index=False)
    lake.write_table(nuc, "availability", "availability_nuclear_units", index=False)
    lake.write_table(pd.concat(ic_frames, ignore_index=True), "availability", "interconnectors", index=False)
    lake.write_table(reservoir_energy_budget(config, model, wetness), "availability", "reservoir_budget", index=False)

    meta = run_metadata(config, weather_draw="shared_cube")
    meta["n_draws"] = nd
    (outdir / "run_metadata.json").write_text(json.dumps(meta, indent=2))

    _print_summary(by_tech, registry, nd, outdir)
    return by_tech


def _interconnectors(config: Config) -> pd.DataFrame:
    from ..io.assumptions import load_assumptions
    wb = config.resolve(config.section("assumptions")["workbook"])
    return load_assumptions(wb)["interconnectors"]


def _print_summary(by_tech, registry, nd, outdir):
    nuc = by_tech[by_tech["technology"] == "nuclear"]
    nuc_cap = registry.loc[(registry["technology"] == "nuclear")
                           & registry["closure_year"].isna(), "capacity_mw"].sum()
    kd_by_draw = nuc.groupby("draw")["available_mw"].mean() / nuc_cap
    print(f"[project] {nd} draws -> {outdir}")
    print(f"[project] nuclear Kd across draws: mean {kd_by_draw.mean():.3f} "
          f"[{kd_by_draw.min():.3f}, {kd_by_draw.max():.3f}] (low = common-mode draws)")
