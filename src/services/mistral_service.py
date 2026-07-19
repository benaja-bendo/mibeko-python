import asyncio
import json
import os
import time
from typing import Awaitable, Callable, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "mistral-large-latest")
MISTRAL_TIMEOUT_SECONDS = float(os.getenv("MISTRAL_TIMEOUT_SECONDS", "60"))
MISTRAL_MIN_INTERVAL_SECONDS = float(os.getenv("MISTRAL_MIN_INTERVAL_SECONDS", "1.5"))


class MistralService:
    def __init__(
        self,
        min_interval: float = MISTRAL_MIN_INTERVAL_SECONDS,
        max_retries: int = 4,
        backoff_base: float = 5.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.api_url = MISTRAL_API_URL
        self.model = MISTRAL_MODEL
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleep
        self._last_request_at: float = 0.0
        self.headers = {
            "Authorization": f"Bearer {MISTRAL_API_KEY}",
            "Accept": "application/json"
        }

    # -- politesse (même pattern que src/acquisition/politeness.py) -----------

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

    async def _post_with_backoff(self, client: httpx.AsyncClient, payload: dict) -> httpx.Response:
        """POST poli : délai minimal entre appels, backoff exponentiel sur 429/5xx.

        Backoff : 5 s, 10 s, 20 s, 40 s (max entre le palier et l'en-tête
        Retry-After si présent), abandon après max_retries + 1 tentatives en
        levant l'erreur httpx d'origine. Les autres 4xx (400, 401, 403…) lèvent
        immédiatement : une clé invalide ne se répare pas en réessayant.
        """
        response: Optional[httpx.Response] = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                # Backoff exponentiel : 5 s, 10 s, 20 s, 40 s…
                wait = self.backoff_base * (2 ** (attempt - 1))
                retry_after = self._retry_after_seconds(response)
                if retry_after is not None:
                    wait = max(wait, retry_after)
                await self._sleep(wait)
            await self._respect_delay()
            response = await client.post(self.api_url, headers=self.headers, json=payload)
            if response.status_code == 429 or response.status_code >= 500:
                continue
            response.raise_for_status()
            return response
        response.raise_for_status()
        return response

    async def extract_metadata(self, texte: str, instructions: str) -> dict:
        """
        Appelle l'API Mistral pour extraire des métadonnées structurées.
        Retourne le dict brut décodé du JSON renvoyé (non validé ici).
        """
        if not MISTRAL_API_KEY:
            raise ValueError("MISTRAL_API_KEY est vide ou absente.")

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": texte}
            ],
            "response_format": {"type": "json_object"}
        }

        async with httpx.AsyncClient(timeout=MISTRAL_TIMEOUT_SECONDS) as client:
            response = await self._post_with_backoff(client, payload)

        data = response.json()
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)

mistral_service = MistralService()
