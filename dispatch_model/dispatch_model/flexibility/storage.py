"""Storage in the dispatch LP (PSP + BESS) — the v2 increment the CH/ES level biases named.

PSP was excluded from dispatch since v1 (`_EXCLUDE_DISPATCH`); the CH/NL/ES decomposition measured the
cost: CH priced off its upper hydro tranches with imports saturated while 6.7 GW of PSP sat idle, ES's
peaks lacked their 3.4 GW smoother. BESS is the same LP machinery with different parameters (shorter
duration, higher round-trip). Pure LP: SoC dynamics are linear; weekly energy neutrality via pinned
window-end SoC (see `highs_solver._build`). Flex-gated: flag-off keeps the historic exclusion.

Parameters: PSP power = the zone's measured `hydro_psp` stack capacity; duration 8 h (CH 12 h — larger
alpine basins), round-trip η 0.76. BESS 2024 grid-scale seeds below (GW / GWh, conservative public
figures), round-trip η 0.90. All workbook-overridable later (`dispatch_storage` tab, backlog)."""
from __future__ import annotations

import numpy as np

#: zone → (power_GW, energy_GWh) grid-scale battery seeds, 2024
_BESS_2024 = {"DE_LU": (1.6, 2.5), "NL": (0.3, 0.6), "BE": (0.2, 0.4), "FR": (0.6, 0.9),
              "ES": (0.2, 0.4), "CH": (0.1, 0.2), "IT_NORTH": (0.5, 1.0)}
#: zone → (effective_power_GW, effective_daily_energy_GWh): MEASURED PSP envelopes, 2024
#: (`scratchpad/psp_envelopes.py`) — p95 hourly generation (real fleets never run nameplate
#: simultaneously: derates 0.47–0.86×) and p95 daily generated energy (the reservoir actually cycled,
#: ~0.6× the theoretical duration). Replaces the nameplate parameters whose frictionless arbitrage
#: annihilated the negative tail (probe I). Zones without measurement fall back to nameplate × 0.6.
_PSP_MEASURED = {"DE_LU": (4.6, 41.6), "CH": (2.8, 46.7), "ES": (2.7, 34.8),
                 "FR": (3.3, 34.6), "BE": (0.75, 4.7)}
#: pumping-friction cost (€/MWh on CHARGE): calibration seed that stops the LP pumping at every
#: micro-spread — effective round-trip threshold ≈ vom_ch/η² ≈ 8.7 €/MWh; passed the storage re-gate.
#: (An earlier discharge-side rationale — "32–54 % top-quartile discharge ⇒ half balancing duty" — was
#: refuted as a fleet-composition artifact: FLEX_CALIBRATION_2024.md §"PSP discharge friction: REFUTED".)
_PUMP_VOM = 5.0
_ETA_PSP, _ETA_BESS = 0.76 ** 0.5, 0.90 ** 0.5
#: BESS build-out factor vs the 2024 seed (starter trajectory, TYNDP-order; workbook-overridable later).
#: In PROJECTION the real storage machinery replaces the battery share of #83's crude `cap_flex_gw`
#: block (the caller shrinks that block by the BESS power added — no double count).
_BESS_GROWTH = [(2024, 1.0), (2030, 4.0), (2040, 10.0), (2050, 12.0)]


def bess_factor(year: int) -> float:
    ys, fs = zip(*_BESS_GROWTH)
    return float(np.interp(year, ys, fs))


def bess_power_mw(zone: str, year: int) -> float:
    """BESS power (MW) the storage spec carries for `zone`/`year` — the amount the caller must SHRINK the
    #83 `cap_flex_gw` adequacy block by, since that block already counts batteries (no double count)."""
    b = _BESS_2024.get(zone)
    return b[0] * 1e3 * bess_factor(year) if b else 0.0


def storage_spec(psp_mw: dict[str, float], year: int) -> dict:
    """→ the `storage` dict for `solve_multizone(..., storage=)`: {zone: {p_dis, p_ch, e_max, eta_ch,
    eta_dis, vom}} with one PSP unit (where the zone has measured PSP capacity) and one BESS unit
    (where the seed table has an entry). `psp_mw` = measured `hydro_psp` capacity per zone."""
    out: dict = {}
    for z in set(psp_mw) | set(_BESS_2024):
        p_dis, p_ch, e_max, ech, edis, vch = [], [], [], [], [], []
        psp = float(psp_mw.get(z, 0.0))
        meas = _PSP_MEASURED.get(z)
        if meas:
            p, e = meas[0] * 1e3, meas[1] * 1e3
        else:
            p, e = 0.6 * psp, 0.6 * 8.0 * psp              # unmeasured zone: conservative 0.6× fallback
        if p > 100:
            p_dis.append(p); p_ch.append(p); e_max.append(e)
            ech.append(_ETA_PSP); edis.append(_ETA_PSP); vch.append(_PUMP_VOM)
        b = _BESS_2024.get(z)
        if b:
            f = bess_factor(year)
            p_dis.append(b[0] * 1e3 * f); p_ch.append(b[0] * 1e3 * f); e_max.append(b[1] * 1e3 * f)
            ech.append(_ETA_BESS); edis.append(_ETA_BESS); vch.append(1.0)
        if p_dis:
            out[z] = {"p_dis": np.array(p_dis), "p_ch": np.array(p_ch), "e_max": np.array(e_max),
                      "eta_ch": np.array(ech), "eta_dis": np.array(edis), "vom": 0.5,
                      "vom_ch": np.array(vch)}
    return out
