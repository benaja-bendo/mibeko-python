"""Client Mistral OCR (étage 2, remplace MinerU comme moteur OCR par défaut).

Décision du 14/09/2026 (docs/decisions.md) : Mistral OCR épinglé plutôt que
MinerU (clé cloud expirée depuis le 24/06, faiblesse du triage inchangée) ou
MinerU local redimensionné (hors périmètre `vps_infra`). Viabilité vérifiée
sur cinq documents représentatifs (tableaux denses, scan réel 2 colonnes,
comparaison contre MinerU local, contrôle de non-régression) avant ce commit.

Deux appels REST (`POST /v1/files` puis `POST /v1/ocr`, cf. la doc Mistral) :
le fichier est d'abord déposé, puis référencé par son `file_id` — pas de
double encodage base64 d'un PDF qui peut peser plusieurs dizaines de Mo.

Même pattern de politesse/backoff que `mistral_service.py` (délai minimal
entre appels, backoff exponentiel sur 429/5xx, échec immédiat sur les autres
4xx) : une clé invalide ne se répare pas en réessayant.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv
import os

load_dotenv()

MISTRAL_OCR_BASE_URL = "https://api.mistral.ai"
# Clé dédiée à l'OCR — distincte de MISTRAL_API_KEY (structuration) pour lire
# les coûts séparément en production. Repli sur MISTRAL_API_KEY en dev pour ne
# pas dupliquer la configuration quand une seule clé est disponible.
MISTRAL_OCR_API_KEY = os.getenv("MISTRAL_OCR_API_KEY", "") or os.getenv("MISTRAL_API_KEY", "")
# Épinglé (règle 3 du pipeline, rejouabilité) : jamais "-latest" — un OCR
# neuronal n'est pas déterministe d'un appel à l'autre, la rejouabilité vient
# de l'artefact conservé sur disque (pipeline/md/), pas du modèle. Disponible
# sur /v1/models au 14/09/2026 ; mistral-ocr-2503/-2505 sont déjà retirés.
MISTRAL_OCR_MODEL = os.getenv("MISTRAL_OCR_MODEL", "mistral-ocr-4-1")
MISTRAL_OCR_TIMEOUT_SECONDS = float(os.getenv("MISTRAL_OCR_TIMEOUT_SECONDS", "300"))
MISTRAL_OCR_MIN_INTERVAL_SECONDS = float(os.getenv("MISTRAL_OCR_MIN_INTERVAL_SECONDS", "0.5"))


class MistralOcrError(RuntimeError):
    """Échec de l'appel OCR Mistral (upload, traitement, ou réponse invalide)."""


def _page_marker_markdown(pages: List[Dict[str, Any]]) -> str:
    """Assemble les pages OCRisées avec des marqueurs `[[MIBEKO_PAGE:N]]`
    (même convention 1-based que le chemin natif, cf. `src/parsing/triage.py`
    et `src/api/main.py`) : la citabilité par page ne dépend donc pas de la
    méthode d'extraction retenue.
    """
    lines: List[str] = []
    for page in sorted(pages, key=lambda p: p.get("index", 0)):
        page_num = page.get("index", 0) + 1
        lines.append(f"[[MIBEKO_PAGE:{page_num}]]")
        lines.append((page.get("markdown") or "").strip())
    return "\n".join(lines)


class MistralOcrService:
    def __init__(
        self,
        min_interval: float = MISTRAL_OCR_MIN_INTERVAL_SECONDS,
        max_retries: int = 4,
        backoff_base: float = 5.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.base_url = MISTRAL_OCR_BASE_URL
        self.model = MISTRAL_OCR_MODEL
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleep
        self._last_request_at: float = 0.0
        self.headers = {
            "Authorization": f"Bearer {MISTRAL_OCR_API_KEY}",
            "Accept": "application/json",
        }

    # -- politesse / backoff (même pattern que mistral_service.py) ----------

    async def _respect_delay(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = self.min_interval - elapsed
        if wait > 0:
            await self._sleep(wait)
        self._last_request_at = time.monotonic()

    @staticmethod
    def _retry_after_seconds(response: Optional[httpx.Response]) -> Optional[float]:
        if response is None:
            return None
        retry_after = response.headers.get("Retry-After")
        if retry_after is None:
            return None
        try:
            return float(retry_after)
        except ValueError:
            return None

    async def _request_with_backoff(
        self, client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        """Backoff exponentiel sur 429/5xx : 5 s, 10 s, 20 s, 40 s (ou l'en-tête
        Retry-After si plus grand), abandon après max_retries + 1 tentatives.
        Les autres 4xx (400, 401, 403…) lèvent immédiatement.
        """
        response: Optional[httpx.Response] = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                wait = self.backoff_base * (2 ** (attempt - 1))
                retry_after = self._retry_after_seconds(response)
                if retry_after is not None:
                    wait = max(wait, retry_after)
                await self._sleep(wait)
            await self._respect_delay()
            response = await client.request(method, url, **kwargs)
            if response.status_code == 429 or response.status_code >= 500:
                continue
            response.raise_for_status()
            return response
        response.raise_for_status()
        return response

    # -- API publique ---------------------------------------------------

    async def upload_pdf(self, pdf_path: Path) -> str:
        """Dépose un PDF (`purpose=ocr`) et renvoie son `file_id`."""
        if not MISTRAL_OCR_API_KEY:
            raise MistralOcrError("MISTRAL_OCR_API_KEY (ou MISTRAL_API_KEY) est vide ou absente.")

        async with httpx.AsyncClient(timeout=MISTRAL_OCR_TIMEOUT_SECONDS) as client:
            with open(pdf_path, "rb") as fh:
                files = {"file": (pdf_path.name, fh, "application/pdf")}
                data = {"purpose": "ocr"}
                response = await self._request_with_backoff(
                    client, "POST", f"{self.base_url}/v1/files",
                    headers=self.headers, data=data, files=files,
                )

        file_id = response.json().get("id")
        if not file_id:
            raise MistralOcrError(f"Dépôt du fichier sans identifiant en retour : {response.text[:500]}")
        return file_id

    async def run_ocr(self, file_id: str) -> Dict[str, Any]:
        """Lance l'OCR sur un fichier déjà déposé et renvoie la réponse brute."""
        payload = {
            "model": self.model,
            "document": {"type": "file", "file_id": file_id},
            "confidence_scores_granularity": "page",
            "include_blocks": True,
        }
        async with httpx.AsyncClient(timeout=MISTRAL_OCR_TIMEOUT_SECONDS) as client:
            response = await self._request_with_backoff(
                client, "POST", f"{self.base_url}/v1/ocr",
                headers={**self.headers, "Content-Type": "application/json"}, json=payload,
            )
        return response.json()

    async def extract(self, pdf_path: Path) -> Tuple[str, str]:
        """Dépôt + OCR : renvoie (markdown avec marqueurs de page, JSON brut).

        Lève `MistralOcrError` si la réponse ne contient aucune page (jamais
        d'insertion partielle en aval — le caller traite ça comme un échec).
        """
        file_id = await self.upload_pdf(pdf_path)
        raw = await self.run_ocr(file_id)
        pages = raw.get("pages") or []
        if not pages:
            raise MistralOcrError("Réponse OCR sans aucune page.")
        markdown = _page_marker_markdown(pages)
        return markdown, json.dumps(raw, ensure_ascii=False)


mistral_ocr_service = MistralOcrService()
