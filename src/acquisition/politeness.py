"""Client HTTP poli pour l'acquisition (règle n°6 de la mission).

- User-Agent identifiable (MibekoBot + email de contact) ;
- ~1 requête toutes les 3-5 s (jitter aléatoire) ;
- backoff exponentiel sur 429/5xx et erreurs réseau ;
- aucune logique de re-téléchargement ici : c'est le manifeste (checksums/URLs
  connus) qui l'empêche en amont.
"""

from __future__ import annotations

import hashlib
import os
import random
import time
from pathlib import Path
from typing import Callable, Optional

import httpx

from .config import bot_user_agent


class AcquisitionError(RuntimeError):
    """Échec définitif après épuisement des tentatives."""


class PoliteClient:
    def __init__(
        self,
        min_delay: float = 3.0,
        max_delay: float = 5.0,
        max_retries: int = 4,
        backoff_base: float = 5.0,
        timeout: float = 120.0,
        client: Optional[httpx.Client] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleep
        self._last_request_at: float = 0.0
        self._client = client or httpx.Client(
            headers={"User-Agent": bot_user_agent()},
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- politesse -----------------------------------------------------------

    def _respect_delay(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = random.uniform(self.min_delay, self.max_delay) - elapsed
        if wait > 0:
            self._sleep(wait)
        self._last_request_at = time.monotonic()

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                # Backoff exponentiel : 5 s, 10 s, 20 s, 40 s…
                self._sleep(self.backoff_base * (2 ** (attempt - 1)))
            self._respect_delay()
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                last_error = exc
                continue
            if response.status_code in (429,) or response.status_code >= 500:
                last_error = AcquisitionError(
                    f"HTTP {response.status_code} sur {url}"
                )
                continue
            return response
        raise AcquisitionError(f"Abandon après {self.max_retries + 1} tentatives : {url}") from last_error

    # -- API ------------------------------------------------------------------

    def get(self, url: str) -> httpx.Response:
        return self._request("GET", url)

    def head(self, url: str) -> httpx.Response:
        return self._request("HEAD", url)

    def download(self, url: str, dest: Path) -> tuple[str, int]:
        """Télécharge en streaming vers un fichier temporaire puis os.replace.

        Retourne (sha256, size_bytes). Lève AcquisitionError si HTTP != 200.
        Le fichier final n'apparaît jamais partiellement écrit.
        """
        response = self._request("GET", url)
        if response.status_code != 200:
            raise AcquisitionError(f"HTTP {response.status_code} sur {url}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest.with_suffix(dest.suffix + ".part")
        digest = hashlib.sha256()
        size = 0
        with open(tmp_path, "wb") as handle:
            for chunk in response.iter_bytes(chunk_size=1024 * 256):
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        os.replace(tmp_path, dest)
        return digest.hexdigest(), size
