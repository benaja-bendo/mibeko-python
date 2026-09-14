"""Tests du client Mistral OCR (étage 2), réseau mocké (aucun appel réel)."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

import src.services.mistral_ocr_service as mistral_ocr_service_module
from src.services.mistral_ocr_service import MistralOcrError, MistralOcrService

_RealAsyncClient = httpx.AsyncClient


def _client_factory(handler):
    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return _RealAsyncClient(transport=httpx.MockTransport(handler))

    return factory


def _sleep_recorder(waits: list):
    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    return fake_sleep


def _make_pdf(tmp_path: Path) -> Path:
    pdf_path = tmp_path / "document.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 contenu factice")
    return pdf_path


def _ocr_success_response(pages: list) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "pages": pages,
            "model": "mistral-ocr-4-1",
            "usage_info": {"pages_processed": len(pages), "doc_size_bytes": 100},
        },
    )


def test_extract_reussi_assemble_le_markdown_avec_marqueurs_de_page(monkeypatch, tmp_path):
    monkeypatch.setattr(mistral_ocr_service_module, "MISTRAL_OCR_API_KEY", "cle-de-test")
    pdf_path = _make_pdf(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/files":
            assert request.headers["Authorization"] == "Bearer cle-de-test"
            return httpx.Response(200, json={"id": "file-abc", "object": "file"})
        assert request.url.path == "/v1/ocr"
        body = json.loads(request.content)
        assert body["model"] == "mistral-ocr-4-1"
        assert body["document"] == {"type": "file", "file_id": "file-abc"}
        return _ocr_success_response([
            {"index": 1, "markdown": "Deuxième page"},
            {"index": 0, "markdown": "Première page"},
        ])

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))

    service = MistralOcrService(sleep=_sleep_recorder([]))
    markdown, raw_json = asyncio.run(service.extract(pdf_path))

    # Pages réordonnées par index (0-based -> marqueur 1-based), quel que soit
    # l'ordre de la réponse. Même convention que render_native_markdown
    # (triage.py) : pas de ligne blanche entre marqueur et contenu suivant.
    assert markdown == "[[MIBEKO_PAGE:1]]\nPremière page\n[[MIBEKO_PAGE:2]]\nDeuxième page"
    assert json.loads(raw_json)["model"] == "mistral-ocr-4-1"


def test_cle_api_absente_leve_avant_tout_appel_reseau(monkeypatch, tmp_path):
    monkeypatch.setattr(mistral_ocr_service_module, "MISTRAL_OCR_API_KEY", "")
    pdf_path = _make_pdf(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("aucun appel réseau ne doit être tenté sans clé API")

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))

    service = MistralOcrService(sleep=_sleep_recorder([]))
    with pytest.raises(MistralOcrError, match="MISTRAL_OCR_API_KEY"):
        asyncio.run(service.extract(pdf_path))


def test_reponse_sans_page_leve_mistralocrerror(monkeypatch, tmp_path):
    monkeypatch.setattr(mistral_ocr_service_module, "MISTRAL_OCR_API_KEY", "cle-de-test")
    pdf_path = _make_pdf(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/files":
            return httpx.Response(200, json={"id": "file-abc"})
        return _ocr_success_response([])

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))

    service = MistralOcrService(sleep=_sleep_recorder([]))
    with pytest.raises(MistralOcrError, match="sans aucune page"):
        asyncio.run(service.extract(pdf_path))


def test_429_puis_succes_backoff(monkeypatch, tmp_path):
    monkeypatch.setattr(mistral_ocr_service_module, "MISTRAL_OCR_API_KEY", "cle-de-test")
    pdf_path = _make_pdf(tmp_path)
    statuts = iter([429, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/files":
            statut = next(statuts)
            if statut != 200:
                return httpx.Response(statut, text="rate limited")
            return httpx.Response(200, json={"id": "file-abc"})
        return _ocr_success_response([{"index": 0, "markdown": "contenu"}])

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))

    waits: list = []
    service = MistralOcrService(sleep=_sleep_recorder(waits))
    markdown, _ = asyncio.run(service.extract(pdf_path))

    assert markdown == "[[MIBEKO_PAGE:1]]\ncontenu"
    assert waits[0] == 5.0  # premier palier de backoff


def test_4xx_hors_429_leve_immediatement_sans_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(mistral_ocr_service_module, "MISTRAL_OCR_API_KEY", "cle-de-test")
    pdf_path = _make_pdf(tmp_path)
    appels: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        appels.append(request)
        return httpx.Response(401, text="clé invalide")

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))

    service = MistralOcrService(sleep=_sleep_recorder([]))
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        asyncio.run(service.extract(pdf_path))

    assert excinfo.value.response.status_code == 401
    assert len(appels) == 1


def test_repli_sur_mistral_api_key_si_ocr_key_absente(monkeypatch, tmp_path):
    """MISTRAL_OCR_API_KEY vide : repli sur MISTRAL_API_KEY (dev, une seule
    clé disponible) — cf. le calcul au chargement du module."""
    monkeypatch.setattr(mistral_ocr_service_module, "MISTRAL_OCR_API_KEY", "cle-partagee-structuration")
    pdf_path = _make_pdf(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer cle-partagee-structuration"
        if request.url.path == "/v1/files":
            return httpx.Response(200, json={"id": "file-abc"})
        return _ocr_success_response([{"index": 0, "markdown": "x"}])

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))

    service = MistralOcrService(sleep=_sleep_recorder([]))
    asyncio.run(service.extract(pdf_path))
