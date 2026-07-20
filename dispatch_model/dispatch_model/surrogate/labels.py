"""Derive the marginal-tranche label from ENTSO-E data alone (weak supervision).

**The target is latent.** ENTSO-E publishes prices, generation per production type, load, flows and REMIT
outages — never "which unit set the price". So the label is *constructed* from real data and carries an
explicit confidence, rather than being pretended into existence. Three signals, established empirically in
the S0 feasibility work:

1. **Response (Δ)** — the marginal plant is the one that *moves* to absorb the change in residual demand.
   Ranking movers by **magnitude** is wrong: it finds the fleet's shock-absorber (French nuclear modulates
   in large MW blocks, hydro follows its water-value schedule) and mislabels ~48 % of hours as nuclear,
   implying an SRMC 24-52 €/MWh below the observed price. Ranking the movers by **cost** finds the
   price-setter — gas in 80 % of FR hours, median implied-vs-observed error +4.7 €/MWh. The Δ filter also
   beats the static "running with headroom" test (median bias +4.7 vs +10.5 €/MWh) because it excludes
   plant that *could* move but didn't: ramp-blocked, reserve-held or heat-obligated — i.e. it is the
   empirical footprint of exactly the ramp/commitment constraints the sequence model must learn.

2. **Market coupling** — the label must be defined over the **price-coupled area**, not one zone. France
   shares an identical clearing price with a neighbour in ~61-65 % of hours, so "which of *France's* techs
   is marginal" is wrong by construction there; pooling the coupled area's merit order lifted FR label
   agreement from 68 %→90 % (2019) and 57 %→83 % (2023).

3. **Price matching** — an independent cross-check: the tranche whose SRMC sits nearest the observed
   price. Agreement between (1)+(2) and (3) is the confidence signal; disagreement marks the hour
   ambiguous rather than silently guessing.

Candidates are **capacity-filtered**: without it the matcher assigns lignite to Spain (43 %) and coal to
FR-2023 (45 %) — technologies those zones barely have — purely because their SRMC lands near the price.

The label is **factorised** into (`setting_zone`, `tranche`), which mirrors how coupling actually clears
and keeps the classifier's job learnable: *whose* merit order sets my price, then *which* step of it.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

#: dispatchable techs that can set a price by their SRMC
DISPATCHABLE = ("nuclear", "lignite", "coal", "gas", "oil", "biomass", "hydro_reservoir")
MIN_TECH_CAPACITY_MW = 200.0      # below this a tech cannot credibly set the zonal price
COUPLE_EPS = 0.01                 # €/MWh: identical clearing price ⇒ same coupled area
RUN_FLOOR, RUN_CEIL = 0.02, 0.98  # running / has-headroom band, as a fraction of available capacity
MIN_MOVE_MW = 1.0                 # ignore numerical dither when testing "did it move"


def capacity_proxy(gen: pd.DataFrame, quantile: float = 0.995) -> pd.Series:
    """Available capacity per tech from observed generation only (a high quantile). Purely ENTSO-E —
    no model input — and it is what the capacity filter below is applied to."""
    cap = gen.quantile(quantile)
    return cap[cap >= MIN_TECH_CAPACITY_MW]


def coupled_zones(prices: pd.DataFrame, zone: str, eps: float = COUPLE_EPS) -> pd.DataFrame:
    """Boolean [t × zone]: which zones share `zone`'s clearing price each hour (itself always True)."""
    return (prices.sub(prices[zone], axis=0).abs() < eps)


def candidate_mask(gen: pd.DataFrame, cap: pd.Series, d_resid: pd.Series) -> pd.DataFrame:
    """Plants able to be marginal this hour: running, with headroom, and **moved with** Δresidual.

    The direction test is what makes this the *response* signal rather than a static availability test.
    """
    g = gen[cap.index]
    running = g.gt(RUN_FLOOR * cap, axis=1) & g.lt(RUN_CEIL * cap, axis=1)
    moved = g.diff().mul(np.sign(d_resid), axis=0) > MIN_MOVE_MW
    return running & moved


def _regime(price: pd.Series, max_srmc: pd.Series) -> pd.Series:
    """negative | thermal | scarcity — the three price-formation regimes, scored separately downstream."""
    out = pd.Series("thermal", index=price.index, dtype=object)
    out[price < 0] = "negative"
    out[price > max_srmc * 1.5 + 20] = "scarcity"
    return out


def derive_labels(prices: pd.DataFrame, gen: dict[str, pd.DataFrame], srmc: dict[str, pd.DataFrame],
                  d_resid: dict[str, pd.Series], eps: float = COUPLE_EPS) -> pd.DataFrame:
    """Label every (zone, hour) with the marginal tranche of its price-coupled area.

    `prices` [t × zone] observed spot; `gen[zone]` [t × tech] observed generation; `srmc[zone]` [t × tech]
    SRMC from the **exogenous** price vector (see `surrogate.tranches`); `d_resid[zone]` hourly change in
    residual demand. Returns one row per (zone, hour):

        timestamp_utc, zone, setting_zone, tranche_tech, srmc_implied, price_observed,
        regime, agrees_price_match, margin_eur, confidence
    """
    zones = [z for z in prices.columns if z in gen and z in srmc]
    caps = {z: capacity_proxy(gen[z]) for z in zones}
    cands = {z: candidate_mask(gen[z], caps[z], d_resid[z]) for z in zones}
    idx = prices.index
    max_srmc = pd.concat([srmc[z][caps[z].index].max(axis=1) for z in zones], axis=1).max(axis=1)

    rows = []
    for z in zones:
        coup = coupled_zones(prices[zones], z, eps)
        # pooled candidate SRMCs across the coupled area: NaN where not a candidate or not coupled
        pooled, owner = {}, {}
        for o in zones:
            s = srmc[o][caps[o].index].where(cands[o])                 # candidates only
            s = s.where(coup[o], other=np.nan)                          # coupled hours only
            for tech in s.columns:
                key = f"{o}|{tech}"
                pooled[key] = s[tech]
                owner[key] = (o, tech)
        P = pd.DataFrame(pooled, index=idx)
        # Δ-response is the FILTER (which plants could physically be marginal); the observed clearing
        # price is the SELECTOR (which of them actually set it). Taking the pooled max instead is an
        # upper envelope — across a 6-zone coupled area it picks the dearest plant ramping anywhere,
        # usually a peaker moving for non-price reasons (measured: 29 % agreement, 12-104 €/MWh error).
        # The label may use the price; the *model* never sees it at prediction time.
        D = P.sub(prices[z], axis=0).abs()
        best = D.idxmin(axis=1)                                         # pandas idxmin skips NaN
        has = D.notna().any(axis=1)
        pos = D.fillna(np.inf).to_numpy().argmin(axis=1)                # match idxmin's NaN handling
        implied = pd.Series(P.to_numpy()[np.arange(len(idx)), pos], index=idx).where(has)
        srt = np.sort(np.nan_to_num(D.to_numpy(), nan=np.inf), axis=1)
        margin = pd.Series(srt[:, 1] - srt[:, 0] if srt.shape[1] > 1 else np.inf, index=idx)

        # independent cross-check: nearest-SRMC tranche in this zone
        own = srmc[z][caps[z].index]
        pm_tech = own.sub(prices[z], axis=0).abs().idxmin(axis=1)
        lab_tech = best.map(lambda k, o=owner: o[k][1] if isinstance(k, str) else None)
        set_zone = best.map(lambda k, o=owner: o[k][0] if isinstance(k, str) else None)

        err = (implied - prices[z]).abs()
        rows.append(pd.DataFrame({
            "timestamp_utc": idx, "zone": z, "setting_zone": set_zone.to_numpy(),
            "tranche_tech": lab_tech.to_numpy(), "srmc_implied": implied.to_numpy(),
            "price_observed": prices[z].to_numpy(), "regime": _regime(prices[z], max_srmc).to_numpy(),
            "agrees_price_match": (lab_tech == pm_tech).to_numpy(),
            "margin_eur": margin.to_numpy(), "abs_err_eur": err.to_numpy(),
        }))
    out = pd.concat(rows, ignore_index=True)
    # confidence: agreement with the independent check, a well-separated best candidate, and a small
    # implied-vs-observed gap. Ambiguous hours are down-weighted rather than dropped, so the model still
    # sees them but cannot be dominated by them.
    out["confidence"] = (0.5 * out["agrees_price_match"].astype(float)
                         + 0.25 * (out["margin_eur"].fillna(0) > 3).astype(float)
                         + 0.25 * (out["abs_err_eur"] < 10).astype(float))
    out.loc[out["tranche_tech"].isna(), "confidence"] = 0.0
    return out


def label_quality(labels: pd.DataFrame) -> pd.DataFrame:
    """Per-zone honesty report: coverage, agreement with the independent check, implied-price error and
    class balance. This is the gate — a model is only ever as good as this table."""
    rows = []
    for z, g in labels.groupby("zone"):
        th = g[g["regime"] == "thermal"]
        rows.append({
            "zone": z, "hours": len(g),
            "pct_labelled": 100 * g["tranche_tech"].notna().mean(),
            "pct_thermal": 100 * (g["regime"] == "thermal").mean(),
            "agree_pct": 100 * th["agrees_price_match"].mean() if len(th) else np.nan,
            "median_abs_err": th["abs_err_eur"].median() if len(th) else np.nan,
            "pct_coupled_out": 100 * (th["setting_zone"] != z).mean() if len(th) else np.nan,
            "mean_confidence": g["confidence"].mean(),
        })
    return pd.DataFrame(rows)
