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
VOM), the neighbours DE-LU / BE / CH / IT-North / ES as aggregated technology-block stacks, **four virtual
neighbour clusters** (NL / DK / PL_CZ / AT_SI — DE-LU's out-of-model neighbours, price-responsive; see §6f),
and GB as a border supply curve — coupled by NTC-bounded cross-border flows. Solved over rolling weekly
windows.

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
c'est aussi ce qui rend le markup insoluble aujourd'hui (voir §7). **C'est fait — voir §6d.**

### 6c. Valeur de l'eau structurelle : arbitrage de Bellman (2026-07)

La courbe de §6b est **descriptive** — préférence révélée : on observe le comportement passé et on en
déduit un prix de réserve implicite. Elle ne modélise aucun arbitrage et subit une circularité (calibrée
sur des prix observés pour produire les prix du modèle). `hydro/bellman.py` calcule la vraie grandeur :

    V_t(S) = E[ max_u  R_t(u) + V_{t+1}( min(S + I_t − u, S_max) ) ]        λ_t(S) = ∂V_t/∂S

`λ_t(S)` est le prix auquel offrir l'eau. Trois éléments font le travail qu'un ajustement de courbe ne
fait pas : `R_t(u)` est **concave** (l'eau va d'abord aux heures chères, ce qui étale la production
structurellement) ; la récursion donne une valeur qui dépend **du stock et de la saison** ; le
**déversement** est explicite, donc λ s'effondre à réservoir plein.

Deux points de méthode, tous deux des corrections d'erreurs faites en chemin :
- **itération sur la valeur relative** — sans actualisation V croît d'un gain annuel constant et ne
  converge jamais en niveau ; seules ses différences convergent, et λ étant un gradient le décalage est
  sans effet ;
- **balayage Gauss-Seidel** — lire l'itération précédente ne propage l'information que d'une semaine par
  balayage (λ encore en mouvement au 40ᵉ) ; réutiliser la valeur qui vient d'être calculée propage
  l'année entière, et converge en 3 à 5 balayages.

**Données** : `rte_water_reserves` (FR) et `entsoe_hydro_storage` (16.1.D — CH/ES/IT, ingéré par
`pricemodeling.entsoe.series.ingest_hydro_storage`, série **hebdomadaire** contrairement aux autres).
Les apports sont inférés par bilan `ΔStock + production`, sur **toute** la production tirant sur la
retenue (STEP comprises) : n'y mettre que la filière « reservoir » sous-estime les soutirages donc les
apports, et rendait l'eau trop chère. Le déversement n'étant pas publié, le stock est borné à `S_max` et
les semaines à moins de 2 % du plafond sont écartées.

**Validation — écart entre utilisation impliquée et observée du parc, 2024 :**

| zone | sans débit réservé | avec | observée |
|---|---|---|---|
| FR | −4,8 pts | **−0,3** | 0,228 |
| CH | −6,8 | **−1,8** | 0,275 |
| IT_NORTH | −7,6 | **+2,7** | 0,398 |
| ES | −3,4 | **−4,2** | 0,167 |

Écart absolu moyen 5,65 → 2,25 points. Le **débit réservé** (turbinage minimal, calibré au 5ᵉ centile de
la production observée) est une contrainte *physique* — obligations de débit, irrigation, navigation — que
l'arbitrage pur ne peut pas produire ; sans lui la SDP sous-utilisait le parc de 15 à 25 % dans les quatre
zones, biais de même signe partout.

Cette validation utilise une métrique d'**utilisation impliquée** `P(prix > λ)` — imparfaite, à ne pas
confondre avec l'objectif (l'erreur de prix). Elle a d'ailleurs mal orienté le cas espagnol (voir §6d).

### 6d. Câblage de la SDP : synthèse niveau-Bellman × dispersion-empirique (2026-07)

Les deux modèles de §6b et §6c ont chacun ce qui manque à l'autre. La courbe empirique capture la
**dispersion** réelle du parc (des retenues aux coûts d'opportunité différents) mais son **niveau** est
circulaire. La SDP donne un **niveau** λ_t(S) structurel, dépendant du stock et de la saison, mais son
réservoir équivalent agrégé écrase la dispersion. `hydro/synthesis.py` les combine : on recentre la
courbe empirique pour que sa valeur d'eau moyenne (tranches arbitrées, hors débit réservé et hors rareté)
égale λ_t(S_t) à la semaine et au stock courants —

    prix d'offre de la tranche i  =  λ_t(S_t)  +  (valeur empirique_i − moyenne empirique)

Le décalage est additif et uniforme (monotonie préservée) ; le débit réservé et la tranche de rareté
restent des ancres physiques. Câblé dans le backtest (`hydro_sdp_level`, actif par défaut), appliqué **par
fenêtre** — le λ de la semaine, pas une médiane annuelle. La **projection** ne l'utilise pas : elle n'a
pas de trajectoire de stock observée (il faudra une trajectoire projetée + le point fixe prix↔λ).

**Résultat** (backtest, |erreur baseload| moyenne 4 zones hydro) : 2024 25,55 → **23,50**, 2019
8,40 → **8,33**. Le gain vient surtout de l'Espagne (baseload −28,4 → **−22,0**, médiane −38,9 → **−11,9**,
corrélation 0,729 → **0,730**). La version par fenêtre gagne aussi la corrélation FR 2024 (0,761 →
**0,766**) ; la version statique (λ médian) était un cheveu meilleure en niveau mais perdait la
saisonnalité, donc la corrélation.

**Ce que ce résultat retourne (cf. réserve ES de §6c).** Le λ espagnol de 120,7 que la métrique
d'utilisation faisait voir comme un *défaut* (« saturation, trop haut ») **améliore le backtest** quand il
sert à fixer le niveau d'offre : l'empirique sous-price l'eau espagnole à 28 €/MWh, la SDP dit 121, et 121
est plus proche du vrai. Le diagnostic complémentaire montre d'ailleurs que le bilan d'eau ES boucle
(+10,4 %) et que `s_max` (série ENTSO-E déjà en énergie turbinable, max observé ≈ 80-86 % de la capacité
technique) n'est **pas** sur-estimé : la vraie spécificité espagnole est un lâcher d'irrigation saisonnier
price-agnostic, pas une capacité gonflée. Réduire `s_max` est écarté (#137 re-scopé sur un `min_release`
saisonnier depuis MITECO).

**Caveat de fuite, assumé.** La SDP calcule λ à partir des prix *observés* de l'année. Dans un backtest,
c'est une fuite — comme l'était déjà la courbe empirique (calibrée sur prix observés). La comparaison
empirique vs synthèse est donc équitable et le gain réel, mais aucune des deux n'est un résultat propre
hors échantillon. La levée passe par le point fixe prix↔valeur de l'eau, à traiter en projection. Restent
aussi des prix déterministes dans la SDP (seuls les apports sont aléatoires).

### 6e. Découplage suisse : plancher des NTC d'interconnexion (2026-07)

La Suisse était le pire résidu du modèle (+37,5 % en 2024) et on l'attribuait à la valeur de l'eau. La
**lecture du LP** (`lp.diagnostics`, désormais câblée jusqu'au backtest par un flag `diagnose` opt-in) dit
autre chose : le prix CH est porté par l'hydraulique **domestique 82 % des heures**, l'interconnexion ne
mord qu'à 3,9 %, et l'écart se concentre dans les heures **bon marché** (décile bas : modèle 50 contre 3,5
observé). Dans ces heures, CH est **découplée** de ses voisins bon marché de **64 €/MWh** (contre 5
observés) : la France est à 8, le modèle plante la Suisse à 67.

Cause : `flow_derived_ntc` dérive la NTC du **p99.5 des flux réalisés** — elle mesure l'usage, pas la
capacité, et sous-estime toute frontière non saturante. La Suisse, petite importatrice nette entourée de
quatre marchés, répartit ses imports sur plusieurs frontières dont aucune ne mord : chacune est sous-lue
(DE→CH à 960 MW contre ~4000 physiques ; la frontière CH-Autriche est en outre absente, l'Autriche étant
dans le puits DE_REST). Faute de capacité d'import, l'hydraulique suisse (correctement valorisée) reste
marginale au lieu de suivre le voisin bon marché.

**Le total dérivé est bon, c'est sa répartition qui est fausse.** Mesuré sur CH 2024 : la somme des
frontières dérivées donne 5 422 MW d'import contre **5 676 observés en p99.5 simultané** — le total est
juste, parce que c'est lui que le facteur de coïncidence contraint. C'est l'allocation qui dérape
(DE→CH lu à 960 MW pour ~4 000 physiques, les autres frontières compensant).

**Correctif** (`assemble._apply_ntc_floor`) : porter chaque frontière suisse à sa capacité **physique**
(table `NTC`), puis **renormaliser pour retrouver le total dérivé**, séparément dans chaque sens. DE→CH
remonte ainsi de 960 à 2 437 MW tandis que l'import total reste à 5 422 MW et l'export à 6 886 (soit
exactement le p99.5 observé). Résultat, backtest **année pleine** 2024 (|erreur baseload|) :

| zone | avant | après |
|---|---|---|
| CH | +37,9 | **+24** |
| IT_NORTH | −1,4 | −8 |
| DE_LU | −31,8 | −28 |
| BE | −19,4 | −19 |
| FR | −32,7 | −35 |
| ES | −22,0 | −23 |
| **moyenne** | **24,2** | **22,8** |

**Amélioration modeste, et encore un transfert partiel d'erreur** (CH −13,9 points, mais IT +6,6, FR +2,3).
Assumé : le correctif traite un défaut de *données* démontré sans en introduire un de *physique*.

**Variante rejetée, et c'est le point de méthode.** Plancher sans renormaliser score nettement mieux
(moyenne **19,0**, CH à +6) — mais en donnant à la Suisse **9 144 MW** d'import simultané contre 5 676
observés (+61 %) et 11 200 d'export contre 6 886. Elle achète son score avec une capacité de transit qui
n'existe pas, et noie l'Italie (−18). Même raisonnement que pour le markup, où la variante « + coût
combustible » gagnait 0,5 point en compensant un biais par un autre : **on ne garde pas un gain obtenu par
une erreur qui en compense une autre.** Deux autres variantes rejetées : toutes frontières planchées
(18,1, mais IT −28,1 et BE −24,5) et direction entrante seule (19,1, aucun gain).

**Ce qui reste, et c'est le vrai correctif complet : la topologie est incomplète.** CH↔AT est absente
(l'Autriche est dans le puits DE_REST), et IT_NORTH est amputée d'AT↔IT et SI↔IT — son import modélisé
plafonne à 6 177 MW contre 7 786 observés, et l'écart correspond presque exactement aux deux frontières
manquantes. Les ajouter donnerait aux deux zones leurs vraies options d'import sans gonfler aucun total
(#141). **→ fait au §6f** : le split de DE_REST rouvre CH↔AT et IT↔AT/SI ; CH passe de +11,4 à +7,4.

*Note de méthode, apprise à mes dépens.* `tools/golden.py capture` **hashe le lac existant, il ne rejoue
pas le backtest**. Après une série d'expériences qui écrasent chacune `data/lake/dispatch/backtest_prices`,
capturer sans avoir régénéré le lac avec le code retenu fige les chiffres d'une *autre* variante. C'est
arrivé ici : une première version de cette section annonçait une moyenne de 12,3 % qui était en réalité
celle d'un run « toutes frontières » sur 10 semaines d'hiver. Toujours régénérer le lac avec le code
commité **avant** `capture`.

### 6f. Split de DE_REST en quatre clusters voisins (2026-07)

Le puits `DE_REST` (NL+AT+DK+PL+CZ agrégés en une zone) achetait à DE-LU ses ~10,2 GW d'export manquants,
mais **agrégeait cinq marchés qui ne sont pas dans le même mode aux mêmes heures** : en abondance éolienne
allemande, NL peut encore importer pendant que PL/CZ exportent, si bien que la charge nette agrégée ne sature
jamais (+41 GW, 0 % d'heures de surplus en 2024). Deux conséquences : les négatifs régionaux restaient
sous-tirés (#138) et les frontières alpines (CH↔AT, IT↔AT/SI) étaient impossibles à représenter (#141).

Le split le remplace par **quatre clusters price-responsive** (chacun sa demande / son RES / son stack) :
`NL` (bord DE-LU, BE), `DK` (DK_1+DK_2), `PL_CZ`, `AT_SI` (AT+SI ; bords DE-LU, CH, IT-North). Données
ENTSO-E ré-extraites pour 2024 (charge, génération, flux — dont les nouvelles frontières BE↔NL, CH↔AT,
IT↔AT, IT↔SI). La NTC de chaque frontière reste dérivée des flux réalisés (`flow_derived_ntc`).

Backtest **année pleine** 2024, A/B à données identiques (|erreur baseload| SMC) :

| zone | DE_REST | split | corr DE_REST → split |
|---|---|---|---|
| BE | −16,4 | **−2,5** | 0,76 → 0,77 |
| CH | +11,4 | **+7,4** | 0,72 → **0,77** |
| ES | −20,9 | −21,5 | — |
| FR | −29,7 | −31,4 | — |
| DE_LU | −11,1 | −17,6 | négatifs 346 → **502** (obs 457) |
| IT_NORTH | −11,7 | **−15,6** | 0,61 → 0,61 |
| **\|moyenne\|** | 16,9 | **16,0** | |

**Le gain n'est pas dans la moyenne (quasi nulle) mais structurel** : les frontières alpines existent enfin
(#141), BE et CH gagnent en niveau *et* en corrélation, et DE-LU cale ses négatifs sur l'observé (le −17,6
est le prix d'un DE qui price *enfin* ses heures négatives). Coût assumé : **IT −4 pts**, dont moitié
physique (import réel de nucléaire slovène bon marché via IT↔SI) et moitié une sous-tarification IT
préexistante (−11,7 déjà sous DE_REST — cf. #142), qui survit au markup (~−13 %). **#138 n'est pas résolu**
(non régressé) : le verrou des négatifs FR/BE/ES est domestique, pas topologique.

**Valeur de l'eau des clusters sans prix.** Un cluster virtuel n'a ni prix observé (courbe empirique) ni
série de stock réservoir (SDP Bellman) : son hydro de lac serait offerte au plancher ~1 €/MWh et déferlerait
dans la zone modélisée la plus chère qu'elle borde. AT_SI (1,3 GW d'hydro alpine) **emprunte donc la courbe
révélée de CH** (même hydrologie ; `_WATER_VALUE_PROXY` dans `hydro/water_value.py`). Effet sur le backtest
2024 : neutre (l'import IT bon marché est dominé par le nucléaire SI, pas l'hydro), mais correct pour la
projection, où le prix propre d'AT_SI compte.

### 6d. Offre nucléaire FR en courbe de tranches (2026-07)

Le verrou identifié ci-dessus est levé par la **même méthode que l'hydraulique**, appliquée au nucléaire
(`stacks/nuclear_curve.py`, moteur commun dans `stacks/revealed.py`). Le parc nucléaire *a* une courbe
d'offre et on l'observe. Part de la capacité **disponible** (installé − indisponibilité REMIT) produite,
moyenne pondérée 2019/2022/2023/2024 :

| prix | <−1 | 0-5 | 5-10 | 10-20 | 20-30 | 30-40 | 40-60 | 60-80 | 80-120 | >120 |
|---|---|---|---|---|---|---|---|---|---|---|
| part | 0,738 | 0,825 | 0,851 | 0,871 | 0,891 | 0,912 | 0,920 | 0,952 | 0,979 | 1,026 |

L'amplitude de modulation croît avec la pénétration renouvelable — 0,16 en 2019, 0,27 en 2024 : c'est du
suivi de charge, pas du bruit. Le dénominateur est la capacité **disponible**, jamais l'installée : en
2022 la moitié du parc était à l'arrêt, et rapporter à l'installé lirait comme un refus de produire ce
qui n'était qu'une indisponibilité.

Trois lectures : un **socle inflexible** d'environ 74 % produit même à prix négatif (minimum technique,
xénon, fin de campagne — l'arrêt-redémarrage coûte plus cher que quelques heures à perte), offert *sous
zéro* et non en `min_gen_frac` dur ; une **bande modulable à coût d'opportunité croissant** (moduler
profond consomme de la marge de manœuvre pour la suite), d'où la pente ; et un dernier percentile qui
n'apparaît qu'en tension, écrêté à 100 % plutôt qu'extrapolé.

**Résultat mesuré** (|erreur baseload| moyenne, six zones) :

| variante | 2019 | 2024 |
|---|---|---|
| bloc unique à 7,0 €/MWh | 13,00 | 29,87 |
| courbe, prix = borne basse de classe | 6,47 | 26,35 |
| courbe, borne basse + coût combustible | 5,98 | **25,15** |
| courbe, borne basse planchée au combustible | 6,43 | 25,95 |
| **courbe, prix = moyenne observée de la classe** | **5,93** | 25,68 |

FR 2019 −19,6 → **−0,3 %**, BE −17,5 → **+0,2 %**, corrélation FR 0,782 → **0,841**. FR 2024 −51,2 →
**−34,9 %**, corrélation 0,643 → **0,761**. Dégénérescence FR 2024 (part des heures dans une classe de
1 €/MWh) : 67,8 % → **~20 %**. **Le markup redevient posable** (§7).

Trois points de méthode, dont deux vont contre le score :

- Le prix de tranche est la **moyenne observée de la classe**, pas sa borne basse : la capacité qui
  apparaît dans [80, 120) a un prix de réserve *dans* l'intervalle, pas à 80.
- La variante « + coût combustible » gagne 2024 de 0,5 point et est pourtant écartée. En 2024 le modèle
  est massivement biaisé vers le bas pour des causes étrangères au nucléaire (CH +37 %, DE −32 %,
  ES −28 %, inchangés dans *toutes* les variantes), donc tout ce qui remonte les prix gagne
  mécaniquement. 2019, quasi non biaisé, est le discriminant propre — et la moyenne de classe y gagne.
  Elle est de plus la seule variante sans constante d'ajustement.
- **La queue négative française n'apparaît toujours pas** : 0 heure modélisée contre 352 observées en
  2024, dans aucune variante, alors même que le socle offre à −40 €/MWh. J'avais prédit l'inverse ; la
  mesure me dément. Le verrou est le mécanisme d'export de S1c, pas le prix d'offre nucléaire.

Les 56 lignes unitaires nucléaires disparaissent au passage : elles portaient toutes le même coût et la
même disponibilité, donc n'apportaient aucune information au LP — seulement des colonnes.

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

### 7a. Le markup était mal posé ; il ne l'est plus (2026-07)

**Historique — pourquoi il était insoluble.** La distribution des SMC était **dégénérée** (69 % des heures
FR à 7,0 €/MWh) : aucune transformation, additive, multiplicative ou monotone, ne peut extraire une
distribution réaliste d'une masse ponctuelle. Le wedge par régression triplait alors l'erreur en 2019
(FR : MAE 8,3 → 22,9) et détruisait la métrique de valorisation (capture solaire FR 2024 à 1,10 contre
0,68 observé). La variante monotone (appariement de quantiles SMC→spot) sortait 6 864 heures négatives
contre 352 observées. Conclusion de l'époque : le markup ne pourra être ajusté qu'après avoir cassé la
dégénérescence.

**C'est fait (§6d, offre nucléaire différenciée), et le markup est devenu posable.** Ajusté sur 2019+2022,
il réduit maintenant l'erreur de prix en échantillon avec des R²_spot de 0,77 à 0,81 pour FR/BE/DE/ES — là
où la masse ponctuelle rendait la régression vide de sens. **C'est la validation de tout l'effort nucléaire
+ eau structurelle : casser la dégénérescence a rendu le wedge ajustable.**

**Mais le wedge plein surcorrige hors échantillon, donc il est rétréci (`shrink=0,5`).** Le wedge est
calibré sur une année normale et la crise 2022, d'où une moyenne ≈ +40 €/MWh. Or le SMC est maintenant si
bon (nucléaire + eau) que l'écart résiduel 2023/2024 est plus petit : le wedge plein le dépasse (ES 2024
−22 % → +31 %, MAE moyenne 26,3 → 31,2). **Le rétrécir de moitié** garde la MAE au niveau du SMC brut
(26,4 vs 26,3) tout en corrigeant le niveau — indispensable pour la projection, où le SMC brut est 20-33 %
trop bas et donc inutilisable :

| baseload hors éch. | SMC brut | wedge ×0,5 |
|---|---|---|
| FR 2023 | −16,2 % | **−0,1** |
| FR 2024 | −32,7 % | **−17,2** |
| ES 2023 | −20,2 % | **−1,5** |
| ES 2024 | −22,0 % | **+4,7** |

Le levier est le **rétrécissement**, pas une reparamétrisation « régime-consciente » : des formes
proportionnelles au SMC (les plus régime-conscientes) ont été testées et *dégradent* la MAE — la régression
ridge porte déjà le régime par ses variables SMC/tightness ; son seul défaut était d'être trop grosse. On
paie ce rétrécissement d'un fit en échantillon un peu moindre (R²_spot 0,81 → 0,77), compromis biais-variance
assumé. La variante monotone (`fit_markup_monotone`) reste dans le code mais **n'est pas utilisée** : sur le
SMC amélioré elle reste nettement pire (MAE 38,5).

**Reste ouvert : IT-North**, dont le wedge garde un R²_spot < 0 (−2,11) — problème propre à la
microstructure italienne, indépendant du rétrécissement, à traiter à part.

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
