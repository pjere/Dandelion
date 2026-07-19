"""Client générique pour l'API data.rte-france.com.

Responsabilités :
* découper la période en fenêtres respectant la limite de chaque ressource (chunk_days) ;
* émettre les requêtes GET authentifiées avec backoff et gestion du 429 (Retry-After) ;
* mettre en cache chaque réponse JSON sur disque -> ré-exécutions incrémentales.
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import RteResource
from .auth import TokenManager

BASE_URL = "https://digital.iservices.rte-france.com"
PARIS = ZoneInfo("Europe/Paris")


class RteHttpError(RuntimeError):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"HTTP {status}: {message}")


def iter_chunks(
    start: date, end: date, chunk_days: int
) -> Iterator[tuple[datetime, datetime]]:
    """Génère des fenêtres [début, fin) en heure locale Europe/Paris, de taille <= chunk_days."""
    cur = datetime(start.year, start.month, start.day, tzinfo=PARIS)
    end_dt = datetime(end.year, end.month, end.day, tzinfo=PARIS) + timedelta(days=1)
    step = timedelta(days=chunk_days)
    while cur < end_dt:
        nxt = min(cur + step, end_dt)
        yield cur, nxt
        cur = nxt


def iter_year_chunks(start: date, end: date) -> Iterator[tuple[datetime, datetime]]:
    """Fenêtres alignées sur l'année civile (1er janv. -> 1er janv.), requises par certaines
    API (ex. capacités installées)."""
    for year in range(start.year, end.year + 1):
        c0 = datetime(year, 1, 1, tzinfo=PARIS)
        c1 = datetime(year + 1, 1, 1, tzinfo=PARIS)
        yield c0, c1


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S%z")[:-2] + ":" + dt.strftime("%z")[-2:]


def chunk_key(c0: datetime, c1: datetime) -> str:
    return f"{c0.date().isoformat()}_{c1.date().isoformat()}"


class RteClient:
    def __init__(
        self,
        token_manager: TokenManager,
        raw_dir: Path,
        sandbox: bool = False,
        min_interval_s: float = 1.0,
        session: requests.Session | None = None,
    ):
        self._tm = token_manager
        self._raw_dir = raw_dir
        self._sandbox = sandbox
        self._min_interval = min_interval_s
        self._session = session or requests.Session()
        self._last_call = 0.0

    def _url(self, res: RteResource) -> str:
        base = f"{BASE_URL}/open_api/{res.api}/{res.version}"
        if self._sandbox:
            return f"{base}/sandbox/{res.resource}"
        return f"{base}/{res.resource}"

    def _cache_path(self, res: RteResource, key: str) -> Path:
        env = "sandbox" if self._sandbox else "prod"
        p = self._raw_dir / "rte" / env / res.name
        p.mkdir(parents=True, exist_ok=True)
        return p / f"{key}.json"

    def _throttle(self) -> None:
        wait = self._min_interval - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    @retry(
        retry=retry_if_exception_type(requests.RequestException),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _get(self, url: str, params: dict) -> dict:
        self._throttle()
        resp = self._session.get(
            url, params=params, headers=self._tm.auth_header(), timeout=120
        )
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "30"))
            time.sleep(min(retry_after, 120))
            raise requests.RequestException("429 Too Many Requests")
        if resp.status_code == 401:
            # token probablement expiré (runs longs > 2 h) : on force un refresh et on réessaie
            self._tm.invalidate()
            raise requests.RequestException("401 -> refresh token")
        if resp.status_code == 403:
            raise RteHttpError(403, resp.text[:300])
        if resp.status_code == 404:
            raise RteHttpError(404, f"Ressource introuvable : {url}")
        if resp.status_code >= 500:
            raise requests.RequestException(f"{resp.status_code} serveur")
        if not resp.ok:
            raise RteHttpError(resp.status_code, resp.text[:300])
        return resp.json()

    def fetch_chunk(
        self, res: RteResource, c0: datetime | None, c1: datetime | None, use_cache: bool = True
    ) -> dict:
        """Récupère une fenêtre (avec cache disque). Retourne le JSON brut.

        Pour une ressource « snapshot » (res.params == 'none'), c0/c1 valent None et aucun
        paramètre de date n'est envoyé.
        """
        key = "snapshot" if res.params == "none" else chunk_key(c0, c1)
        cache = self._cache_path(res, key)
        if use_cache and cache.exists() and cache.stat().st_size > 0:
            return json.loads(cache.read_text(encoding="utf-8"))

        params: dict[str, str] = {}
        if res.params in ("start_end", "yearly"):
            params = {"start_date": _iso(c0), "end_date": _iso(c1)}
        payload = self._get(self._url(res), params)
        cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return payload
