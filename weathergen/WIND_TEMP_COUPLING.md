# #39 — Winter demand–RES correlation gap: quantified diagnosis

> **⚠ CORRECTION (#79, 2026-07-18) — read this first.** The original diagnosis below blamed the wind100
> co-generation for the temp↔wind gap. That was **wrong**, and the error was an **apples-to-oranges
> comparison**: the "historical +0.48" was a *raw* correlation with the seasonal cycle left in, compared
> against a cube number computed differently. Measured **consistently** (deseasonalized, DJF, nat-avg), the
> historical temp↔w100 coupling is **0.28**, and the cube already reproduces it (**0.24** — within 0.04). And
> deseasonalized, w100 couples *less* than w10 (0.28 < 0.41), the **opposite** of the premise that "100 m
> couples more" (that too was a raw-correlation artifact). **The wind100 co-generation is fine; no fix is
> warranted there.** A temperature-conditioning term was implemented, fitted, and **rejected** — it made the
> cube coupling *worse* (0.24 → 0.12), because after controlling for log(w10) there is no extra *local* temp
> signal (per-station resid↔temp ≈ −0.11), and the nat-avg residual carries only 0.10 coupling (a
> residual-common-mode fix can't reach the target either). See "§79 — corrected analysis" at the bottom.
> The real, small residual gap is in the **dependence layer** (cube w10 0.34 vs hist 0.41) and **load
> composition** (legitimate). The shipped model stays transfer-only.

**Symptoms (as originally reported).** Projected within-winter (DJF) `corr(load, wind_CF) = −0.15` vs historical
`−0.34`; and the cube's DJF `corr(temp, wind) = +0.25` vs historical `+0.48`. *(The +0.48 is the mismeasured
raw figure — see the correction above; the like-for-like historical number is 0.28.)*

The three hypothesised sources were (a) the wind100 co-generation's independent residual, (b) the seasonal-VAR
dependence, (c) future load composition (EV/HP/electrolysis). **Measured, on the fitted model + the actual
simulation cube + the historical panel:**

| coupling | value | source |
|---|---|---|
| historical DJF `corr(temp, w10)` | **0.380** | `load_cube`, n=24 562 h, nat-avg |
| cube DJF `corr(temp, w10)` | **0.340** | `simulation.nc` |
| cube DJF `corr(temp, w100)` | **0.244** | `simulation.nc` (≈ the reported +0.25) |
| historical DJF `corr(temp, w100)` | **~0.48** | ERA5 100 m (the reported target) |
| wind100 transfer R² (mean) | **0.351** | `models/wind100.json` → 65 % independent residual |

## ~~The finding: it is (a)~~ — SUPERSEDED by §79 (kept for the investigation trail)

> Everything from here to "§79 — corrected analysis" rests on the mismeasured 0.48 and is **wrong**; read it
> only as the reasoning that §79 overturns. The measured, like-for-like conclusion is at the bottom.

The decisive fact is the **inversion**. Physically, 100 m wind couples to temperature *more* strongly than 10 m
wind (0.48 > 0.38) — it is more geostrophic/synoptic, so it tracks the pressure systems that also set
temperature. But the co-generation produces the **opposite**: cube `corr(temp, w100)=0.244 < corr(temp,
w10)=0.340`.

That is baked into the model form (`wind100.py`):

    log(w100) = a + b·log(w10) + r,   r = spatially-correlated AR(1),  r ⟂ temperature

- All of w100's temperature information can only arrive **through w10** (the `b·log(w10)` term).
- The residual `r` is drawn from `rng.standard_normal` — **temperature-independent by construction** — and it is
  **65 %** of the w100 variance, so it dilutes the inherited coupling by √R² ≈ 0.59.
- Net: `corr(temp,w100)_cube ≈ corr(temp,w10)_cube · √R² ≈ 0.34 · 0.59 ≈ 0.20` (measured 0.24). The model
  **cannot** represent the extra synoptic coupling that real 100 m wind has *over* 10 m wind — it can only ever
  land *below* the 10 m coupling, when physics says it should be *above*.

### Contribution of each hypothesis

**temp↔wind gap (0.48 → 0.25):**
- **(a) wind100 co-generation — dominant (~0.20 of the 0.24 gap).** Both the 65 % independent-residual dilution
  *and* the structural inability to carry synoptic temp↔w100 coupling beyond w10.
- **(b) seasonal-VAR under-coupling — minor (~0.04).** The cube reproduces `corr(temp, w10)` = 0.34 vs the
  historical 0.38 well; the dependence layer is *not* the main problem.

**corr(load, wind_CF) gap (−0.34 → −0.15):** with `corr(load,wind) ≈ corr(load,temp)·corr(temp,wind)` (wind
reaches load mainly via temperature; historical `corr(load,temp) ≈ −0.70` reproduces −0.70·0.48 = −0.34):
- **~85 %** from the temp↔wind collapse above (i.e. almost all **(a)**): −0.34 → −0.70·0.25 = **−0.175**.
- **~15 %** from **(c)** load composition — EV/HP/electrolysis flatten `corr(load,temp)` (−0.70 → ~−0.60):
  −0.175 → **−0.15**. This part is **genuine future physics**, not a modelling defect, and should be *kept*.

## Recommended fix (priority order) — follow-on task

1. **(a) — the lever.** Condition the wind100 co-generation on the coherent temperature/pressure state so w100
   can be *more* temp-coupled than w10, matching reality. Options, cheapest first: add the standardized
   temperature anomaly (or MSLP / pressure-gradient proxy) as an extra predictor in the w100 **mean**
   (`log(w100) = a + b·log(w10) + c·temp_anom + r`); and/or correlate the AR(1) innovations with the temperature
   field. Re-fit `Wind100Model`, re-run the res_model `_killer_test`. Target: cube `corr(temp,w100)` ≈ 0.45–0.48
   and `corr(load,wind)` ≈ −0.30.
2. **(b)** — optionally tighten the seasonal-VAR, but the payoff is only ~0.04; low priority.
3. **(c)** — no change; document that ~15 % of the load–wind decorrelation is real (a less weather-sensitive
   future load), so the historical −0.34 is *not* the right projection target — roughly −0.30 is.

*Investigation only (#39): the numbers above are measured; the fix is filed as a separate task.*

---

## §79 — corrected analysis (the fix attempt and why it was rejected)

Acting on the original diagnosis, a temperature term was added to the co-generation
(`log(w100) = a + b·log(w10) + c·temp_anom + r`, `temp_anom` deseasonalized by a day-of-year harmonic
climatology). It was fitted on 42 stations × 100 752 h and measured end-to-end. **It failed**, and the
measurements corrected the diagnosis itself. All figures deseasonalized, DJF, nat-avg (the like-for-like basis):

| quantity | value | note |
|---|---|---|
| HIST corr(temp_anom, log **w100**) | **0.28** | the *real* target (not 0.48) |
| HIST corr(temp_anom, log **w10**) | **0.41** | w10 carries the coupling |
| CUBE corr(temp, w100) | **0.24** | already ≈ the historical 0.28 |
| CUBE corr(temp, w10) | **0.34** | slightly under the historical 0.41 |
| mean per-station corr(residual, temp_anom) | **−0.11** | no local coupling to add |
| nat-avg corr(residual, temp_anom) | **0.10** | far below the ~0.35 a residual fix would need |
| cube coupling **with** the temp term | **0.12** | the fix makes it *worse* |

**Conclusions.**
1. **No wind100 gap.** The co-generation reproduces the deseasonalized within-winter w100 coupling (0.24 vs
   0.28). The apparent 0.48→0.24 "gap" was raw-vs-deseasonalized. w100 couples *less* than w10 (residual
   dilution), correctly, and #39's "w100 couples more" was a raw-correlation artifact.
2. **Local temp term rejected.** After controlling for log(w10), per-station resid↔temp ≈ −0.11, so 83 % of
   stations fit c < 0 and the term *subtracts* coupling (cube 0.24 → 0.12).
3. **Residual-common-mode fix can't work.** The historical nat-avg residual carries only 0.10 temp coupling;
   to lift the cube from 0.24 to ~0.48 the residual would need ≈0.35. The signal isn't there.
4. **Where the (small) real gap is.** (a) the cube's w10 under-couples (0.34 vs hist 0.41) — a *dependence
   layer* / seasonal-VAR matter, worth ~0.07; (c) load composition (EV/HP/electrolysis) genuinely flattens
   `corr(load,temp)` — real future physics, keep it. Neither is a wind100 issue.

**Disposition.** Shipped `Wind100Model` stays **transfer-only** (`c=None`); `fit_wind100.py` has `USE_TEMP=False`.
The temperature-conditioning capability remains in `wind100.py` (backward-compatible, tested) as dormant wiring
for a possible future *dependence-layer* fix — not a wind100 one. The residual demand–RES winter correlation,
to the extent it is still low, should be pursued in the dependence model (cube w10 coupling), not here.

---

## §82 — assessment of the cube w10 winter temp-coupling gap (dependence layer)

The only real residual from §79 is the cube's DJF nat-avg `corr(temp, w10)` = **0.34** vs historical **0.41**
(~0.07). #82 asked whether the seasonal-VAR dependence layer can be tightened to close it. Measured on the
cached Gaussian latent field (`models/_gauss_cache.npz`, 412 752 h × 252 = 42 stations × 6 vars):

| stage (DJF nat-avg temp↔wind, **gauss space**) | value |
|---|---|
| raw historical, full rank (252 modes) | **0.297** |
| EOF-reconstructed at 90 % (58 modes) | 0.298 |
| EOF-reconstructed at 95 % / 99 % | 0.297 |

**EOF truncation is NOT the cause** — reconstruction at the shipped 90 % is identical to full rank (0.298 vs
0.297). Raising `eof_variance` would do nothing; that cheap lever is ruled out.

**The locus is the VAR simulation.** The historical gauss-space coupling is a modest 0.297; the same marginals
map that to the historical 0.41 in original space. Since the cube lands at 0.34 < 0.41 in original space, its
gauss coupling must be *below* 0.297 — i.e. the monthly VAR reproduces only part of the (already modest)
contemporaneous coupling. By design (decision D5.3) the cross-variable coupling is carried by the VAR
**dynamics**, with one-step innovation cross-covariance ≈ 0; a VAR(6) converges toward but does not fully reach
the monthly stationary coupling within a season.

**Recommendation: accept 0.34; do not tighten.** The only fix would be to inject contemporaneous winter
innovation covariance (or raise `var_order`) — a change to the *validated* dependence model (the seasonal-VAR
was itself a delicate fix; a first attempt on innovation covariance alone failed) with real re-validation risk,
for a ~0.07 payoff. And §79 already showed the projection's `corr(load,wind)` target is **~−0.30**, not the
historical −0.34, because future load (EV/HP/electrolysis) is genuinely less temperature-sensitive — so the
cube's slightly-lower coupling is *partly the right direction*. Effort ≫ benefit. If ever revisited, the lever
is a small winter contemporaneous term in `innov_chol_m`, re-validated against the full Phase-8 dependence suite
— not the EOF rank.
