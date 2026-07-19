"""Chargement de la configuration (settings.yaml, rte_catalog.yaml, .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Racine du projet = dossier parent du package pricemodeling/
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"


@dataclass(frozen=True)
class RteResource:
    """Une ressource REST de l'API data.rte-france.com (lue depuis rte_catalog.yaml)."""

    name: str
    api: str
    version: str
    resource: str
    table: str
    chunk_days: int
    start_date: date
    params: str = "start_end"
    category: str = "autre"
    enabled: bool = True

    def url(self, base: str) -> str:
        """URL complète de la ressource pour un base_url donné."""
        return f"{base.rstrip('/')}/open_api/{self.api}/{self.version}/{self.resource}"


@dataclass(frozen=True)
class Settings:
    raw: dict[str, Any]
    rte_resources: list[RteResource]
    project_root: Path = PROJECT_ROOT

    # ----- chemins -----
    @property
    def data_dir(self) -> Path:
        return (self.project_root / self.raw["paths"]["data_dir"]).resolve()

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / self.raw["paths"]["raw_subdir"]

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.raw["paths"]["database"]

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"

    # ----- période -----
    @property
    def period_start(self) -> date:
        return _as_date(self.raw["period"]["start"])

    @property
    def period_end(self) -> date:
        end = _as_date(self.raw["period"]["end"])
        today = datetime.utcnow().date()
        return min(end, today)

    # ----- sous-sections -----
    @property
    def meteo(self) -> dict[str, Any]:
        return self.raw.get("meteo", {})

    @property
    def rte(self) -> dict[str, Any]:
        return self.raw.get("rte", {})

    @property
    def merge(self) -> dict[str, Any]:
        return self.raw.get("merge", {})

    @property
    def entsoe(self) -> dict[str, Any]:
        return self.raw.get("entsoe", {})

    @property
    def entsoe_token(self) -> str | None:
        return os.getenv("ENTSOE_TOKEN")

    # ----- secrets RTE (.env) -----
    @property
    def rte_credentials(self) -> tuple[str | None, str | None]:
        return os.getenv("RTE_CLIENT_ID"), os.getenv("RTE_CLIENT_SECRET")

    def enabled_rte_resources(self) -> list[RteResource]:
        return [r for r in self.rte_resources if r.enabled]

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)


def _as_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _load_rte_catalog(path: Path) -> list[RteResource]:
    data = _load_yaml(path)
    resources = []
    for item in data.get("resources", []):
        resources.append(
            RteResource(
                name=item["name"],
                api=item["api"],
                version=item["version"],
                resource=item["resource"],
                table=item["table"],
                chunk_days=int(item["chunk_days"]),
                start_date=_as_date(item["start_date"]),
                params=item.get("params", "start_end"),
                category=item.get("category", "autre"),
                enabled=bool(item.get("enabled", True)),
            )
        )
    return resources


@lru_cache(maxsize=1)
def load_settings(
    settings_path: str | None = None,
    catalog_path: str | None = None,
) -> Settings:
    """Charge la configuration complète (mémoïsée)."""
    load_dotenv(PROJECT_ROOT / ".env")
    s_path = Path(settings_path) if settings_path else CONFIG_DIR / "settings.yaml"
    c_path = Path(catalog_path) if catalog_path else CONFIG_DIR / "rte_catalog.yaml"
    raw = _load_yaml(s_path)
    resources = _load_rte_catalog(c_path)
    settings = Settings(raw=raw, rte_resources=resources)
    settings.ensure_dirs()
    return settings
