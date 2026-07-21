# Modelling approach — end to end

> **This is the living overview of *how the whole thing works*.** It must be updated in the same change
> as any behavioural edit to the models (see [CONTRIBUTING.md](../CONTRIBUTING.md)). Per-step detail lives
> in each package's `METHODOLOGY.md`; the design decisions behind each choice live in the per-package
> `DECISIONS.md` and in [docs/ADR.md](ADR.md). This document is the map that ties them together.

## 1. What the project computes

A bottom-up, weather-driven simulator of **hourly electricity spot prices** for France (and five European
neighbours), usable both as a **backtest** against historical years and as a **20-year projection**
(2026–2046) under capacity and climate scenarios. Prices are formed the way the real market forms them —
by clearing supply against demand on an economic merit order — rather than fitted directly. Everything
above the raw market data is reconstructed from physical and economic first principles so the model can
be pushed into futures (high-RES, electrified demand, retired thermal fleets) that no historical
regression could reach.

The chain is a sequence of independent, individually validated packages. Each is one "step" (numbered
ii–vii, following the original design memo); each consumes the previous steps' outputs through shared
data artifacts, never through hidden coupling.

```
        ii                iii            iv              v                 vi                 vii
   ┌───────────┐    ┌───────────┐  ┌──────────┐   ┌──────────────┐   ┌────────────┐   ┌────────────┐
   │ weathergen│───▶│  demand   │  │   res    │   │ availability │   │  dispatch  │──▶│   markup   │
   │  weather  │─┬─▶│  (load)   │  │ (wind/PV)│   │ (fleet up/   │   │  7-zone LP │   │  SMC→spot  │
   │   cube    │ │  └─────┬─────┘  └────┬─────┘   │  down time)  │   │  → prices  │   │  (in disp.)│
   └───────────┘ └────────┼─────────────┼────────▶└──────┬───────┘   └─────┬──────┘   └─────┬──────┘
        │                 │             │                │                 │                │
        └── same draw ────┴─────────────┘                │                 │                │
                                                          ▼                 ▼                ▼
                                              net load, RES potential,  system marginal   day-ahead
                                              firm availability  ─────▶  cost (LP duals) ─▶ spot price
```

The shared substrate underneath all of it is **`powersim_core`** (glossary, RNG authority, the Parquet
lake + DuckDB catalog, the scenario workbook accessor, serialization, the weather-cube loader) and the
**`pricemodeling`** ETL that lands all the raw market/weather/registry data into one SQLite `master_hourly`
base. See [ARCHITECTURE.md](ARCHITECTURE.md) for the module map and the data-flow diagram.

## 2. Step ii — `weathergen`: the stochastic weather cube

A multi-site hourly **weather generator** fitted to ~12 years of French SYNOP observations (42 stations)
and ERA5 reanalysis. It produces `simulation.nc`, an `xarray` cube `(time, station, variable)` of
physically plausible synthetic weather over the 20-year horizon — temperature, wind (10 m **and** 100 m),
humidity, pressure, precipitation.

Key choices (detail in `weathergen/METHODOLOGY.md` / `DECISIONS.md`):
- **EVT marginals** (semi-parametric, generalized-Pareto tails) so extremes *extrapolate* beyond the
  observed record instead of being capped at historical maxima — essential for cold-snap / heatwave price
  events.
- **Dependence** via a Gaussian copula plus an EOF-VAR latent field, so spatial correlation (a cold, calm
  day is cold and calm nationwide) and temporal persistence (spell length) are reproduced, not just
  marginals.
- The **climate-change trend is imposed externally** (CMIP6 quantile deltas, quantile-delta mapping),
  never estimated from the short record — tail intensification comes from the climate model.
- 100 m wind is **co-generated** with 10 m via a fitted transfer so wind-power conversion downstream sees
  hub-height wind coherent with the surface field (see `weathergen/WIND_TEMP_COUPLING.md` for the
  temperature-coupling investigation and why the shipped model is transfer-only).

One RNG realization of the cube is the single weather input shared by demand and res, so their draws are
**coherent** (the same weather drives both load and renewables).

## 3. Step iii — `demand_model`: hourly French load

A **hybrid statistical–structural** long-term demand model. A calibrated statistical core decomposes
historical load into components (thermosensitive heating/cooling, calendar, base, lighting) with an
hourly temperature-response shape; a **structural projection layer** then evolves the base forward with
bottom-up drivers that a pure time-series model cannot see: heat-pump stock and its COP-vs-temperature
curve, EV fleet and charging profiles, electrolysis / datacentre point loads, efficiency gains,
behind-the-meter PV. A stochastic residual layer adds the correlated noise. Consumes the weathergen cube
for the temperature/irradiance drivers. Detail: `demand_model/METHODOLOGY.md`.

## 4. Step iv — `res_model`: weather → renewable production

Calibrated **conversion chains** turning the same weather draw into hourly **potential** production for
PV (utility + distributed), onshore wind, offshore wind (fixed + floating) and run-of-river hydro. A
transfer layer bridges station-level weather to ERA5-100 m hub-height wind and clear-sky-model irradiance;
the chains are calibrated to national capacity factors and their distributions; a stochastic residual
layer preserves the demand↔RES correlation structure. Because demand and res consume the **same**
weathergen realization, a cold, calm winter hour is simultaneously high-load and low-wind in the coupled
draw — the physical driver of scarcity pricing. Detail: `res_model/METHODOLOGY.md`.

## 5. Step v — `availability_model`: stochastic fleet availability

Unit-level **availability** of the French dispatchable fleet — the supply-side twin of demand's weather
risk. It models planned outages (nuclear refuelling/decennial cadence with a concurrency-capped
scheduler), forced outages (heavy-tailed durations), common-mode events (the 2022 stress-corrosion crisis
as the reference trough), weather derating (river-temperature limits on nuclear, hydro inflows) and
interconnector availability. Calibrated against **REMIT** outage disclosures (the market's own outage
feed, ingested by `pricemodeling`), with the forced/planned split re-parameterised to the ~8–10 % REMIT
share rather than inferred from production. Produces per-draw hourly available-MW trajectories that the
dispatch consumes. Detail: `availability_model/METHODOLOGY.md`.

## 6. Step vi — `dispatch_model`: the 7-zone economic dispatch

The price-formation core. A continuous **linear dispatch** (linopy/HiGHS, no unit commitment) over a
7-zone European footprint — France **unit-resolved** (≈170 units, SRMC = fuel/η + CO₂·intensity/η·EUA +
VOM), the neighbours DE-LU / BE / CH / IT-North / ES as aggregated technology-block stacks, a virtual
DE-REST export sink (NL+AT+DK+PL+CZ), and GB as a border supply curve — coupled by NTC-bounded
cross-border flows. Solved over rolling weekly windows.

The **price of each zone is the dual of its energy-balance constraint** (the system marginal cost, SMC).
Scarcity, negative prices and cross-border spreads are therefore *duals, never post-processed*: unserved
energy prices at VoLL, RES over-generation at the price floor, and — the negative-price mechanism —
must-take renewables bid a **subsidy-scheme supply curve** (paid-regardless FiT deep floors, sliding
market premiums, merchant ≈0) with the German **§51 EEG trigger** cancelling premiums after N consecutive
negative hours, solved as a fixed point. Hydro reservoirs get weekly energy budgets from historical guide
curves, and their water value is the dual of the budget cap. Detail: `dispatch_model/METHODOLOGY.md`.

### 6b. Le parc français est millésimé, et l'hydraulique a une valeur de l'eau (2026-07)

Deux corrections structurelles du cœur du dispatch, mesurées et documentées ici parce qu'elles déplacent
tous les prix.

**Le stack FR était le parc *maximum historique*, pas celui de l'année modélisée.** Le scan de capacité
portait sur tout l'historique sans filtre de déclassement, et seules les unités déclarées groupe par
groupe par RTE y entraient. D'où, face à la capacité installée : +156 % de charbon (Vitry, Bouchain, La
Maxe, fermées en 2015, y figuraient encore), +134 % de fioul (Aramon, Porcheville), Flamanville 3 présente
dès 2019 — et à l'inverse −75 % d'hydraulique de lac (2 140 MW contre 8 702 installés), −33 % de gaz,
−88 % de biomasse, tout le parc diffus étant absent. `io.fr_fleet` filtre désormais sur les unités
réellement en service dans l'année et comble l'écart avec l'installé RTE par un bloc agrégé, pris au bas
de la bande de rendement (les unités trop petites pour être déclarées sont les moins performantes).

*Effet mesuré, et il faut le lire correctement* : FR 2024 passe de −52,9 % à **−65,1 %** d'erreur baseload.
Le stack corrigé **dégrade** la métrique de niveau — parce que l'ancien compensait une erreur par une
autre : 6,3 GW de charbon et fioul morts soutenaient artificiellement les prix. La vraie sous-évaluation
est de −65 %, elle était simplement masquée. Les taux de capture, eux, s'améliorent (solaire FR 2024 :
écart au réel de −0,057 à +0,020). Une erreur masquée est pire qu'une erreur exposée, a fortiori en
projection où la compensation par du charbon mort ne survivrait pas.

**L'hydraulique de lac est offerte en courbe de tranches** (`hydro/water_value.py`), plus en bloc unique au
VOM. Un budget dur et une valeur de l'eau scalaire sont équivalents dans un LP — le dual du budget *est* la
valeur de l'eau — et produisent un comportement tout-ou-rien. La courbe, calibrée sur les couples (prix
observé, production observée), reproduit ce qu'un scalaire ne peut pas : 13-25 % de la capacité produit
**même à prix négatif** (débit réservé, offert sous zéro, ce qui préserve la queue négative là où un
`min_gen_frac` dur la supprimerait), et l'élasticité est graduelle car un parc agrège des retenues aux
coûts d'opportunité différents.

*Effet mesuré, mitigé et à connaître* : FR 2024 gagne (MAE 40,3 → 34,7, corrélation 0,59 → 0,66),
IT_NORTH gagne en niveau (−7,1 → −1,3 %), mais **CH se dégrade** (+29,7 → +37,5 %, MAE 27,9 → 32,7) et
reste à instruire. Surtout, l'objectif déclaré **n'est pas atteint** : la distribution française reste
dégénérée, 69 % des heures à 7,0 €/MWh. La cause est arithmétique — 63 GW de nucléaire à prix unique
contre 8,7 GW d'hydraulique différenciée. **Différencier l'offre nucléaire est le prochain verrou**, et
c'est aussi ce qui rend le markup insoluble aujourd'hui (voir §7).

## 7. Step vii — the SMC→spot markup (the "wedge")

The LP returns *marginal cost*; real **day-ahead spot** sits above it on average and is more volatile
(unit-commitment start-up/no-load recovery, scarcity rents, downward decoupling in surplus). Step vii
(`dispatch_model/markup.py`) fits that wedge — `spot = SMC + markup(drivers)` — as a **sign-constrained
ridge regression on projectable structural drivers only** (SMC level, system tightness = residual demand
/ firm capacity, RES share, hour/month harmonics — **never** a calendar-year dummy, which could not
extrapolate to 2040). Economic sign constraints keep the wedge non-decreasing in price and tightness so
it degrades gracefully in the high-RES/high-price 2040 regime instead of extrapolating absurdly. Fitted on
a multi-regime panel (2019 normal + 2022 gas crisis + 2023) with a quality gate that drops zone-years the
dispatch prices badly. Detail: `dispatch_model/STEP_VII_METHODOLOGY.md`.

**Ce markup est un problème mal posé aujourd'hui, et le savoir évite de le réajuster en boucle.** Mesuré
hors échantillon (ajusté sur 2019+2022, testé sur 2023/2024) puis après correction du stack :

- il **triple l'erreur** en 2019 (FR : MAE 8,3 brut → 22,9 avec markup) tout en aidant en 2022 — un wedge
  unique ne peut pas servir des régimes dont l'écart varie d'un facteur cinq ;
- il **détruit la métrique de valorisation** : capture solaire FR 2024 à 1,104 contre 0,676 observé, là où
  le LP brut sort 0,697. Un taux supérieur à 1 signifierait que le solaire produit aux heures chères ;
- il est **instable** : retirer une seule année d'entraînement dégrade *toutes* les cellules.

Une variante monotone (appariement de quantiles SMC→spot, préservant l'ordre des heures) a été essayée et
**échoue** : 6 864 heures négatives contre 352 observées en FR 2024. La raison vaut d'être retenue —
la distribution des SMC est **dégénérée** (69 % des heures à 7,0 €/MWh), et aucune transformation, additive,
multiplicative ou monotone, ne peut extraire une distribution réaliste d'une masse ponctuelle. Le code de
cette variante reste dans `markup.py` (`fit_markup_monotone`) mais **n'est pas utilisé**.

**Conclusion : le markup ne pourra être ajusté correctement qu'après avoir cassé la dégénérescence**, donc
après avoir différencié l'offre nucléaire (§6b). Le réajuster avant est du réglage sur un problème mal posé.

## 7b. A learned surrogate was tried and rejected (2026-07)

Recorded so it is not rebuilt from scratch. To speed up Monte-Carlo runs, a model was built to predict the
**marginal tranche** per hour (weak-supervised labels from ENTSO-E, ratio features, a linear-chain CRF for
ramp structure, analytic tranche→SRMC mapping) and fall back to the LP when unsure. It **failed its
held-out-2024 gate and the code was deleted**; the LP is the only price path.

Why, in order of importance:

1. **Label quality is the binding constraint, not model capacity.** The marginal tranche is *latent* —
   ENTSO-E never publishes which unit set the price. Even with the *true* derived label the implied price
   still erred by **21-30 €/MWh**; in IT_NORTH that ceiling was worse than a trivial merit order.
2. **It did not reliably beat no-ML.** Reading residual load off the supply curve won in 2 of 5 zones.
3. **The Markov structure did not clearly pay.** Tranche accuracy 59.9 % against a 56.1 % majority-class
   floor, and the best accuracy in the sweep came from the *chain-free* variant.
4. **Negative and scarcity hours are structurally unreachable** from a tranche label (they are set by RES
   support floors / the §51 trigger, and by scarcity rents), so they would always defer to the LP anyway.

Against the *repaired* LP the surrogate was in fact competitive pre-markup (2024 MAE: FR 32.9 vs 33.8,
DE_LU 24.3 vs 31.7, but ES 32.6 vs 26.5) — it is recorded here only to note that the gap was not the
reason for rejection. The reasons are the four above, plus the fact that the LP's production path is
LP **+ markup**, and at ~60 % tranche accuracy the deferral rate would have eaten the speedup that was the
entire motivation.

**If ever resumed, start with label quality, not architecture** — specifically hydro water value (the
reason Switzerland had to be dropped) and a setting-zone head. Full implementation: commit `7b69827`.

## 8. Backtest vs projection

- **Backtest** (`dispatch-model backtest`) clears a **historical** year against ENTSO-E actuals (real net
  loads, observed reservoir energy, REMIT nuclear availability) and scores the §8 price metrics —
  baseload error, quantile errors, correlation, negative/spike frequency — against observed spot. This is
  the acceptance gate; the 2019 baseline is frozen in the golden harness.
- **Projection** (`dispatch-model run` / `rolling.projection`) clears a **future** year: it takes a
  reference-year hourly weather shape (or a re-drawn weathergen realization via the `weather_shapes` hook)
  and evolves the *structure* forward — capacity from **TYNDP** trajectories (with a flexibility fleet for
  2040 adequacy) or a CAGR fallback, the year-varying RES subsidy bid stack (support roll-off, §51 trigger
  tightening 6h→1h), forward commodity prices — then applies the markup. Output: per-zone hourly spot
  trajectories 2026–2046.

## 9. Cross-cutting engineering (what makes it reproducible)

- **`powersim_core`** is the single authority for: the naming glossary ([CONVENTIONS.md](../CONVENTIONS.md)),
  the RNG (`SeedSequence` keyed by draw id — same seed + config ⇒ identical output, collision-free across
  processes), the Parquet **lake** + DuckDB **catalog** (all model outputs), portable **JSON+npz model
  serialization** (never pickle), and the single hand-edited **`scenarios.xlsx`** workbook.
- **Golden harness** (`tools/golden.py` + `golden/baseline.json`) freezes 13 model outputs by numerical
  stat-digest; `python tools/golden.py check` gates every change so a refactor cannot silently move a
  number.
- Architecture decisions are recorded in [docs/ADR.md](ADR.md); the full Phase-0/1 and Phase-2 code
  reviews in [REVIEW.md](../REVIEW.md).

## 10. Honest limitations (do not oversell the output)

- Neighbour zones use **reduced-form** weather-response models (load ~ FR national temperature, RES ~ FR
  national CF), not station-resolved weather — a full build would extend weathergen and the demand/RES
  models to each neighbour.
- The headline 2026–2046 projection is a **deterministic central path on a fixed reference-year weather
  shape**, not a weather-ensemble distribution. The machinery to run ensembles is in place — the
  `weather_shapes` hook + per-draw REMIT availability, and a **parallel Monte-Carlo harness**
  (`dispatch_model/rolling/montecarlo.py`) that runs draws across cores byte-identically to serial — but
  the *headline* figures quoted here are one central path, not a distribution.
- **IT-North** markup quality is poor (negative R²) — a dispatch-side problem (IT scarcity premium / gas
  basis), documented not hidden.
- 2040 capacity is a **starter TYNDP trajectory** (editable in `scenarios.xlsx`), and raw RTE/ENTSO-E
  re-pulls are **not bit-reproducible** because the sources revise published history.
