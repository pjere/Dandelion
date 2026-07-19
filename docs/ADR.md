# Architecture Decision Records (§8)

## ADR-1 — Lighter-touch shared library, not a full monorepo
**Context:** 6 sibling packages, solo/personal project. **Decision:** extract duplicated cross-cutting code
into an importable `powersim_core/` package rather than relocating everything into a `powersim/` monorepo
with uv workspaces + heavy multi-writer CI. **Consequence:** far less migration risk; the packages depend
on `powersim_core`; if the team grows, promoting to a monorepo is a later, mechanical step. Revisit if
concurrency rises.

## ADR-2 — RNG: SeedSequence.spawn keyed by draw_id (resolves F4)
**Context:** ad-hoc `default_rng(seed + draw*K + salt)` scattered across models; a global `np.random.seed`
in weathergen silently shares state under multiprocessing. **Decision:** one authority in
`powersim_core.rng` — `draw_rng(master_seed, draw_id)` via `SeedSequence(entropy, spawn_key=(draw_id,))`,
`substream(...)` for named per-component streams. **Consequence:** independent, collision-free streams
across 50+ parallel draws; reproducible from (master_seed, draw_id).

**Status: ADOPTED (DONE).** Converted: availability ×4 (`common_mode`/`forced`/`interconnectors`/`planned`),
dispatch commodity-OU + its additive `Config.rng` helper; res/demand residual `simulate()` take an `rng=`
from the authority, threaded from the engines (`seed=` kept as a legacy fallback). **Correction to the
original context:** weathergen's *package* code was already `Generator`-based with an explicit threaded
`rng` — the global `np.random.seed` lived only in a diagnostics script + tests, so **no cube re-baseline was
needed** and `simulation.nc` is untouched. The re-baseline was limited to what actually re-draws:
availability's 3 stochastic outputs (mean −0.09 % … −0.74 %) and res `production`; the deterministic
artifacts (availability `reservoir_budget`, all demand/dispatch goldens, res caches) were unchanged.
Acceptance was *still-valid*, not identical — nuclear Kd 0.755 [0.748, 0.762] stays inside the 0.72–0.80
historical band and every validation suite passes.

## ADR-3 — Leap-day policy: 8760 h model grid, Feb-29 dropped (resolves F5)
**Context:** the frozen weather cube is 20×8760 h (no Feb-29); real-calendar joins misalign by a day after
each leap year. **Decision:** the canonical model grid is 8760 h/year with Feb-29 dropped
(`powersim_core.time_grid`, `DROP_FEB29=True`); real-calendar sources drop Feb-29 when curated. Raw extracts
keep native calendars. **Consequence:** consistent joins without regenerating the cube (out of scope §9,
and would invalidate the golden baseline). Flip `DROP_FEB29` + re-baseline only if the cube is ever rebuilt
to a 8784-on-leap grid.

## ADR-4 — Storage: partitioned Parquet lake + DuckDB catalog (DONE, §6)
**Context:** many parallel draw-writers; want "single DB" semantics without serialising writes. **Decision:**
Parquet is the storage of record under `data/lake/{layer}/{dataset}/[key=value/…]/part.parquet` (zstd),
written/read only through `powersim_core.lake` (`write_table`/`read_table`; row order preserved, `index=`
passthrough, optional pandera `schema=` validate-on-write, `POWERSIM_LAKE`-overridable root for test
isolation). A single `data/powersim.duckdb` (`powersim_core.catalog`) holds one **view per dataset** over
the partition globs (Hive partition columns projected out), a `_catalog` summary and an append-only `runs`
ledger. The pandera contract vocabulary + registry live in `powersim_core.schemas` (one `validate`, shared
column builders). Models query the catalog, never each other's paths. No pickle (ADR-6), no CSV except
human exports. **Status:** DONE — all model outputs + caches migrated (availability×4, demand×3, res×4,
dispatch×1); golden repointed to the lake and **numerically identical**; the availability projection was
re-run through the new writer to prove faithfulness. *Sorting storage by `timestamp_utc` is deferred to a
future re-baseline (the fingerprint vhash is row-order-sensitive, so enforcing it now would trip golden).*

## ADR-5 — Scenario workbook: one hand-edited `scenarios.xlsx` + accessor + snapshot (DONE, §7)
**Context:** three per-model workbooks (`assumptions_*.xlsx`) with bespoke `pd.read_excel` calls scattered
across 7 sites; the goal is a **single file to edit** without changing any numbers. The literal ADR-5
"one tidy-long table" was a poor fit — much of the data is structural reference (169-unit fleet registry,
offshore-farm coordinates, hourly profiles) that isn't a `(variable, value)` pair. **Decision:** one
`scenarios.xlsx` at the repo root, tabs **prefixed by model** (`avail_fleet_registry`, `demand_macro`,
`res_capacity_trajectories`, `dispatch_commodities`, …) so names stay unique and ownership is obvious; a
"scenario" remains a value in the `scenario` column, selected via config/CLI (not a file). `powersim_core.scenario`
is the single read path — `load_model_sheets(path, "avail")` returns a model's tabs with the prefix
stripped, so each loader sees its historical sheet names unchanged; it also tolerates a legacy unprefixed
single-model workbook. `snapshot()` freezes the workbook to immutable Parquet + a manifest (source sha256 +
timestamp) for provenance — invisible to the editor. `dispatch_commodities` is now a real tab (seeded from
the former code defaults, so numbers are unchanged but henceforth editable). **Status:** DONE — 3 workbooks
merged → 28 tabs; all 7 read sites + 4 configs repointed; per-model `init-workbook` redirected to a
`_template.xlsx` so it can't clobber the live file. Loaders proven to read byte-identically; availability
re-run through the merged workbook → **golden identical**. *(The 3 old `assumptions_*.xlsx` are now inert —
safe for the user to delete once comfortable.)*

## ADR-7 — Asset master data: a `reference` layer in the lake, split from scenario overrides (in progress)
**Context:** static (non-time-series) asset data is scattered across **three** stores and one hack: the FR
fleet (168 units) lives in the **workbook** (`avail_fleet_registry`), ENTSO-E installed capacity in
**SQLite**, dispatch's `fr_fleet` is a **p99.9-of-generation quantile hack**, and the incoming plant
registries (MaStR/ODRÉ/…) would make a fourth. There is no single ID space and no provenance. This is a
real hygiene problem *and* it blocks modelling: DE's stack is 3 synthetic efficiency sub-blocks spread by
`np.linspace` over an **assumed** range, and `must_run_frac` is a literature guess (lignite 0.45 / coal
0.35 / gas 0.15) that measurably cost ~12 points of price level across every coupled zone.

**Decision:** add a **`reference` layer to the existing Parquet lake** — `data/lake/reference/plant_registry/`
— exposed as a DuckDB catalog view (ADR-4). **Not a new database**: a fourth store is the thing to avoid.
Vendor dumps (the `open-mastr` SQLite under `~/.open-MaStR/`, ODRÉ CSVs, …) stay as **raw landing zones**,
exactly the role `data/raw/rte` plays for the RTE extract; the canonical registry is ETL'd out of them.

Canonical schema (one row per physical unit, one stable ID space):
`plant_id, source, source_id, as_of, zone, tech, fuel, capacity_mw, commissioning_date, retirement_date,
chp_flag, efficiency_est, scheme, aw_eur_mwh, support_end, lat, lon`.

**The load-bearing split — registry ≠ scenario:**
- **registry (lake `reference`)** = *observed truth*, immutable, re-derivable from ETL, provenance-stamped.
- **workbook** = *scenario deltas* on top (closures, new build, overrides) — the file the user edits.
- models read **`registry ⊕ overrides`**.

Collapsing the two would either destroy the single-file editing workflow (ADR-5) or corrupt source truth on
every scenario tweak. Keeping them separate preserves both.

**Consequences.** DE gets a **unit-level** merit order (real capacities/vintages) instead of an invented
efficiency ladder, and — the strongest single argument — MaStR registers **KWK/CHP status per unit**, so
must-run becomes **measured rather than guessed**. Per-unit legislated coal-phase-out dates become
representable, which aggregated blocks cannot do.

**Honest limits (do not oversell):** (1) this will *not* fix the headline price gap — DE's P50 error is
already −1.1 and the damage is at P95 (−13.6), which is scarcity/markup and belongs to step (vii); the CHP
route is what plausibly moves the *level*. (2) Registry quality is **asymmetric**: MaStR is exceptional,
ODRÉ good (but <36 kW aggregate-only), BE/ES/IT patchy — so DE goes unit-level, FR near it, the rest stay
block-based with better parameters. Uniformity is not achievable and should not be promised. (3) MaStR has
**no efficiency field**; vintage+tech+size → efficiency is still a model, merely a better one than a linspace.
**Status:** decided; ETL + layer being built.

## ADR-6 — Fitted-model persistence: JSON + npz sidecar, never pickle (DONE, F6)
**Context:** every fitted model persisted itself with `pickle` (8 files across the four models). Pickle is
non-portable (silently breaks across numpy/pandas/library versions) and unsafe (loading executes arbitrary
code) — unacceptable for artifacts we reload across environments and time. **Decision:** `powersim_core.serialize`
is the single persistence authority. Two entry points: `save_params`/`load_params` for flat param bags
(nested dicts/lists/scalars, arrays → an `.npz` sidecar keyed by a `{"__ndarray__": …}` tag); and
`save_dataclass`/`load_dataclass` for composite fitted models (weathergen's `FittedModel`), which walk a
possibly-nested dataclass tree, tag each node with its fully-qualified type, push arrays to the sidecar and
Series/DataFrames to a tagged encoding, and rebuild via the class constructor with keyword fields — **no
arbitrary-code execution on load** (only named-class instantiation). Model layers own the type quirks JSON
can't carry: **tuple keys** (res `pv_bias` `(month,hour)` → `"m,h"`) and **int keys** (availability
`seasonality`/`seasonal_profile_week`, res-hydro `monthly_clim`/`feat_clim`) are stringified on save and
restored on load, because the projection indexes those dicts by the original key type (a silent str-key miss
would have quietly changed results). **Verification:** each of the 8 models was migrated by loading the legacy
pickle, re-saving via the new path, reloading, and asserting a **field-by-field deep-equal** (arrays via
`array_equal`, Series/DataFrame via `.equals`) — all 8 round-trip identical; golden outputs unchanged.
**Status:** DONE — no pickle remains in any package; `.pkl` files deleted, `.json`(+`.npz`) are the store.

## ADR-8 — Cross-package coupling: read-paths via the owning package's API, editable installs (Phase-2 review)
**Context:** the pre-vii principle was "packages coupled only through the SQLite DB and the weathergen cube".
Step vii (#77/#78/#80) introduced three deliberate exceptions: `dispatch_model.weather_shapes` runs the FR
demand/RES models (`demand_model`, `res_model`) for weather-coherent projection shapes, and
`dispatch_model.{rolling.backtest, neighbour_availability}` read REMIT availability through
`pricemodeling.entsoe.unavailability`. Duplicating that logic (SQL + reconstruction + model runs) inside
dispatch would be worse than importing it. **Decision:** cross-package *read paths* are allowed when they go
through the owning package's public API, are lazily imported at the call site (the base dispatch path must not
require the other packages), and never *write* into another package's store. To make the imports real rather
than caller-arranged `sys.path` hacks, **every package now ships a `pyproject.toml` and is installed editable**
(`pip install -e` × 7); the `sys.path.insert` shim in `weather_shapes.py` is removed. **Status:** DONE
(Phase-2 review); the dependency direction stays acyclic: pricemodeling ← {all}, weathergen ← {demand, res},
{demand, res, availability} ← dispatch, powersim_core ← everything.
