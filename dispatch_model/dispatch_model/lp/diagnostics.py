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
