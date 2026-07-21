"""CLI orchestrateur du pipeline PriceModeling.

Exemples
--------
    python -m pricemodeling init-db
    python -m pricemodeling rte-token            # teste l'authentification RTE
    python -m pricemodeling extract-meteo
    python -m pricemodeling extract-rte          # toutes les ressources activées
    python -m pricemodeling extract-rte --only generation_per_unit
    python -m pricemodeling reconcile-units
    python -m pricemodeling build-master
    python -m pricemodeling all                  # enchaîne tout
    python -m pricemodeling status
"""
from __future__ import annotations

import sys
from datetime import date, datetime

import typer
from sqlalchemy import text

# La console Windows est souvent en cp1252 : on force l'UTF-8 (avec repli) pour éviter
# qu'un caractère accolé d'un message d'API ne fasse planter l'affichage.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

from .config import load_settings  # noqa: E402 — after the UTF-8 stream reconfigure above
from .db import get_engine, init_db  # noqa: E402

app = typer.Typer(add_completion=False, help="Pipeline d'extraction et de fusion météo + RTE.")


def _engine_and_settings():
    settings = load_settings()
    engine = get_engine(settings.db_url)
    init_db(engine)
    return engine, settings


def _parse_date(value: str | None, default: date) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date() if value else default


@app.command("init-db")
def cmd_init_db():
    """Crée la base et les tables fixes."""
    engine, settings = _engine_and_settings()
    typer.echo(f"Base initialisée : {settings.db_path}")


@app.command("rte-token")
def cmd_rte_token():
    """Teste l'obtention d'un jeton OAuth2 RTE (valide les identifiants)."""
    settings = load_settings()
    from .rte.auth import TokenManager

    cid, secret = settings.rte_credentials
    tm = TokenManager(cid, secret)
    token = tm.token()
    typer.echo(f"OK — jeton obtenu (longueur {len(token)}). Sandbox={settings.rte.get('sandbox')}")


@app.command("extract-meteo")
def cmd_extract_meteo(
    start: str = typer.Option(None, help="YYYY-MM-DD (défaut: période config)"),
    end: str = typer.Option(None, help="YYYY-MM-DD (défaut: période config)"),
    force: bool = typer.Option(False, help="Ré-extrait même les mois déjà ingérés"),
):
    """Extrait les stations + observations SYNOP."""
    engine, settings = _engine_and_settings()
    if not settings.meteo.get("enabled", True):
        typer.echo("Météo désactivée dans settings.yaml")
        raise typer.Exit()
    from .meteo.stations import load_stations
    from .meteo.synop import extract_synop

    s = _parse_date(start, settings.period_start)
    e = _parse_date(end, settings.period_end)
    stations = settings.meteo.get("stations") or None
    rows = extract_synop(engine, settings.raw_dir, s, e, settings.meteo["parameters"], stations, force)
    typer.echo(f"SYNOP : {rows} observations écrites ({s} -> {e}).")
    # métadonnées stations (best-effort) après les observations, pour autoriser le repli
    n_st, st_source = load_stations(engine)
    typer.echo(f"Stations : {n_st} (source: {st_source})")


@app.command("extract-rte")
def cmd_extract_rte(
    only: str = typer.Option(None, help="Nom d'une ressource précise (cf. rte_catalog.yaml)"),
    start: str = typer.Option(None, help="YYYY-MM-DD"),
    end: str = typer.Option(None, help="YYYY-MM-DD"),
    force: bool = typer.Option(False, help="Ignore le cache d'ingestion"),
):
    """Extrait les ressources RTE activées."""
    engine, settings = _engine_and_settings()
    if not settings.rte.get("enabled", True):
        typer.echo("RTE désactivé dans settings.yaml")
        raise typer.Exit()
    from .rte.auth import TokenManager
    from .rte.client import RteClient
    from .rte.extract import extract_resource

    cid, secret = settings.rte_credentials
    tm = TokenManager(cid, secret)
    client = RteClient(
        tm, settings.raw_dir,
        sandbox=bool(settings.rte.get("sandbox", False)),
        min_interval_s=float(settings.rte.get("min_interval_s", 1.0)),
    )
    s = _parse_date(start, settings.period_start)
    e = _parse_date(end, settings.period_end)
    resources = settings.enabled_rte_resources()
    if only:
        resources = [r for r in resources if r.name == only]
        if not resources:
            typer.echo(f"Ressource '{only}' introuvable ou désactivée.")
            raise typer.Exit(code=1)
    for res in resources:
        try:
            rows = extract_resource(engine, client, res, s, e, force)
            typer.echo(f"[{res.category}] {res.name}: {rows} lignes -> {res.table}")
        except Exception as exc:  # on continue les autres ressources
            typer.echo(f"[ERREUR] {res.name}: {exc}")


@app.command("extract-entsoe")
def cmd_extract_entsoe(
    start: str = typer.Option(None, help="YYYY-MM-DD"),
    end: str = typer.Option(None, help="YYYY-MM-DD"),
    force: bool = typer.Option(False, help="Ignore le cache d'ingestion"),
):
    """Extrait les prix day-ahead (Spot) depuis ENTSO-E Transparency."""
    engine, settings = _engine_and_settings()
    cfg = settings.entsoe
    if not cfg.get("enabled", True):
        typer.echo("ENTSO-E désactivé dans settings.yaml")
        raise typer.Exit()
    from .entsoe.prices import extract_prices

    s = _parse_date(start, _parse_date(cfg.get("start"), settings.period_start))
    e = _parse_date(end, settings.period_end)
    try:
        rows = extract_prices(
            engine, settings.entsoe_token, settings.raw_dir,
            cfg.get("bidding_zone", "10YFR-RTE------C"), s, e, force,
        )
        typer.echo(f"ENTSO-E prix day-ahead : {rows} lignes ({s} -> {e}).")
    except Exception as exc:
        typer.echo(f"[ERREUR] ENTSO-E: {exc}")


@app.command("ingest-remit")
def cmd_ingest_remit(
    start: str = typer.Option(None, help="YYYY-MM-DD"),
    end: str = typer.Option(None, help="YYYY-MM-DD"),
    zones: str = typer.Option(None, help="Comma-separated zone list (default: the 7-zone footprint)"),
    force: bool = typer.Option(False, help="Ignore le cache d'ingestion"),
):
    """Ingère la disponibilité REMIT (indisponibilités groupes) ENTSO-E → table entsoe_unavailability (step v)."""
    engine, settings = _engine_and_settings()
    token = settings.entsoe_token
    if not token:
        typer.echo("[ERREUR] ENTSOE_TOKEN manquant dans .env")
        raise typer.Exit(code=1)
    from .entsoe.series import ZONES, _client
    from .entsoe.unavailability import ingest_unavailability

    s = _parse_date(start, settings.period_start)
    e = _parse_date(end, settings.period_end)
    zmap = {z: ZONES[z] for z in zones.split(",")} if zones else ZONES
    rows = ingest_unavailability(engine, _client(token), s, e, zones=zmap, force=force)
    typer.echo(f"REMIT indisponibilités : {rows} lignes ({s} -> {e}).")


@app.command("reconcile-units")
def cmd_reconcile_units():
    """Réconcilie les groupes de production et construit dim_production_unit."""
    engine, settings = _engine_and_settings()
    from .reconcile.units import reconcile_units

    stats = reconcile_units(engine, settings.data_dir / "reconciliation_report.csv")
    typer.echo(f"Réconciliation : {stats}")


@app.command("build-master")
def cmd_build_master(
    start: str = typer.Option(None, help="YYYY-MM-DD"),
    end: str = typer.Option(None, help="YYYY-MM-DD"),
    no_units: bool = typer.Option(False, help="Sans le bloc production par groupe (colonnes unit_*)"),
    no_stations: bool = typer.Option(False, help="Sans le bloc météo par station (colonnes meteo_<station>_*)"),
):
    """Construit l'unique table maître horaire large (agrégats + météo station + groupes)."""
    engine, settings = _engine_and_settings()
    from .merge.build_master import build_master

    s = _parse_date(start, settings.period_start)
    e = _parse_date(end, settings.period_end)
    stats = build_master(
        engine, s, e,
        timezone_local=settings.merge.get("timezone_local", "Europe/Paris"),
        include_units=not no_units,
        include_stations=not no_stations,
    )
    typer.echo(f"Fusion : {stats}")


@app.command("all")
def cmd_all(
    start: str = typer.Option(None, help="YYYY-MM-DD"),
    end: str = typer.Option(None, help="YYYY-MM-DD"),
):
    """Enchaîne extract-meteo, extract-rte, extract-entsoe, reconcile-units, build-master."""
    cmd_extract_meteo(start=start, end=end, force=False)
    cmd_extract_rte(only=None, start=start, end=end, force=False)
    cmd_extract_entsoe(start=start, end=end, force=False)
    cmd_reconcile_units()
    cmd_build_master(start=start, end=end, no_units=False)


@app.command("status")
def cmd_status():
    """Affiche un résumé du contenu de la base."""
    engine, settings = _engine_and_settings()
    from sqlalchemy import inspect

    insp = inspect(engine)
    typer.echo(f"Base : {settings.db_path}")
    for name in sorted(insp.get_table_names()):
        with engine.connect() as conn:
            n = conn.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar()
        typer.echo(f"  {name:40s} {n:>12,} lignes")


@app.command("qc-sources")
def cmd_qc_sources(
    strict: bool = typer.Option(False, help="Sort en code 1 s'il reste des divergences inexpliquées"),
):
    """Compare la production RTE et ENTSO-E, mois par mois et par filière.

    À lancer après une extraction : le repli de `build-master` répare les trous RTE en silence, donc sans
    ce contrôle un nouveau défaut de source deviendrait invisible. `--strict` permet de l'utiliser comme
    garde-fou automatique.
    """
    from .qc import report

    engine, _ = _engine_and_settings()
    text_out, n = report(engine)
    typer.echo(text_out)
    if strict and n:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
