"""Storage in the dispatch LP (PSP+BESS) — toy assertions: arbitrage compresses the spread, charging
concentrates in the trough, window-end SoC neutrality holds, and storage absorbs at negative prices."""
from __future__ import annotations

import numpy as np
import pandas as pd

from dispatch_model.lp.highs_solver import solve_multizone_highs

_STACK = pd.DataFrame({"unit_id": ["BASE", "PEAK"], "tech": ["nuclear", "gas"],
                       "capacity_mw": [5000.0, 5000.0], "srmc_eur_mwh": [10.0, 100.0],
                       "min_gen_frac": [0.0, 0.0]})
_STORE = {"N": {"p_dis": np.array([1000.0]), "p_ch": np.array([1000.0]), "e_max": np.array([4000.0]),
                "eta_ch": np.array([0.95]), "eta_dis": np.array([0.95]), "vom": 0.5}}


def _run(demand, res_pot=None, storage=_STORE):
    T = pd.date_range("2024-05-19", periods=len(demand), freq="h", tz="UTC")
    zd = {"N": {"stack": _STACK, "demand": list(map(float, demand)),
                "res_pot": res_pot or [0.0] * len(demand), "avail": None, "energy_caps": None}}
    return solve_multizone_highs(T, zd, [], {}, storage=storage)


def test_storage_arbitrages_trough_to_peak_and_stays_neutral():
    dem = [3000.0] * 6 + [7000.0] * 6                       # cheap night, expensive day
    base = _run(dem, storage=None)
    st = _run(dem)
    s = st["storage"]["N"]
    assert s["ch"][0][:6].sum() > 1000                      # charges in the trough
    assert s["dis"][0][6:].sum() > 1000                     # discharges at the peak
    assert abs(s["soc"][0][-1] - 2000.0) < 1.0              # window-end neutrality (0.5·e_max)
    peak_base = base["prices"]["N"].iloc[6:].mean()
    peak_st = st["prices"]["N"].iloc[6:].mean()
    assert peak_st <= peak_base + 1e-6                      # arbitrage cannot raise the peak


def test_storage_absorbs_negative_price_surplus():
    # must-take RES beyond demand with a −30 floor: storage charges instead of curtailing at −30
    dem = [2000.0] * 4 + [6000.0] * 8
    out = _run(dem, res_pot=[4000.0] * 4 + [0.0] * 8)
    s = out["storage"]["N"]
    assert s["ch"][0][:4].sum() > 2000                      # soaks the surplus hours
