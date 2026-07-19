"""Authentification OAuth2 (client_credentials) pour l'API data.rte-france.com.

Flux : POST https://digital.iservices.rte-france.com/token/oauth/
       header Authorization: Basic base64(client_id:client_secret)
       body   grant_type=client_credentials
Réponse : { access_token, token_type, expires_in }. Token valable ~2 h.
"""
from __future__ import annotations

import base64
import time

import requests

TOKEN_URL = "https://digital.iservices.rte-france.com/token/oauth/"


class RteAuthError(RuntimeError):
    pass


class TokenManager:
    """Gère l'obtention et le rafraîchissement du jeton OAuth2 (cache en mémoire)."""

    def __init__(self, client_id: str | None, client_secret: str | None, session: requests.Session | None = None):
        if not client_id or not client_secret:
            raise RteAuthError(
                "Identifiants RTE manquants. Renseignez RTE_CLIENT_ID et RTE_CLIENT_SECRET "
                "dans le fichier .env (cf. .env.example)."
            )
        self._client_id = client_id
        self._client_secret = client_secret
        self._session = session or requests.Session()
        self._token: str | None = None
        self._expiry: float = 0.0

    def _basic_header(self) -> str:
        raw = f"{self._client_id}:{self._client_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def _refresh(self) -> None:
        resp = self._session.post(
            TOKEN_URL,
            headers={
                "Authorization": self._basic_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials"},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RteAuthError(
                f"Échec d'authentification RTE ({resp.status_code}) : {resp.text[:300]}"
            )
        payload = resp.json()
        self._token = payload["access_token"]
        # marge de 60 s avant expiration réelle
        self._expiry = time.time() + int(payload.get("expires_in", 7200)) - 60

    def token(self) -> str:
        if self._token is None or time.time() >= self._expiry:
            self._refresh()
        return self._token  # type: ignore[return-value]

    def invalidate(self) -> None:
        """Force le rafraîchissement au prochain appel (ex. après un 401 serveur)."""
        self._token = None
        self._expiry = 0.0

    def auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}
