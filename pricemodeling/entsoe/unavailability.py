"""ENTSO-E REMIT generation-unit unavailability → `entsoe_unavailability` + per-unit availability (step v).

Supersedes availability_model decision D1 (infer outages from per-unit production). REMIT is the market's
own outage disclosure: `query_unavailability_of_generation_units(zone, …)` returns one record per outage
interval with the unit EIC, nominal power, the **available** capacity during the outage (`avail_qty` — so
partial deratings are captured, not just full trips), the **businesstype** (planned maintenance A53 vs forced
/ unplanned A54 → a direct planned/forced split, no duration-band heuristic), the interval, and a docstatus
(drop Withdrawn/Cancelled A09). Coverage is good from ~2015.

Two products:
  1. `ingest_unavailability` — REMIT records → the `entsoe_unavailability` table (yearly-chunked, idempotent
     via `ingest_log`, same machinery as `series.py`).
  2. `reconstruct_daily_availability` — collapse the overlapping messages into a per-unit **daily available
     MW** series (the ground truth), from which step v recalibrates Kd / planned-forced / common-mode and
     step vi can read true historical FR unit availability. The production-inference path stays as a fallback
     where REMIT is sparse.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import text

from ..db import already_ingested, log_ingest, upsert_df
from .series import ZONES, _fetch_retry, _year_chunks

T_UNAVAIL = "entsoe_unavailability"

# ENTSO-E businessType → our label. entsoe-py 0.8 returns the *human-readable* label ("Planned maintenance"
# / "Unplanned outage"), older/other paths return the raw code (A53 planned / A54 unplanned) — handle both.
# NB: "UNPLANNED" contains "PLANNED" as a substring, so forced markers MUST be tested first.
def _outage_type(businesstype: str) -> str:
    bt = businesstype.upper()
    if "A54" in bt or "UNPLANNED" in bt:
        return "forced"
    if "A53" in bt or "PLANNED" in bt:
        return "planned"
    return "forced"                              # unclassified → conservatively forced


# docStatus Withdrawn/Cancelled (A09 / the text label) → not a real outage; anything else is kept
_CANCELLED_MARKERS = ("A09", "WITHDRAWN", "CANCEL")


def ensure_unavailability_table(engine) -> None:
    ddl = f"""
    CREATE TABLE IF NOT EXISTS "{T_UNAVAIL}" (
        mrid           TEXT NOT NULL,
        start_utc      TEXT NOT NULL,
        end_utc        TEXT,
        zone           TEXT,
        eic            TEXT,
        unit_name      TEXT,
        plant_type     TEXT,
        businesstype   TEXT,
        outage_type    TEXT,
        nominal_mw     REAL,
        avail_mw       REAL,
        unavailable_mw REAL,
        docstatus      TEXT,
        created_utc    TEXT,
        PRIMARY KEY (mrid, start_utc)
    )
    """
    with engine.begin() as conn:
        conn.execute(text(ddl))


def _pick(row, *names, default=None):
    """First present, non-null column among `names` (entsoe-py column names drift across versions)."""
    for n in names:
        if n in row and pd.notna(row[n]):
            return row[n]
    return default


def _parse_unavailability(df: pd.DataFrame, zone: str) -> pd.DataFrame:
    """entsoe-py unavailability frame → our row schema. Defensive about column names/business codes."""
    if df is None or len(df) == 0:
        return pd.DataFrame()
    rows = []
    for i, (_, r) in enumerate(df.iterrows()):
        btype = str(_pick(r, "businesstype", "business_type", default="") or "")
        docstatus = str(_pick(r, "docstatus", "doc_status", default="") or "")
        if any(m in docstatus.upper() for m in _CANCELLED_MARKERS):
            continue
        nominal = _pick(r, "nominal_power", "nominal_mw", "installed_capacity")
        avail = _pick(r, "avail_qty", "available_capacity", "quantity", "qty", default=0.0)
        start = _pick(r, "start", "start_utc")
        end = _pick(r, "end", "end_utc")
        mrid = _pick(r, "mrid", "mRID", "docmrid", default=f"{zone}_{i}")
        nominal_f = pd.to_numeric(nominal, errors="coerce")
        avail_f = pd.to_numeric(avail, errors="coerce")
        rows.append({
            "mrid": str(mrid),
            "start_utc": _iso(start), "end_utc": _iso(end),
            "zone": zone,
            "eic": str(_pick(r, "production_resource_id", "production_resource_mrid", "eic", default="")),
            "unit_name": str(_pick(r, "production_resource_name", "unit_name", default="")),
            "plant_type": str(_pick(r, "plant_type", "production_resource_psr_type", "psr_type", default="")),
            "businesstype": btype, "outage_type": _outage_type(btype),
            "nominal_mw": float(nominal_f) if pd.notna(nominal_f) else None,
            "avail_mw": float(avail_f) if pd.notna(avail_f) else None,
            "unavailable_mw": (float(nominal_f - avail_f)
                               if pd.notna(nominal_f) and pd.notna(avail_f) else None),
            "docstatus": docstatus,
            "created_utc": _iso(_pick(r, "created_doc_time", "created", "createdDateTime")),
        })
    return pd.DataFrame(rows).dropna(subset=["unavailable_mw"])


def _iso(ts) -> str | None:
    if ts is None or (isinstance(ts, float) and pd.isna(ts)):
        return None
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")
    return t.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def ingest_unavailability(engine, client, start: date, end: date, zones=None, force=False) -> int:
    """REMIT unavailability per zone × year chunk → entsoe_unavailability (idempotent)."""
    ensure_unavailability_table(engine)
    total = 0
    for z, area in (zones or ZONES).items():
        for c0, c1 in _year_chunks(start, end):
            def build(df, z=z):
                return [_parse_unavailability(df, z)]
            total += _do_unavail(engine, lambda area=area, c0=c0, c1=c1: client
                                 .query_unavailability_of_generation_units(area, start=c0, end=c1),
                                 f"entsoe:unavail:{z}", f"{z}_{c0.date()}", force, build)
    return total


def _do_unavail(engine, client_call, source, key, force, build) -> int:
    """Fetch (unless cached) → parse → upsert; no-data and errors logged and skipped (mirrors series._do)."""
    if not force and already_ingested(engine, source, key):
        return 0
    try:
        raw = _fetch_retry(client_call)
    except Exception as exc:  # noqa: BLE001
        low = str(exc).lower()
        status = "nodata" if ("no matching data" in low or "nodata" in low
                              or "NoMatchingData" in type(exc).__name__) else "error"
        log_ingest(engine, source, key, 0, status=status)
        if status == "error":
            print(f"    ! {source} {key}: {type(exc).__name__} {str(exc)[:80]}", flush=True)
        return 0
    total = sum(upsert_df(engine, T_UNAVAIL, df, ["mrid", "start_utc"]) for df in build(raw))
    log_ingest(engine, source, key, total)
    return total


def reconstruct_daily_availability(engine, zone: str, year: int) -> pd.DataFrame:
    """Per-unit **daily available MW** for `zone`/`year` from the stored outage messages.

    For each unit-day the binding outage is the **most restrictive** active message covering that day (using
    the max unavailable MW avoids double-counting overlapping/superseding REMIT revisions). Returns
    [date, eic, unit_name, nominal_mw, unavailable_mw, available_mw, outage_type] — one row per unit-day that
    has an outage (units with no outage are implicitly fully available).

    Vectorized (Phase-2 review, AR-10): each message interval [s, e) is clipped to the year and exploded
    into its covered days, then the binding message per (eic, day) is the stable-sorted max — verified
    row-identical to the original per-day loop on FR/DE_LU/BE/ES real data, ~80× faster."""
    q = text(f'SELECT eic, unit_name, nominal_mw, unavailable_mw, outage_type, start_utc, end_utc '
             f'FROM "{T_UNAVAIL}" WHERE zone=:z AND unavailable_mw > 0')
    with engine.connect() as conn:
        msgs = pd.read_sql(q, conn, params={"z": zone})
    cols = ["date", "eic", "unit_name", "nominal_mw", "unavailable_mw", "available_mw", "outage_type"]
    if msgs.empty:
        return pd.DataFrame(columns=cols)
    y0 = pd.Timestamp(f"{year}-01-01", tz="UTC")
    y1 = pd.Timestamp(f"{year + 1}-01-01", tz="UTC")
    s = pd.to_datetime(msgs["start_utc"], utc=True)
    e = pd.to_datetime(msgs["end_utc"], utc=True).fillna(y1)
    # per-eic constants: nominal = max over ALL of the unit's zone messages; name = its first message's
    nominal = pd.to_numeric(msgs["nominal_mw"], errors="coerce").groupby(msgs["eic"]).max()
    name = msgs.groupby("eic")["unit_name"].first()
    # interval [s, e) overlaps day [d, d+1day) ⇔ floor(s) ≤ d < ceil(e): a midnight start doesn't spill
    # onto the previous day, a midnight end doesn't spill onto the next. Clip to the year, explode to days.
    d0 = s.dt.floor("D").clip(lower=y0)
    d1 = e.dt.ceil("D").clip(upper=y1)
    n = (d1 - d0).dt.days.clip(lower=0).to_numpy()
    keep = n > 0
    rep = msgs.loc[keep, ["eic", "unavailable_mw", "outage_type"]].reset_index(drop=True)
    rep = rep.loc[rep.index.repeat(n[keep])]
    off = np.concatenate([np.arange(k) for k in n[keep]]) if keep.any() else np.array([], int)
    rep["date"] = (d0[keep].reset_index(drop=True).loc[rep.index]
                   + pd.to_timedelta(off, unit="D")).dt.date.to_numpy()
    # binding = max unavailable_mw per (eic, day); the stable sort keeps the first-occurring max (== idxmax)
    rep = rep.reset_index(drop=True).sort_values("unavailable_mw", ascending=False, kind="stable")
    binding = (rep.groupby(["eic", "date"], sort=True).first().reset_index()
                  .sort_values(["eic", "date"], kind="stable").reset_index(drop=True))
    nom = binding["eic"].map(nominal)
    out = pd.DataFrame({"date": binding["date"], "eic": binding["eic"],
                        "unit_name": binding["eic"].map(name),
                        "nominal_mw": [float(v) if pd.notna(v) else None for v in nom],
                        "unavailable_mw": binding["unavailable_mw"].astype(float),
                        "available_mw": [float(nv) - float(uv) if pd.notna(nv) else None
                                         for nv, uv in zip(nom, binding["unavailable_mw"])],
                        "outage_type": binding["outage_type"]})
    return out[cols]


def outage_rate_summary(engine, zone: str, year: int, plant_type: str | None = None) -> dict:
    """Ground-truth calibration targets for step v: fleet outage rate (Kd) and the planned/forced split,
    straight from REMIT — the numbers that validate or replace the ~0.74 inferred Kd and the duration bands.
    Pass `plant_type` (e.g. "Nuclear") to restrict to one technology for a like-for-like comparison.

    The **planned/forced split** is computed at the message level (energy = Σ unavailable_MW·hours by type),
    NOT from the daily binding reconstruction: a unit in a partial forced derate *during* a full planned
    outage would otherwise have its forced energy masked by the more-restrictive planned message — which
    would badly understate forced outages precisely in a crisis year like 2022 (stress-corrosion)."""
    daily = reconstruct_daily_availability(engine, zone, year)
    pt_clause = " AND plant_type = :pt" if plant_type else ""
    q = text(f'SELECT outage_type, SUM(unavailable_mw * 24 * '
             f'(julianday(MIN(COALESCE(end_utc,:y1), :y1)) - julianday(MAX(start_utc, :y0)))) mwh '
             f'FROM "{T_UNAVAIL}" WHERE zone=:z AND unavailable_mw > 0{pt_clause} '
             f'AND start_utc < :y1 AND COALESCE(end_utc, :y1) > :y0 GROUP BY outage_type')
    params = {"z": zone, "y0": f"{year}-01-01T00:00:00+00:00", "y1": f"{year+1}-01-01T00:00:00+00:00"}
    if plant_type:
        params["pt"] = plant_type
    with engine.connect() as conn:
        split = pd.read_sql(q, conn, params=params)
    tot = float(split["mwh"].sum())
    planned = float(split.loc[split["outage_type"] == "planned", "mwh"].sum())
    forced = float(split.loc[split["outage_type"] == "forced", "mwh"].sum())
    return {"zone": zone, "year": year, "unit_days": int(len(daily)),
            "unavailable_gwd": round(daily["unavailable_mw"].sum() / 1000, 1) if not daily.empty else 0.0,
            "planned_share": round(planned / tot, 3) if tot else None,
            "forced_share": round(forced / tot, 3) if tot else None,
            "units_with_outage": int(daily["eic"].nunique()) if not daily.empty else 0}


def _tech_eics(engine, zone: str, plant_type: str) -> tuple[set, float]:
    q = text(f'SELECT eic, MAX(nominal_mw) nom FROM "{T_UNAVAIL}" WHERE zone=:z AND plant_type=:pt GROUP BY eic')
    with engine.connect() as conn:
        t = pd.read_sql(q, conn, params={"z": zone, "pt": plant_type})
    return set(t["eic"]), float(t["nom"].sum())


def tech_unavailable_mw(engine, zone: str, year: int, plant_type: str) -> pd.Series:
    """Daily **absolute** unavailable MW for one `plant_type` in `zone`/`year` from the REMIT binding
    reconstruction. Divide by the fleet's *installed* capacity (not the REMIT fleet) for an availability."""
    daily = reconstruct_daily_availability(engine, zone, year)
    if daily.empty:
        return pd.Series(dtype=float)
    eics, _ = _tech_eics(engine, zone, plant_type)
    d = daily[daily["eic"].isin(eics)]
    days = pd.date_range(f"{year}-01-01", f"{year}-12-31", freq="D").date
    return d.groupby("date")["unavailable_mw"].sum().reindex(days).fillna(0.0)


def nuclear_unavailable_mw(engine, zone: str, year: int, plant_type: str = "Nuclear") -> pd.Series:
    """Daily absolute nuclear unavailable MW (thin wrapper over `tech_unavailable_mw` for the FR step-vi feed)."""
    return tech_unavailable_mw(engine, zone, year, plant_type)


def installed_by_tech(engine, zone: str) -> dict[str, float]:
    """Installed MW per plant_type for `zone` from entsoe_installed_capacity (latest value per PSR). The
    sub_key labels match REMIT plant_type ("Nuclear", "Fossil Gas", …), so they align for an availability."""
    q = text("SELECT sub_key, value FROM entsoe_installed_capacity WHERE series_key=:z AND label='installed_mw'")
    with engine.connect() as conn:
        df = pd.read_sql(q, conn, params={"z": zone})
    return {str(k): float(v) for k, v in df.groupby("sub_key")["value"].last().items() if v and v > 0}


def _eic_plant_type(engine, zone: str) -> dict[str, str]:
    """eic → plant_type. ENTSO-E labels drift for ~17 units ("Other" vs "Fossil Gas", PSP vs reservoir for
    mixed hydro), so the map takes the unit's **most frequent** label (ties alphabetical) — deterministic,
    where the previous un-aggregated GROUP BY let SQLite pick an arbitrary row (Phase-2 review, AR-12)."""
    q = text(f'SELECT eic, plant_type, COUNT(*) AS n FROM "{T_UNAVAIL}" WHERE zone=:z '
             f'GROUP BY eic, plant_type')
    with engine.connect() as conn:
        df = pd.read_sql(q, conn, params={"z": zone})
    df = df.sort_values(["eic", "n", "plant_type"], ascending=[True, False, True], kind="stable")
    top = df.groupby("eic", sort=False).first()
    return dict(zip(top.index, top["plant_type"]))


def zone_availability_stats(engine, zone: str, years: list[int],
                            min_installed_mw: float = 500.0) -> dict:
    """Per-plant_type availability distribution for `zone` from REMIT over `years`: for each tech the mean
    and across-year std of the annual availability = 1 − mean_daily_unavailable_MW / installed_MW. These are
    the calibration targets + the stochastic spread for a projection draw. Techs below `min_installed_mw`
    are skipped; a tech with installed capacity but no REMIT outages carries availability 1.0 with zero
    spread (⇒ a no-op multiplier downstream).

    Reconstructs the daily availability **once per year** and splits by tech via the eic→plant_type map
    (reconstructing per tech would repeat the O(units×days) collapse for every technology)."""
    installed = installed_by_tech(engine, zone)
    eic2pt = _eic_plant_type(engine, zone)
    annual: dict[str, list] = {t: [] for t, c in installed.items() if c >= min_installed_mw}
    for y in years:
        daily = reconstruct_daily_availability(engine, zone, y)
        if daily.empty:
            continue
        daily = daily.assign(plant_type=daily["eic"].map(eic2pt))
        by_tech = daily.groupby("plant_type")["unavailable_mw"].sum() / len(
            pd.date_range(f"{y}-01-01", f"{y}-12-31", freq="D"))         # mean daily unavailable MW per tech
        for tech in annual:
            un = float(by_tech.get(tech, 0.0))
            annual[tech].append(max(0.0, 1.0 - un / installed[tech]))
    out = {}
    for tech, v in annual.items():
        if not v:
            continue
        mean_avail = float(np.mean(v))
        # a real operating fleet does not sit structurally below ~40 % available — a very low mean means the
        # tech is a *retiring* fleet where REMIT permanent-closure messages / a shrinking installed base make
        # the "availability" meaningless (FR hard coal 2019-24 is the canonical case). Drop it.
        if mean_avail < 0.4:
            continue
        out[tech] = {"installed_mw": round(installed[tech], 0), "mean_avail": round(mean_avail, 3),
                     "std_avail": round(float(np.std(v)), 3), "n_years": len(v)}
    return out


def nuclear_available_fraction(engine, zone: str, year: int, plant_type: str = "Nuclear") -> pd.Series:
    """Daily available fraction (0–1) of the `zone` **nuclear** fleet from REMIT, using the REMIT fleet
    nominal (units that had ≥1 outage) as the denominator — a standalone diagnostic. The dispatch should
    instead use ``nuclear_unavailable_mw`` against its own installed capacity."""
    _, fleet_mw = _tech_eics(engine, zone, plant_type)
    un = nuclear_unavailable_mw(engine, zone, year, plant_type)
    if fleet_mw <= 0 or un.empty:
        return pd.Series(dtype=float)
    return (1.0 - un / fleet_mw).clip(0.0, 1.0)
