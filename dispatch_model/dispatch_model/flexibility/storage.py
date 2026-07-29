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
_PSP_HOURS = {"CH": 12.0}
_PSP_H_DEFAULT = 8.0
_ETA_PSP, _ETA_BESS = 0.76 ** 0.5, 0.90 ** 0.5


def storage_spec(psp_mw: dict[str, float], year: int) -> dict:
    """→ the `storage` dict for `solve_multizone(..., storage=)`: {zone: {p_dis, p_ch, e_max, eta_ch,
    eta_dis, vom}} with one PSP unit (where the zone has measured PSP capacity) and one BESS unit
    (where the seed table has an entry). `psp_mw` = measured `hydro_psp` capacity per zone."""
    out: dict = {}
    for z in set(psp_mw) | set(_BESS_2024):
        p_dis, p_ch, e_max, ech, edis = [], [], [], [], []
        psp = float(psp_mw.get(z, 0.0))
        if psp > 100:
            h = _PSP_HOURS.get(z, _PSP_H_DEFAULT)
            p_dis.append(psp); p_ch.append(0.9 * psp); e_max.append(h * psp)
            ech.append(_ETA_PSP); edis.append(_ETA_PSP)
        b = _BESS_2024.get(z)
        if b:
            p_dis.append(b[0] * 1e3); p_ch.append(b[0] * 1e3); e_max.append(b[1] * 1e3)
            ech.append(_ETA_BESS); edis.append(_ETA_BESS)
        if p_dis:
            out[z] = {"p_dis": np.array(p_dis), "p_ch": np.array(p_ch), "e_max": np.array(e_max),
                      "eta_ch": np.array(ech), "eta_dis": np.array(edis), "vom": 0.5}
    return out
