"""Tests du client poli : User-Agent, backoff, téléchargement atomique."""

from pathlib import Path

import httpx
import pytest

from src.acquisition.politeness import AcquisitionError, PoliteClient


def _client(handler, **kwargs) -> PoliteClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, headers={"User-Agent": "MibekoBot/test"})
    sleeps: list[float] = []
    client = PoliteClient(client=http, sleep=sleeps.append, min_delay=0, max_delay=0, **kwargs)
    client._test_sleeps = sleeps  # type: ignore[attr-defined]
    return client


def test_user_agent_identifiable_par_defaut():
    client = PoliteClient(sleep=lambda _: None)
    try:
        agent = client._client.headers["User-Agent"]
        assert agent.startswith("MibekoBot/")
        assert "@" in agent  # email de contact présent
    finally:
        client.close()


def test_backoff_exponentiel_sur_5xx_puis_succes():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, text="ok")

    client = _client(handler, backoff_base=5.0)
    response = client.get("https://www.sgg.cg/index.html")
    assert response.status_code == 200
    assert calls["n"] == 3
    # Deux retries → deux attentes de backoff : 5 s puis 10 s.
    assert [s for s in client._test_sleeps if s >= 5.0] == [5.0, 10.0]


def test_abandon_apres_epuisement_des_tentatives():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = _client(handler, max_retries=2)
    with pytest.raises(AcquisitionError):
        client.get("https://www.sgg.cg/toujours-en-panne")


def test_pas_de_retry_sur_404():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    client = _client(handler)
    response = client.head("https://www.sgg.cg/JO/2026/congo-jo-2026-99.pdf")
    assert response.status_code == 404
    assert calls["n"] == 1


def test_download_atomique_et_checksum(tmp_path: Path):
    payload = b"%PDF-1.4 contenu de test"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    client = _client(handler)
    dest = tmp_path / "sources" / "sgg" / "JO" / "congo-jo-2026-13.pdf"
    sha, size = client.download("https://www.sgg.cg/JO/2026/congo-jo-2026-13.pdf", dest)

    import hashlib

    assert dest.read_bytes() == payload
    assert sha == hashlib.sha256(payload).hexdigest()
    assert size == len(payload)
    assert not list(dest.parent.glob("*.part"))  # pas de fichier partiel résiduel
