"""Scenario workbook accessor (§7, ADR-5).

One `scenarios.xlsx` at the repo root is the single hand-edited source of assumptions. Its tabs are
prefixed by model — `avail_fleet_registry`, `demand_macro`, `res_capacity_trajectories`,
`dispatch_commodities`, … — so names stay unique and it is obvious what belongs where. This module is the
one read path: `load_model_sheets(path, "avail")` returns that model's tabs with the prefix stripped, so
each model sees its historical sheet names unchanged.

`snapshot()` freezes the workbook to immutable Parquet + a manifest (source hash + timestamp) for
provenance — you can always tell exactly which assumption values produced a given run. Editing stays in
the one xlsx; the snapshot is a code-side record, invisible to the editor.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from . import lake

MODEL_PREFIXES = ("avail_", "demand_", "res_", "dispatch_")


def _sep(prefix: str) -> str:
    return f"{prefix}_"


def load_model_sheets(path: str | Path, prefix: str) -> dict[str, pd.DataFrame]:
    """This model's tabs from the merged scenarios.xlsx, keyed with the `{prefix}_` stripped.

    Tolerant of a **legacy single-model workbook** (unprefixed tabs): if the file carries no model-prefixed
    tabs at all, every tab is returned as-is. But a merged workbook that simply lacks this model's tabs is
    an error (misconfiguration), not a silent all-tabs fallback.
    """
    pre = _sep(prefix)
    xl = pd.read_excel(path, sheet_name=None)
    out = {name[len(pre):]: df for name, df in xl.items() if name.startswith(pre)}
    if out:
        return out
    if any(name.startswith(k) for name in xl for k in MODEL_PREFIXES):
        raise ValueError(f"no '{pre}*' tabs in {path} (merged workbook missing this model?)")
    return dict(xl)                                          # unprefixed single-model / template workbook


def load_sheet(path: str | Path, prefix: str, sheet: str) -> pd.DataFrame:
    """One tab, addressed by (prefix, unprefixed sheet name)."""
    return pd.read_excel(path, sheet_name=f"{_sep(prefix)}{sheet}")


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snapshot(path: str | Path, out_dir: str | Path | None = None) -> Path:
    """Freeze every tab of the workbook to Parquet + a manifest (source hash, timestamp, per-tab shape).

    Immutable provenance record under `data/lake/scenarios/<sha12>/`. Returns the manifest path.
    """
    path = Path(path)
    digest = file_sha256(path)
    out_dir = Path(out_dir) if out_dir else (lake.lake_root() / "scenarios" / digest[:12])
    out_dir.mkdir(parents=True, exist_ok=True)
    xl = pd.read_excel(path, sheet_name=None)
    tabs = {}
    for name, df in xl.items():
        df.to_parquet(out_dir / f"{name}.parquet", compression="zstd", index=False)
        tabs[name] = list(df.shape)
    manifest = {"source": str(path), "sha256": digest, "frozen_at_utc": datetime.now(UTC).isoformat(),
                "tabs": tabs}
    mpath = out_dir / "manifest.json"
    mpath.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return mpath
