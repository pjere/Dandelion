"""Phase 2 — weather-derating parameters for river/estuary-cooled thermal units (§6.4).

River-cooled reactors lose output in hot, low-flow summers (river-temperature regulatory limits). The
effect is real but small for France (a few TWh only in extreme years, e.g. 2003 / 2018 / 2022) and is
badly confounded with summer *economic* modulation (low demand → the same reactors throttle down for
price reasons). Fitting a slope off summer production would mostly capture that confound, so we use
physically-grounded per-basin defaults here (river/estuary sensitive; sea/tower not) and leave a
data-driven refit — which would need a river-temperature series, not just air temperature — as a hook.

The important structural property for the price model is preserved regardless of the exact slope: the
derating is driven by the SAME weather draws as demand (iii) and RES (iv), so heat-wave demand spikes
coincide with thermal-availability cuts.
"""
from __future__ import annotations

import pandas as pd

# cooling classes that are exposed to summer thermal derating
_SENSITIVE = {"river", "estuary"}
_DEFAULT = {"air_temp_threshold_c": 25.0, "derate_frac_per_c": 0.03, "water_lag_weeks": 1.5,
            "regulatory_limit_on": 1.0}


def calibrate_derating(registry: pd.DataFrame, config) -> dict:
    nuc = registry[registry["technology"] == "nuclear"]
    out: dict[str, dict] = {}
    for basin, g in nuc.dropna(subset=["basin"]).groupby("basin"):
        sensitive = bool(g["cooling"].isin(_SENSITIVE).any())
        out[str(basin)] = {**_DEFAULT,
                           "derate_frac_per_c": _DEFAULT["derate_frac_per_c"] if sensitive else 0.0,
                           "sensitive": sensitive, "n_units": int(len(g)),
                           "source": "literature_default"}
    return out
