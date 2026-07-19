"""Phase 2 — the calibrated availability parameter set: fitted distributions + save/load.

Holds everything the projection engine (Phase 6) needs, fitted from the inferred nuclear outage
catalogue (planned / forced / common-mode) plus workbook-EFOR defaults for the non-inferable
technologies and lightly-calibrated weather-derating / hydro-inflow sensitivities. Persisted as
portable JSON via `powersim_core.serialize` (no pickle — resolves REVIEW F6); all fields are plain
nested dicts of scalars/strings/lists, so the round-trip is exact.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from powersim_core.serialize import load_params, save_params


@dataclass
class CalibratedAvailability:
    planned: dict[str, Any]          # palier -> {cycle_months, seasonality{month:w}, types{ASR/VP/VD:{...}}}
    forced: dict[str, Any]           # technology -> {freq_per_unit_year, dur_lognorm_mu/sigma, trend, source}
    common_mode: dict[str, Any]      # {event_freq_per_year, affected_fraction_*, per_unit_extra_days_*,
                                     #  stagger_weeks, return_years_2022, target_prob_<palier>}
    derating: dict[str, Any]         # basin -> {air_temp_threshold_c, derate_frac_per_c, source}
    inflows: dict[str, Any]          # {reservoir:{...}, ror:{...}}
    metrics: dict[str, Any] = field(default_factory=dict)

    def save(self, path: str | Path) -> Path:
        """Write params to `<path>.json` (JSON, portable). The `.pkl` suffix historically passed by
        callers is normalised to `.json`; the returned path is what was actually written."""
        return save_params(asdict(self), Path(path).with_suffix(".json"))

    @staticmethod
    def load(path: str | Path) -> CalibratedAvailability:
        d = load_params(Path(path).with_suffix(".json"))
        # JSON coerces all dict keys to str; restore the int keys the projection indexes by, so the
        # loaded object is behaviourally identical to the historical pickle (e.g. planned_scheduler
        # does `seasonality.get(int(month))` — str keys would silently miss → uniform seasonality).
        for palier in d.get("planned", {}).values():
            if isinstance(palier, dict) and "seasonality" in palier:
                palier["seasonality"] = {int(k): v for k, v in palier["seasonality"].items()}
        res = d.get("inflows", {}).get("reservoir", {})
        if "seasonal_profile_week" in res:
            res["seasonal_profile_week"] = {int(k): v for k, v in res["seasonal_profile_week"].items()}
        return CalibratedAvailability(**d)
