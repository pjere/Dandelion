"""Control-area installed capacity allocated to bidding zones — the Italian nameplate gap.

`entsoe_installed_capacity` holds **no rows at all for any Italian bidding zone**. That is not a missing
ingestion: every IT zone was requested and ENTSO-E returned `nodata` (7 zones logged), because Italy
publishes installed capacity at CONTROL-AREA level only while its market is split into seven bidding
zones. Verified directly against the API — `IT` returns 18 technologies and 97.5 GW for 2024, while
`IT_NORD` and `IT_CNOR` both raise `NoMatchingDataError`.

So every Italian technology fell through `build_neighbour_stack`'s p99.9-of-generation fallback, and
`METHODOLOGY.md` records exactly what that costs: a generation quantile UNDER-reads plant that rarely runs
at rated power. It is worst for ENERGY-LIMITED plant, which by construction almost never runs flat out:

    IT_NORTH 2024      p99.9 proxy    allocated    proxy / allocated
    hydro_reservoir       1.54 GW      4.18 GW          0.37
    hydro_psp             2.52         5.18            0.49
    gas                  18.05        22.65            0.80
    coal                  0.00         0.05            ~1     <- the proxy was RIGHT

THE ALLOCATION KEY IS EACH ZONE'S SHARE OF OBSERVED GENERATION FOR THAT TECHNOLOGY. The country total is
then externally anchored and only the SPLIT is inferred, which is strictly better than the status quo where
both the level and the split come from the same quantile.

IT IS VALIDATED GEOGRAPHICALLY, not by whether it improves the gate. The 2024 shares put:

    hard coal    63 % Sardinia, 30 % IT_CNOR, 1 % NORTH   — Fiume Santo and Torrevaldaliga Nord; the
                                                            northern plants (Monfalcone, Fusina, La Spezia)
                                                            had effectively stopped by 2024
    geothermal  100 % IT_CNOR                             — Larderello, Tuscany
    reservoir    92 % NORTH,  run-of-river 88 % NORTH     — the Alpine fleet
    oil          50 % IT_CNOR, 41 % Sicily

Every one of those is where the plant physically is, which is the check that the key is not merely
reproducing the proxy it replaces. It also CORRECTED a hypothesis: IT-North's 0.00 GW of modelled coal
looked like a defect and is not one — Italian coal is southern and insular, and the north really does have
almost none.

A ZONE WITH NO GENERATION OF A TECHNOLOGY GETS NONE OF IT. That is deliberate and it is the key's main
weakness: capacity that exists but never ran in the year — mothballed, or held in a reserve — is allocated
to zero. The alternative (splitting by installed capacity) is precisely the number that does not exist.

Scoped to Italy by `_CONTROL_AREA`. GB is NOT a candidate even though it is also empty: ENTSO-E returns
`NoMatchingDataError` for GB at every level, and Britain's data legitimately comes from Elexon. CH returns
only four technologies (hydro x3 + nuclear, 16.1 GW), which is the same root cause as the Swiss
run-of-river gap in `io.ch_hydro` and is not fixable by allocation either — the API simply does not carry
Swiss solar or wind.

Opt in with `DISPATCH_AREA_CAPACITY=1`.
"""
from __future__ import annotations

import os

import pandas as pd

from ..config import Config
from ..framecache import FrameCache, db_key

#: bidding zone -> the control area whose `entsoe_installed_capacity` rows cover it. Italy only; see the
#: module docstring for why GB and CH are excluded rather than overlooked.
_CONTROL_AREA = {z: "IT" for z in ("IT_NORTH", "IT_CNOR", "IT_CSUD", "IT_SUD",
                                   "IT_CALA", "IT_SICI", "IT_SARD")}

_CACHE = FrameCache(maxsize=16)


def enabled() -> bool:
    """Read at each call site so an A/B arm can flip it without reimporting."""
    return os.environ.get("DISPATCH_AREA_CAPACITY", "0") not in ("0", "false", "False")


def allocated_capacity(config: Config, zone: str, year: int) -> dict[str, float]:
    """→ {tech: MW} for a bidding zone, from its control area's nameplate split by generation share.

    `{}` when the zone has no control area mapped, when the flag is off, or when either the capacity or
    the generation side is missing — never a default, so a zone without evidence keeps the existing
    p99.9 fallback.
    """
    area = _CONTROL_AREA.get(zone)
    if area is None or not enabled():
        return {}

    def build() -> pd.DataFrame | None:
        import sqlite3
        from .entsoe_hist import PSR2TECH
        path = config.resolve(config.section("data")["sqlite_path"])
        con = sqlite3.connect(path)
        try:
            cap = pd.read_sql(
                "SELECT sub_key, MAX(value) mw FROM entsoe_installed_capacity "
                "WHERE series_key = ? AND ts_utc >= ? AND ts_utc < ? GROUP BY sub_key", con,
                params=(area, f"{year}-01-01", f"{year + 1}-01-01"))
            peers = [z for z, a in _CONTROL_AREA.items() if a == area]
            q = ",".join("?" * len(peers))
            gen = pd.read_sql(
                f"SELECT series_key zone, sub_key, AVG(value) mw FROM entsoe_generation "
                f"WHERE series_key IN ({q}) AND ts_utc >= ? AND ts_utc < ? "
                "GROUP BY series_key, sub_key", con,
                params=(*peers, f"{year}-01-01", f"{year + 1}-01-01"))
        except Exception:                              # noqa: BLE001 — no feed → no allocation
            return None
        finally:
            con.close()
        if cap.empty or gen.empty:
            return None
        rows = []
        for sk, mw in cap.set_index("sub_key")["mw"].items():
            tech = PSR2TECH.get(str(sk))
            if tech is None or not mw or mw <= 0:
                continue
            g = gen[gen["sub_key"] == sk]
            tot = float(g["mw"].sum())
            if tot <= 0:
                continue                               # nobody generated it → no basis to split it
            share = float(g.loc[g["zone"] == zone, "mw"].sum()) / tot
            if share > 0:
                rows.append({"tech": tech, "mw": float(mw) * share, "share": share})
        return pd.DataFrame(rows) if rows else None

    df = _CACHE.get_or_build((db_key(config), "areacap", str(zone), int(year)), build)
    if df is None or df.empty:
        return {}
    return {str(r.tech): float(r.mw) for r in df.itertuples()}
