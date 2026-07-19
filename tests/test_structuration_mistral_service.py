"""Tests du client Mistral (étage 3), réseau mocké (aucun appel réel)."""

import asyncio
import json

import httpx
import pytest

import src.services.mistral_service as mistral_service_module
from src.services.mistral_service import MistralService

INSTRUCTIONS = "Extrais les métadonnées d'en-tête."

_RealAsyncClient = httpx.AsyncClient


def _client_factory(handler):
    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return _RealAsyncClient(transport=httpx.MockTransport(handler))

    return factory


def test_appel_reussi_retourne_le_json_decode(monkeypatch):
    monkeypatch.setattr(mistral_service_module, "MISTRAL_API_KEY", "cle-de-test")
    content = json.dumps({"nature": "Loi", "numero": "12-2026"})

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer cle-de-test"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))

    service = MistralService()
    result = asyncio.run(service.extract_metadata("texte à analyser", INSTRUCTIONS))

    assert result == {"nature": "Loi", "numero": "12-2026"}


def test_timeout_est_propage(monkeypatch):
    monkeypatch.setattr(mistral_service_module, "MISTRAL_API_KEY", "cle-de-test")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("délai dépassé")

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))

    service = MistralService()
    with pytest.raises(httpx.TimeoutException):
        asyncio.run(service.extract_metadata("texte à analyser", INSTRUCTIONS))


def test_cle_api_absente_leve_avant_tout_appel_reseau(monkeypatch):
    monkeypatch.setattr(mistral_service_module, "MISTRAL_API_KEY", "")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("aucun appel réseau ne doit être tenté sans clé API")

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))

    service = MistralService()
    with pytest.raises(ValueError, match="MISTRAL_API_KEY"):
        asyncio.run(service.extract_metadata("texte à analyser", INSTRUCTIONS))


def test_reponse_json_malformee_leve_jsondecodeerror(monkeypatch):
    monkeypatch.setattr(mistral_service_module, "MISTRAL_API_KEY", "cle-de-test")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ceci n'est pas du JSON"}}]},
        )

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))

    service = MistralService()
    with pytest.raises(json.JSONDecodeError):
        asyncio.run(service.extract_metadata("texte à analyser", INSTRUCTIONS))


def test_erreur_http_leve_httpstatuserror(monkeypatch):
    monkeypatch.setattr(mistral_service_module, "MISTRAL_API_KEY", "cle-de-test")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="erreur serveur Mistral")

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))

    service = MistralService(sleep=_sleep_recorder([]))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(service.extract_metadata("texte à analyser", INSTRUCTIONS))


def _sleep_recorder(waits: list):
    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    return fake_sleep


def test_429_puis_succes_backoff_et_delai_minimal(monkeypatch):
    monkeypatch.setattr(mistral_service_module, "MISTRAL_API_KEY", "cle-de-test")
    content = json.dumps({"nature": "Loi"})
    statuts = iter([429, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        statut = next(statuts)
        if statut != 200:
            return httpx.Response(statut, text="rate limited")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))

    waits: list = []
    service = MistralService(sleep=_sleep_recorder(waits))
    result = asyncio.run(service.extract_metadata("texte à analyser", INSTRUCTIONS))

    assert result == {"nature": "Loi"}
    # 1er palier de backoff (5 s) puis délai minimal (~1,5 s) avant la relance.
    assert waits[0] == 5.0
    assert 0 < waits[1] <= 1.5


def test_retry_after_prime_sur_le_palier_de_backoff(monkeypatch):
    monkeypatch.setattr(mistral_service_module, "MISTRAL_API_KEY", "cle-de-test")
    content = json.dumps({"nature": "Loi"})
    statuts = iter([429, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        statut = next(statuts)
        if statut != 200:
            return httpx.Response(statut, headers={"Retry-After": "12"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))

    waits: list = []
    service = MistralService(sleep=_sleep_recorder(waits))
    asyncio.run(service.extract_metadata("texte à analyser", INSTRUCTIONS))

    # max(palier 5 s, Retry-After 12 s) = 12 s.
    assert waits[0] == 12.0


def test_4xx_hors_429_leve_immediatement_sans_retry(monkeypatch):
    monkeypatch.setattr(mistral_service_module, "MISTRAL_API_KEY", "cle-de-test")
    appels: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        appels.append(request)
        return httpx.Response(401, text="clé invalide")

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))

    service = MistralService(sleep=_sleep_recorder([]))
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        asyncio.run(service.extract_metadata("texte à analyser", INSTRUCTIONS))

    assert excinfo.value.response.status_code == 401
    assert len(appels) == 1


def test_abandon_apres_5_tentatives_sur_429(monkeypatch):
    monkeypatch.setattr(mistral_service_module, "MISTRAL_API_KEY", "cle-de-test")
    appels: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        appels.append(request)
        return httpx.Response(429, text="rate limited")

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))

    waits: list = []
    service = MistralService(sleep=_sleep_recorder(waits))
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        asyncio.run(service.extract_metadata("texte à analyser", INSTRUCTIONS))

    assert excinfo.value.response.status_code == 429
    assert len(appels) == 5
    # Paliers de backoff : 5, 10, 20, 40 s (les autres attentes = délai minimal).
    assert [w for w in waits if w >= 5.0] == [5.0, 10.0, 20.0, 40.0]
