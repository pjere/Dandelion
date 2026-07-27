"""Lecture du LP résolu : quelle contrainte mord, quel bloc porte le dual, heure par heure.

Pourquoi ce module existe. Six hypothèses ont été testées une par une sur la formation des prix — plancher
nucléaire, export, budget hydraulique, must-run voisin, valeur de l'eau, markup monotone — et cinq sont
tombées à la mesure. C'est le symptôme d'un mauvais protocole : deviner un correctif, le coder, le mesurer,
recommencer. Sur un système où plusieurs mécanismes se compensent, il faut **lire la solution** plutôt que
la deviner.

Le LP contient déjà la réponse. Le prix zonal est le dual de la contrainte de bilan ; le bloc qui le porte
est celui qui est **partiellement chargé** — strictement entre ses bornes — car un bloc saturé ou à l'arrêt
ne fixe rien. Ce module extrait, par (zone, heure) :

  - le bloc marginal (unité, tranche RES, effacement ou écrêtement) et sa technologie ;
  - les contraintes actives : interconnexion saturée, budget énergétique épuisé, ENS, écrêtement ;
  - la marge du bloc marginal, c'est-à-dire l'écart au bloc suivant dans l'ordre de mérite.

Coût : la solution primale est déjà calculée, il ne reste qu'à la lire. `diagnose=False` par défaut, donc
le chemin de production et le golden ne changent pas.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TOL = 1e-6          # tolérance d'égalité aux bornes, en MW
PRICE_TOL = 0.01    # €/MWh : au-delà, le bloc n'est pas celui qui porte le dual


def _partially_loaded(v: np.ndarray, lo: np.ndarray, up: np.ndarray) -> np.ndarray:
    """Blocs strictement entre leurs bornes : les seuls qui peuvent fixer le prix."""
    return (v > lo + TOL) & (v < up - TOL)


def marginal_report(spec: dict, col_value: np.ndarray, prices: pd.DataFrame) -> pd.DataFrame:
    """Une ligne par (zone, heure) : bloc marginal, technologie, contraintes actives.

    `spec` vient de `_build`, `col_value` de la solution HiGHS, `prices` du dual de bilan.
    """
    T, zones, n = spec["T"], spec["zones"], spec["n"]
    lo, up, cost = spec["col_lo"], spec["col_up"], spec["col_cost"]
    rows = []
    for z in zones:
        gbase, m, units, techs = spec["gen_cols"][z]
        g = col_value[gbase:gbase + m * n].reshape(m, n)
        glo = lo[gbase:gbase + m * n].reshape(m, n)
        gup = up[gbase:gbase + m * n].reshape(m, n)
        gsrmc = np.asarray(spec["srmc_by_unit"][z], float)

        rbase, ntr = spec["res_cols"].get(z, (None, 0))
        if rbase is not None and ntr:
            r = col_value[rbase:rbase + ntr * n].reshape(ntr, n)
            rlo = lo[rbase:rbase + ntr * n].reshape(ntr, n)
            rup = up[rbase:rbase + ntr * n].reshape(ntr, n)
            rcost = cost[rbase:rbase + ntr * n].reshape(ntr, n)
            rsch = spec["res_schemes"].get(z, [f"res{i}" for i in range(ntr)])
        else:
            r = rlo = rup = rcost = np.zeros((0, n))
            rsch = []

        ens = col_value[spec["ens_cols"][z]:spec["ens_cols"][z] + n]
        dump = col_value[spec["dump_cols"][z]:spec["dump_cols"][z] + n]
        p = prices[z].to_numpy(float)

        gpart = _partially_loaded(g, glo, gup)
        rpart = _partially_loaded(r, rlo, rup)
        for t in range(n):
            # candidats : blocs partiellement charges dont le cout egale le prix a PRICE_TOL pres
            cand_tech, cand_id, cand_cost = None, None, np.nan
            best = np.inf
            for i in np.nonzero(gpart[:, t])[0]:
                d = abs(float(gsrmc[i]) - p[t])
                if d < best:
                    best, cand_tech, cand_id, cand_cost = d, str(techs[i]), str(units[i]), float(gsrmc[i])
            for j in np.nonzero(rpart[:, t])[0]:
                d = abs(float(rcost[j, t]) - p[t])
                if d < best:
                    best, cand_tech, cand_id, cand_cost = d, "res", str(rsch[j]), float(rcost[j, t])
            if ens[t] > TOL:
                cand_tech, cand_id, cand_cost, best = "ens", "ens", float(cost[spec["ens_cols"][z] + t]), 0.0
            elif dump[t] > TOL and cand_tech is None:
                cand_tech, cand_id, cand_cost, best = "dump", "dump", -float(p[t]), 0.0
            rows.append({
                "timestamp_utc": T[t], "zone": z, "price": float(p[t]),
                "marginal_tech": cand_tech, "marginal_id": cand_id, "marginal_cost": cand_cost,
                # ecart entre le cout du bloc retenu et le prix : > PRICE_TOL signale que le dual est porte
                # par une contrainte (interconnexion, budget) et non par un bloc de la zone
                "price_gap": float(best) if np.isfinite(best) else np.nan,
                "set_by_constraint": bool(np.isfinite(best) and best > PRICE_TOL),
                "n_partial": int(gpart[:, t].sum() + rpart[:, t].sum()),
                "ens_mw": float(ens[t]), "dump_mw": float(dump[t]),
            })
    return pd.DataFrame(rows)


def dual_oscillation(prices: pd.DataFrame, demand: pd.DataFrame, avail: pd.DataFrame | None = None,
                     price_jump: float = 20.0, demand_frac: float = 0.02) -> pd.DataFrame:
    """Flag hour-to-hour balance-dual jumps that are NOT justified by a physical-state change — the signature
    of spurious dual degeneracy rather than a real price move (F6, spec §8).

    An hour `t` is flagged when `|price_t − price_{t−1}| > price_jump` yet the zone's demand moved by less than
    `demand_frac` (and, if `avail` is given, its mean availability is essentially unchanged). With the
    `_tie_break` ε-perturbation in place the degenerate ties are broken, so a clean run flags (near-)nothing;
    a spike of flags points at a still-degenerate family. `prices`/`demand`/`avail` are time×zone frames on the
    window index. Returns one row per flagged (zone, hour)."""
    out = []
    for z in prices.columns:
        p = prices[z].to_numpy(float)
        d = demand[z].to_numpy(float) if z in demand.columns else np.full(len(p), np.nan)
        a = avail[z].to_numpy(float) if (avail is not None and z in avail.columns) else None
        for t in range(1, len(p)):
            dp = abs(p[t] - p[t - 1])
            if dp <= price_jump:
                continue
            base = max(abs(d[t - 1]), 1.0)
            dfrac = abs(d[t] - d[t - 1]) / base if np.isfinite(d[t]) else np.inf
            afrac = abs(a[t] - a[t - 1]) if a is not None else 0.0
            if dfrac < demand_frac and afrac < demand_frac:        # physical state ~unchanged → spurious jump
                out.append({"timestamp_utc": prices.index[t], "zone": z,
                            "price_prev": float(p[t - 1]), "price": float(p[t]), "d_price": float(p[t] - p[t - 1]),
                            "demand_frac_change": float(dfrac)})
    return pd.DataFrame(out, columns=["timestamp_utc", "zone", "price_prev", "price", "d_price",
                                      "demand_frac_change"])


def binding_flows(spec: dict, col_value: np.ndarray, ntc: dict) -> pd.DataFrame:
    """Par (frontière, heure) : flux et saturation. Une interconnexion saturée découple les zones."""
    T, n = spec["T"], spec["n"]
    rows = []
    for name, (fb, wb) in spec["flow_cols"].items():
        f = col_value[fb:fb + n]
        w = col_value[wb:wb + n]
        fup = np.asarray(spec["col_up"][fb:fb + n], float)
        wup = np.asarray(spec["col_up"][wb:wb + n], float)
        rows.append(pd.DataFrame({
            "timestamp_utc": T, "border": name, "net_mw": f - w,
            "binding": (f > fup - TOL) | (w > wup - TOL)}))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(
        columns=["timestamp_utc", "border", "net_mw", "binding"])


#: stable names for the FLEX constraint families (F6) — the debug dump reads binding state off the primal
#: rather than raw LP row indices, so these are the human-facing names of what can set a price.
FLEX_FAMILIES = ("C1a-band-floor", "C1b-deep-band", "C2-deepmod-budget", "C3-xenon-ramp",
                 "C5-start", "min-down", "C6-reserve", "C7-grid-stability")


def debug_hour(spec: dict, col_value: np.ndarray, prices: pd.DataFrame, zone: str, hour: int) -> dict:
    """Dual decomposition of one (zone, hour) balance price (F6, spec §8): the marginal block, the binding
    constraints (saturated borders = the export lock), and the per-reactor commitment / deep-mod state that
    sets the print — the tool that explains an individual negative price for the F7/F8 reports. Reads the
    already-solved primal (`col_value`) + balance dual (`prices[zone]`); no re-solve.

    ``implied_bid`` for a flex unit is `srmc − c_mod` while it deep-modulates (the reservation price to keep
    producing the marginal MWh rather than pay the modulation cost it avoids) — for a nuclear negative print
    this equals the balance price, which is the decomposition the report hand-checks."""
    n, t = spec["n"], int(hour)
    pser = prices[zone]
    price = float(pser.iloc[t] if hasattr(pser, "iloc") else pser[t])
    gbase, m, units, techs = spec["gen_cols"][zone]
    g = col_value[gbase:gbase + m * n].reshape(m, n)
    glo = np.asarray(spec["col_lo"])[gbase:gbase + m * n].reshape(m, n)
    gup = np.asarray(spec["col_up"])[gbase:gbase + m * n].reshape(m, n)
    srmc = np.asarray(spec["srmc_by_unit"][zone], float)
    part = _partially_loaded(g[:, t], glo[:, t], gup[:, t])
    marg = None
    if part.any():
        i = np.nonzero(part)[0]
        j = int(i[np.argmin(np.abs(srmc[i] - price))])
        marg = {"unit": str(units[j]), "tech": str(techs[j]), "srmc": float(srmc[j]), "output_mw": float(g[j, t])}
    out = {"zone": zone, "hour": t, "timestamp_utc": spec["T"][t], "price": price, "marginal": marg,
           "set_by_constraint": bool(marg is None or abs(marg["srmc"] - price) > PRICE_TOL),
           "binding": [], "flex_units": []}
    for name, (fb, wb) in spec.get("flow_cols", {}).items():                    # saturated border = export lock
        f, w = float(col_value[fb + t]), float(col_value[wb + t])
        fup, wup = float(spec["col_up"][fb + t]), float(spec["col_up"][wb + t])
        if fup > TOL and f > fup - TOL:
            out["binding"].append(f"export saturated {name} ({f:.0f}/{fup:.0f} MW)")
        elif wup > TOL and w > wup - TOL:
            out["binding"].append(f"import saturated {name} ({w:.0f}/{wup:.0f} MW)")
    fz = (spec.get("flex_spec") or {}).get(zone)
    fc = spec.get("flex_cols", {}).get(zone)
    if fz is not None and fc is not None:
        ub, db, sb, fidx = fc
        ab = np.asarray(fz["alpha_band"], float); c_mod = float(fz["c_mod"])
        for k, ui in enumerate(fidx):
            u, d, su, p = (float(col_value[ub + k * n + t]), float(col_value[db + k * n + t]),
                           float(col_value[sb + k * n + t]), float(g[ui, t]))
            if u < TOL:
                continue                                                        # unit shut this hour
            flags = []
            if d > TOL:
                flags.append("C2-deepmod-budget" if p <= ab[k] * u - d + TOL else "deep-mod")
                flags.append("C1a-band-floor")
            if su > TOL:
                flags.append("C5-start")
            out["flex_units"].append({"unit": str(units[ui]), "u_mw": u, "p_mw": p, "deepmod_mw": d,
                                      "start_mw": su, "srmc": float(srmc[ui]),
                                      "implied_bid": float(srmc[ui] - (c_mod if d > TOL else 0.0)), "flags": flags})
    return out


def summarise(diag: pd.DataFrame) -> pd.DataFrame:
    """Par zone : qui fixe le prix, et dans quelle proportion. La lecture qui oriente les correctifs."""
    out = []
    for z, g in diag.groupby("zone"):
        n = len(g)
        mix = g["marginal_tech"].value_counts(normalize=True).mul(100).round(1)
        out.append({"zone": z, "heures": n,
                    "pct_par_contrainte": round(100 * g["set_by_constraint"].mean(), 1),
                    "pct_ens": round(100 * (g["ens_mw"] > TOL).mean(), 2),
                    "pct_dump": round(100 * (g["dump_mw"] > TOL).mean(), 2),
                    "n_partial_median": float(g["n_partial"].median()),
                    **{f"pct_{k}": v for k, v in mix.head(6).items()}})
    return pd.DataFrame(out).fillna(0.0)
