# Provenance of the `dispatch_tyndp` anchors

Every figure written into the `dispatch_tyndp` workbook tab that did **not** come from the project's own
data lake is recorded here, with its source document, the exact table it was read from, and — where a
figure is a sum or a conversion rather than a direct quote — the arithmetic that produced it.

Written after the 2024 projection backcast measured what the missing anchors cost. A zone whose anchors
all start after the reference year has `_interp(2019)` clamped flat to the first anchor, so its factor
collapses to ~1.0 and the variable is **frozen at its 2019 level for every projected year** — visually
identical, in the output, to a deliberate "no change" scenario. Measured against observed 2024:

| zone | negatives obs → proj | mean error | cause |
|---|---|---|---|
| CH | 292 → 0 | +20.3 €/MWh | `cap_solar_gw`/`cap_wind_gw` clamped |
| NL | 458 → 0 | +16.4 €/MWh | no RES/demand/flex rows at all → flat CAGR |
| IT_NORTH | 0 → 0 | −4.8 €/MWh | `cap_solar_gw`/`cap_wind_gw`/`cap_gas_gw`/`cap_coal_gw` clamped |

`tyndp.report_coverage()` now prints these gaps on every projection run, so the class of defect cannot
recur silently.

---

## Conventions

* **Capacity is nameplate at 31 December of the stated year**, matching how `tyndp_factors` uses it
  (a ratio against the reference-year stack, which is itself an end-of-year nameplate figure).
* `cap_wind_gw` is **onshore + offshore combined**, matching `_RES_VARS` in `tyndp.py`.
* Solar capacity is quoted DC (kWp) by every statistical office below. The reference-year stack is also
  built from DC nameplate, so the RATIO is unaffected — but do not mix these figures with AC ratings.
* A figure marked **[direct]** is quoted verbatim from the source. **[sum]** is an arithmetic total of
  quoted components, shown below it. **[derived]** applies a stated conversion; the inputs are quoted.

---

## CH — Switzerland

### `cap_solar_gw`, 2019 = **2.498** [direct]

> Bundesamt für Energie (BFE) / Swissolar, *Markterhebung Sonnenenergie 2019*, §4.3 "Gesamthaft
> installierte Leistung in kW per Ende Jahr", row "Photovoltaik Total", column 2019:
> **2'498'050 kWp** (of which 2'492'010 kWp grid-connected, row "davon im Netzverbund").

Survey conducted by Swissolar **on behalf of the BFE** ("Die Erhebung wurde im Auftrag des Bundesamtes
für Energie durchgeführt", §1). It feeds the Schweizerische Gesamtenergiestatistik.

<https://pubdb.bfe.admin.ch/de/publication/download/10140>

**Why this could not be measured in-house:** ENTSO-E's Swiss installed-capacity submission contains only
Hydro (Pumped Storage / Run-of-river / Reservoir) and Nuclear — there is no Solar or Wind row for any
year, in any of 2019–2026. The generation-based proxy is invalid here: p99.9 of CH solar generation in
2019 is **297 MW** against a ~2.5 GW fleet, because Swiss PV is overwhelmingly behind the meter and
absent from the metered feed. Using the proxy would have understated the fleet ~8× and inverted the
error rather than corrected it.

### `cap_wind_gw`, 2019 = **0.075** [direct, weaker source]

Swiss wind at end-2019 was ~75 MW across ~40 turbines. **This is the one figure below not carried by a
primary government table**: the BFE's wind page gives only "almost 40 large wind energy facilities …
around 140 GWh" and no MW total, and the *Schweizerische Statistik der erneuerbaren Energien* advance
extract reports wind in TJ of production (614 TJ in 2024), not installed MW.

Impact of the residual uncertainty is small: wind is 2.9 % of CH's 2019 wind+solar total, so a ±20 %
error here moves the `res` factor by under 0.6 %. **Replace with the full BFE renewables statistics
(Anhang B, published September 2025) or `opendata.swiss` "Windenergieanlagen" aggregated by
commissioning year** when convenient.

<https://www.bfe.admin.ch/bfe/en/home/supply/renewable-energy/wind-energy.html>

### Consequence

`res` factor 2024/2019 goes from a clamped **1.0** to **(interp 2024) / (2.498 + 0.075)**. Note this
also exposes a question for you: the tab's CH `cap_solar_gw` 2025 anchor is 5.0 GW, but Swiss PV passed
that before 2024 on the BFE's own series — **the CH solar trajectory looks stale and understated**.

---

## IT_NORTH — Italian "Nord" bidding zone

The Nord market zone comprises Valle d'Aosta, Piemonte, Liguria, Lombardia, Trentino-Alto Adige, Veneto,
Friuli-Venezia Giulia and Emilia-Romagna. Both figures are therefore regional sums, not national totals.

### `cap_solar_gw`, 2019 = **9.2625** [sum]

> GSE, *Solare Fotovoltaico — Rapporto Statistico 2019*, p.21, table "Numerosità e potenza per provincia
> degli impianti fotovoltaici nel 2018 e 2019", column "2019 / MW", regional subtotal rows:

| region | MW |
|---|---|
| Piemonte | 1 642.5 |
| Valle d'Aosta | 24.6 |
| Lombardia | 2 398.8 |
| Trentino-Alto Adige | 442.7 |
| Veneto | 1 995.8 |
| Friuli Venezia Giulia | 545.2 |
| Liguria | 112.8 |
| Emilia-Romagna | 2 100.1 |
| **sum** | **9 262.5 MW** |

National total for context: 20 865 MW over 880 090 plants at 31/12/2019.

<https://www.gse.it/documenti_site/Documenti%20GSE/Rapporti%20statistici/Solare%20Fotovoltaico%20-%20Rapporto%20Statistico%202019.pdf>

### `cap_wind_gw`, 2019 = **0.129** [derived]

> GSE, *Rapporto Statistico FER 2019*, §3.3.6 "Distribuzione regionale della potenza installata degli
> impianti eolici a fine 2019", p.59. National total stated on the map: **10 715 MW**. Regional shares
> for the Nord zone: Piemonte 0.2 %, Liguria 0.5 %, Emilia-Romagna 0.4 %, Veneto 0.1 %. Lombardia,
> Valle d'Aosta, Trentino-Alto Adige and Friuli-Venezia Giulia are blank (no installed wind).

0.2 + 0.5 + 0.4 + 0.1 = **1.2 %** × 10 715 MW = **128.6 MW**.

Corroborated by the report's own text on the same page: the northern **and** central regions together
hold "solo il 3,4 % della potenza complessiva nazionale". Both GSE tables are "elaborazioni GSE su dati
Terna" and sit in the Programma Statistico Nazionale (statistical work TER-00001, owned by Terna).

<https://www.gse.it/documenti_site/Documenti%20GSE/Rapporti%20statistici/Rapporto%20Statistico%20GSE%20-%20FER%202019.pdf>

### ⚠ This baseline exposes a problem in the existing IT_NORTH trajectory

With wind 2019 = 0.129 GW, the tab's `cap_wind_gw` 2025 anchor of **3.0 GW implies 23× growth in six
years** in a bidding zone that had essentially no wind and no significant pipeline. Italian wind is
concentrated in the South and Islands (Puglia 24.0 %, Sicilia 17.7 %, Campania 16.2 %). The 3.0 GW
figure looks like a **national** number mistakenly entered against the Nord zone. Left unchanged here —
it is a scenario value and yours to set — but it should be checked before any long-horizon run.

---

## NL — Netherlands

NL is a different case from CH/IT: its 2019 data is **fully measured and already in the lake**
(ENTSO-E installed capacity: Solar 7 226 MW, Wind Offshore 957 MW, Wind Onshore 3 527 MW), and
cross-validates against CBS. What is missing is the *forward* trajectory — the tab has no NL rows at all
except `cap_nuclear_gw` — so no baseline can help: the factor is `None` regardless and the projection
silently falls back to a flat +4.5 %/yr CAGR.

### 2019 cross-validation [direct]

> CBS (Centraal Bureau voor de Statistiek), *Hernieuwbare energie in Nederland 2019*: solar **6 870 MW**
> cumulative at end-2019 (2 350 MW added in the year); wind **4.5 GW** total.

Lake (ENTSO-E) gives 7 226 MW solar and 4 484 MW wind. The solar gap is the usual DC/AC and
registration-date difference; the two agree to ~5 %, so **the measured lake figure is sound and
`gen_tyndp_baseline.py` will fill the 2019 row automatically once future anchors exist.**

<https://longreads.cbs.nl/hernieuwbare-energie-in-nederland-2019/zonne-energie/>

### Forward anchors — SOURCED COMPONENTS, NOT YET WRITTEN

These are the official figures found; they are recorded here rather than written into the tab because
each requires a judgement that is yours (see "Open decisions" below).

**Offshore wind** — Nationaal Plan Energiesysteem (Ministerie van Klimaat en Groene Groei / EZK,
1 December 2023), cabinet targets [direct]:

| year | GW offshore |
|---|---|
| 2031 | 21 |
| 2040 | 50 |
| 2050 | 70 |

<https://www.rijksoverheid.nl/documenten/2023/12/01/nationaal-plan-energiesysteem>

**Offshore wind, nearer term** — PBL, *Klimaat- en Energieverkenning 2025*: "almost 5 GW" on the North
Sea now, **10 GW expected in 2030** on the basispad.

**Onshore wind + solar** — Klimaatakkoord / RES target of **35 TWh** of large-scale onshore solar and
wind by 2030; KEV 2025 projects 34–44 TWh, calling the 35 TWh goal very likely met.

**Electricity demand** — PBL, *Klimaat- en Energieverkenning 2025*, Bijlage 2, Tabel 24
"Elektriciteitsbalans in petajoule", row "Totaal verbruik" [direct, PJ → TWh at 1 PJ = 0.277778 TWh]:

| year | PJ | TWh |
|---|---|---|
| 2020 | 430 | 119.4 |
| 2023 | 416 | 115.6 |
| 2024 | 425 | 118.1 |
| 2030 | 548 (band 509–574) | **152.2** (141.4–159.4) |

Same table, 2030 generation split: wind 237 PJ (65.8 TWh), solar 95.4 PJ (26.5 TWh).

<https://www.pbl.nl/system/files/document/2025-09/pbl-2025-klimaat-en-energieverkenning-2025-5692.pdf>

**Electricity demand, 2050** — NPE indicative **direct** electricity demand ≈ **273 TWh** for 2050.
Note this is a different accounting basis from the KEV's "totaal verbruik" and the two should not be
interpolated between without checking the definitions match.

### Open decisions before NL is written into the tab

1. **`cap_solar_gw` has no published GW trajectory** in either source. KEV 2025 gives solar *generation*
   (95.4 PJ = 26.5 TWh in 2030), not capacity. Converting needs a yield assumption — which we can
   measure from our own lake (2019 NL solar generation ÷ 7 226 MW) rather than assume, but that is a
   derivation and I will not write a derived capacity trajectory into a scenario tab without your
   sign-off on the method.
2. **`cap_wind_gw` mixes bases.** The 21 / 50 / 70 GW figures are offshore-only cabinet *targets*; the
   KEV 10 GW-by-2030 is a *projection* of what policy will actually deliver. Targets and projections
   should not be mixed within one trajectory, and onshore must be added on a consistent basis.
3. **`demand_twh` basis.** KEV "totaal verbruik" (152.2 TWh in 2030) vs NPE "direct electricity demand"
   (273 TWh in 2050) are not the same quantity.
4. **`cap_flex_gw` has no source at all** in these documents — and it is read as an *absolute* level, so
   an omission means NL simply gets no adequacy block.

### The same gap applies to DK, PL_CZ and AT_SI

The coverage report shows `cap_flex_gw`, `cap_solar_gw`, `cap_wind_gw` and `demand_twh` all **missing**
for DK, PL_CZ and AT_SI as well. They were not researched here because they were not in scope, but they
are on the identical flat-CAGR fallback as NL and will carry the same bias.

---

## `cap_hydro_gw` — the definition, and why it still stays clamped

### The definition

**`cap_hydro_gw` means reservoir + run-of-river, EXCLUDING pumped storage.**

This is not a preference. `tyndp._CAP_VAR` already encodes it — `hydro_reservoir` and `hydro_ror` map to
`cap_hydro_gw` while `hydro_psp` maps to its own `cap_psp_gw` — and the dispatch forces it: PSP is not in
the hydro merit order at all. `flexibility.storage` sizes the storage LP from the zone's measured
`hydro_psp` stack capacity and dispatches it as storage. A PSP-inclusive factor would scale a stack that
does not contain the PSP it is counting.

### The sheet does not follow it, and not uniformly

Measured against 2019 ENTSO-E installed capacity:

| zone | res+ror | +psp | 2025 anchor | entered on |
|---|---|---|---|---|
| FR | 19.23 | 24.26 | 26.0 | **PSP-inclusive** — deviates |
| CH | 6.05 | 12.69 | 15.0 | **PSP-inclusive** — deviates |
| DE_LU | 5.32 | 14.74 | 5.0 | res+ror — correct |
| ES | 20.30 | 25.95 | 20.0 | res+ror — correct |
| IT_NORTH | 0.00 | 0.00 | 13.0 | undeterminable — no capacity rows at all |

That split is why the first attempt was abandoned: it applied one tech set to every zone, so it was right
for half and wrong for half — PSP-inclusive gave DE 0.34, PSP-exclusive gave FR 1.41.

### Why the mixed definition is nevertheless not the blocker

`tyndp_factors` consumes a **ratio within a zone**, so a constant definitional offset cancels, provided
the reference-year baseline is measured on the same basis as that zone's own anchors.
`gen_tyndp_baseline.py` now detects that basis per zone (`hydro_basis()`), which resolves the
inconsistency without editing a single scenario value.

### RESOLVED — the sheet is now on one definition

`scripts/fix_tyndp_hydro_basis.py` restated the two deviating zones onto res+ror and moved their pumped
storage into `cap_psp_gw`. PSP levels are measured, with the projected-year level chosen by the workbook
owner:

| zone | PSP 2019 (measured) | PSP for projected years | why |
|---|---|---|---|
| FR | 5.023 GW | **5.050 GW** (2022) | France builds no new PSP; measured flat 2019–2026 |
| CH | 6.641 GW | **6.681 GW** (2022) | Swiss PSP is *not* static — Nant de Drance added ~0.9 GW in 2022, so the 2019 figure would understate every projected year |

Anchors after restatement:

| zone | `cap_hydro_gw` was | now (res+ror) | `cap_psp_gw` added |
|---|---|---|---|
| FR | 26 / 26 / 27 / 27 | 20.95 / 20.95 / 21.95 / 21.95 | 5.05 |
| CH | 15 / 15 / 16 / 16 | 8.319 / 8.319 / 9.319 / 9.319 | 6.681 |

All four zones with capacity data now detect **res+ror**, and `gen_tyndp_baseline.py` writes their 2019
baselines. `cap_hydro_gw` is unclamped for FR, DE_LU, CH and ES.

### Accepted error source: the anchor-vs-measured level gap

The scenario anchors and ENTSO-E's measured stock disagree on the *level*, and the ratio carries that
disagreement into the first projected years as apparent growth:

| zone | measured 2019 | first anchor | span 2025→2050 | **level gap** | hydro factor 2024/2019 |
|---|---|---|---|---|---|
| FR | 19.234 | 20.95 | +4.8 % | **+8.9 %** | 1.074 |
| CH | 6.053 | 8.319 | +12.0 % | **+37.4 %** | 1.312 |
| DE_LU | 5.317 | 5 | 0.0 % | **−6.0 %** | 0.950 |
| ES | 20.302 | 20 | 0.0 % | **−1.5 %** | 0.988 |

The gap exceeds the trajectory in every zone, so a material part of each factor is level mismatch rather
than build-out. **This was put to the workbook owner and accepted explicitly**, so the baselines are
written and the gap is printed on every run of `gen_tyndp_baseline.py` rather than silently swallowed.

CH is the worst case by far (+37.4 %), and the cause is on the measurement side, not the scenario side:
ENTSO-E reports only 6.05 GW of Swiss reservoir + run-of-river, which badly under-counts Swiss small
hydro. The residual fix is therefore to reconcile the anchor levels with a better capacity source, not to
change the anchors.

### IT_NORTH remains clamped, correctly

No installed-capacity rows exist for IT_NORTH at all, so its 13 GW anchor cannot be checked against
anything and `hydro_basis()` refuses to guess. Its anchors are perfectly flat, so the clamped factor of
1.0 is exactly right regardless.

---

# Provenance of the `dispatch_ntc_newbuild` anchors

A separate tab, and a separate *shape*, from everything above. `dispatch_tyndp` holds smooth quantities
that `_interp` interpolates between anchor years; an interconnector is a discrete asset that commissions
on a date, adds a fixed MW and is flat either side — the same shape as a reactor in
`dispatch_nuclear_newbuild`. Interpolating it would invent capacity in every year nothing was built and
smear one cable's rating across a decade, so the tab holds **one row per project** and
`tyndp.ntc_delta_mw` sums them as a **step**, never interpolating.

The tab records a **delta, not a level**. The projection starts from the reference year's hourly NTC
series (`assemble.hourly_ntc`), which already embodies the grid as it then stood; what it needs is only
what was commissioned since. That also sidesteps a baseline that **does not exist**: ENTSO-E publishes no
current or starting grid per boundary and direction — ACER's opinion on the draft TYNDP 2024 explicitly
*asks* them to begin publishing exactly that. Any ratio formulation would have had to invent it.

Capacity applies **symmetrically** to both directions: new links are overwhelmingly HVDC with a single
rating, and the asymmetry in `assemble.NTC` comes from network constraints around the border, not the wire.

## Tier `built` — commissioned since the 2019 reference year

These are not forecasts, and omitting them was a real error: the projection's reference year is 2019, so
without them the 20-year crossover carried GB to 2045 on a 1580 MW link to France and no Viking at all.

Every row is **validated in-house** against the step in maximum hourly flow across the border
(`entsoe_flows`, MW forward/backward). `entsoe_flows` holds no 2020/2021 rows on these borders, so a
commissioning year falling between two measured years is bracketed rather than pinned — but the **MW step
is unambiguous in every case**, which is what the model consumes.

| border | project | MW | year | measured step (max hourly flow) |
|---|---|---|---|---|
| DE_LU–BE | ALEGrO | 1000 | 2020 | 249/312 (2019) → **1164/1298** (2022) |
| FR–GB | IFA2 | 1000 | 2021 | 2036 (2019) → **3071** (2022) |
| FR–GB | ElecLink | 1000 | 2022 | 3071 (2022) → **4087** (2023) |
| FR–IT_NORTH | Savoie-Piémont | 1200 | 2023 | FR→IT 3563 (2019) → **4554** (2024) |
| DK–GB | Viking Link | 1400 | 2023 | 0/0 (2022) → **1408/1454** (2023) |

Savoie-Piémont's date is RTE's: half capacity from November 2022, full capacity from August 2023, two
600 MW HVDC bipoles. <https://www.rte-france.com/en/projects/savoie-piemont-190-km-of-european-solidarity-from-chambery-to-turin>

**A trap avoided.** NL–GB reads 0 in `entsoe_flows` until 2024 and then 1091/1061. That is **not** a
commissioning step — BritNed has run since 2011; ENTSO-E simply did not publish the Dutch side of the
border before 2024. Encoding it as a 2024 newbuild would have invented a gigawatt. The same publication
gap affects BE–NL, CH–AT_SI, IT_NORTH–AT_SI and IT_NORTH–IT_SOUTH, all of which read 0/0 in 2019.

## Tier `reference` — committed / under construction

### FR–ES, Bay of Biscay (Golfe de Gascogne) = **2200 MW**, 2028 [direct]

INELFE (RTE 50 % / Red Eléctrica 50 %); two converter stations, Gatika near Bilbao and Cubnezais near
Bordeaux, joined by ~400 km of underground and submarine cable. Testing from mid-2027, commissioning 2028.
Backed by a €1.6 bn EIB facility signed 2025.

Red Eléctrica and INELFE both state the link **doubles** France–Spain exchange capacity **to 5000 MW**.
That is independently corroborated by the TYNDP 2024 reference grid, whose `ES00-FR00` row reads
**5000/5000** — i.e. today's 2800 plus this project's 2200. Two unrelated sources agreeing on the
post-project level is the strongest check available for a project not yet built.

<https://www.inelfe.eu/en/projects/bay-biscay> ·
<https://www.eib.org/en/press/all/2025-241-eib-supports-with-eur1-6-bn-the-strategic-bay-of-biscay-electricity-interconnection-between-spain-and-france>

## Tier `tyndp_candidate` — assessed, NOT committed. Off by default.

36 rows, 68.6 GW, summed per border and horizon from ENTSO-E's own file: *20231103 – Electricity and
Hydrogen Reference Grid & Investment Candidates.xlsx*, sheet **"3. Elec Invest Candidates"**, column
**"DIRECT CAPACITY INCREASE (MW)"**, over the 80 rows (of 257) whose FROM/TO nodes both map into a
modelled zone. `SCENARIO` is `All` on every row. Horizons 2030 / 2035 / 2040.

<https://2024-data.entsos-tyndp-scenarios.eu/files/scenarios-inputs/20231103-Electricity-and-Hydrogen-Reference-Grid-Investment-Candidates.xlsx.zip>

These are the projects the TYNDP cost-benefit analysis exists to **decide on**, so enabling them asserts
that every assessed project gets built. `tyndp.NTC_DEFAULT_SCENARIOS` therefore excludes them; passing
them is a deliberate high-build scenario, not a default.

They also sit **on top of** the 2030 reference grid, which already contains the committed projects above —
so the two tiers add rather than double-count.

### Candidates with nowhere to go

Four candidate rows fall on boundaries the model carries no border for, and are reported by
`gen_ntc_newbuild.py` on every run rather than silently dropped:

| boundary | MW | year | why dropped |
|---|---|---|---|
| AT_SI–PL_CZ | 1000 / 500 | 2030 / 2040 | model has no AT–CZ border |
| DE_LU–GB | 1400 | 2030 | NeuConnect; model has no DE–GB border |
| DK–NL | 2000 | 2040 | model has no DK–NL border, though COBRA already flows there |

The DK–NL entry is the notable one: `entsoe_flows` already carries `DK_1>NL` / `NL>DK_1` (COBRA, 700 MW,
2019), so this is a border the model could add today from existing data.

## Node mapping

TYNDP bidding-zone codes → model zones, per `scripts/gen_ntc_newbuild.py` and `scratchpad/tyndp_ntc_map.py`.
`ITN1` is Italy-North; `ITCN`/`ITCS`/`ITS1`/`ITCA`/`ITSA`/`ITSI`/`ITCO` fold into IT_SOUTH; `AT00`+`SI00`
into AT_SI; `PL00`/`PL00E`/`PL00I`+`CZ00` into PL_CZ; `DKW1`+`DKE1` into DK. **Luxembourg matters**: the
model folds LU into DE_LU, so `BE00-LUB1`, `BE00-LUG1` and `FR00-LUF1` are legs of BE–DE_LU and FR–DE_LU,
while `DE00-LUG1` and `DE00-LUV1` are internal to DE_LU and must be dropped or they invent capacity.

The direction convention — "Summary Direction 1" = first code → second code — is **confirmed, not
assumed**: four model entries reproduce the TYNDP pair exactly and in the right order (FR–BE 4300/2800 vs
`BE00-FR00` 2800/4300; ES–PT 4200/3500; BE–GB 1000/1000; CH–AT_SI 1200/1200).

---

# GB — the one zone whose rows are not TYNDP

GB left ENTSO-E after Brexit and TYNDP publishes no per-country UK capacity trajectory: the 2024 scenario
figures are EU27 aggregates plus low/high ranges at 2040/2050 only. (TYNDP does still model `UK00` in its
reference GRID, which is where this repo's GB border capacities come from — but the grid and the scenario
data are different publications.) So GB uses its own system operator's scenarios, exactly as CH's solar
came from BFE/Swissolar rather than from ENTSO-E.

**Source:** NESO *Future Energy Scenarios 2024*, Data Workbook — sheets **ES.14** (solar capacity),
**ES.13** (onshore wind), **ES.12** (offshore wind), **DB.ED1** (electricity demand), scenario **Holistic
Transition**, NESO's central pathway and the closest analogue to the TYNDP National Trends+ basis the other
zones use. <https://www.neso.energy/publications/future-energy-scenarios-fes/fes-documents>

Written by `scripts/gen_tyndp_gb.py`, which carries the numbers and the arithmetic.

### The 2019 anchor is measured in-house, not taken from FES [measured]

FES's capacity series begin at **2023**, and `tyndp_factors` divides by the projection's **2019** reference
year — an anchor starting after the reference year clamps, which `tyndp.coverage` flags as the dangerous
class. GB's 2019 installed capacity is already in the lake, however: ENTSO-E published it *before* Brexit,
in the same `entsoe_installed_capacity` table, on the same nameplate basis every other zone's anchor uses.

    entsoe_installed_capacity, series_key GB, year 2019
        Solar          13 346 MW   -> cap_solar_gw 13.346
        Wind Onshore   12 638 MW   \
        Wind Offshore   9 379 MW   /  -> cap_wind_gw 22.017   (onshore + offshore, per tyndp._RES_VARS)

**Continuity check:** solar 13.35 (2019, ENTSO-E) -> 15.14 (2023, FES); wind 22.02 -> 28.41. Both are
consistent with four years of build-out, so the two sources join without a step.

### `demand_twh` is deliberately left CLAMPED

FES's demand series starts at 2022 and measures **national** demand (343.4 TWh in 2022). The lake's GB load
is Elexon **ITSDO** — demand at the *transmission* boundary, ~249 TWh — a different quantity by ~95 TWh of
embedded generation and losses (see `io/gb_embedded.py`). Splicing them would inject a 38 % step into a
ratio meant to measure structural growth. FES 2022 is therefore the earliest anchor and `_interp(2019)`
clamps to it. The clamp misprices the small term: GB demand moved a few per cent 2019-2022, while RES
roughly doubled and is properly anchored.

### What filling these rows did, and a correction

It made GB's backcast **worse** — layer -10.7 -> -22.0, projection 60.2 -> 48.9 EUR/MWh against 84.5
observed — and it improved or held the other **eight** zones (FR 3.4 -> -0.5, BE 7.4 -> 5.9, ES 1.1 -> 0.5,
PT 1.8 -> 1.2; pooled excluding GB 5.84 -> 5.31).

**A prior explanation recorded in `scripts/projection_backcast.py` and in commit `b532a48` was WRONG** and
is corrected here: GB's layer was attributed to the generic CAGR *under*-growing its RES. The CAGR grew RES
x1.25 to 2024 where FES grows x1.45 — the fallback grew RES **less** — and the backtest arm already runs
actual 2024 RES (~x1.4), more than the CAGR, while remaining the *dearer* arm. RES growth is therefore not
the mechanism and GB's layer is **unexplained**. The rows were kept regardless: eight zones improved, and a
sourced national scenario beats a generic 4.5 %/yr CAGR on a system whose build-out is nothing like that.

### Still missing for GB

`cap_flex_gw` and every thermal `cap_*` row remain absent, so GB's firm fleet is still CAGR-driven. FES
carries battery, gas and nuclear capacity for the same scenario; that is the next gap to fill.
