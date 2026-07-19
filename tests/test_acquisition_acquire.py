"""Tests bout-en-bout de l'acquisition pilotée par le carnet (réseau mocké).

DoD Phase 1 : une commande télécharge ce que le carnet demande avec manifeste
complet ; toute relance = zéro re-téléchargement, zéro doublon.
"""

from pathlib import Path

import httpx
import yaml

from src.acquisition.acquire import run_acquire
from src.acquisition.manifest import Manifest
from src.acquisition.politeness import PoliteClient

INDEX_HTML = """
<html><body>
  <a href="/JO/2026/congo-jo-2026-13.pdf">JO 13</a>
  <a href="/JO/2026/congo-jo-2026-14.pdf">JO 14</a>
  <a href="/JO/2020/congo-jo-2020-1.pdf">JO hors carnet</a>
</body></html>
"""

PDF_13 = b"%PDF-1.4 jo treize"
PDF_14 = b"%PDF-1.4 jo quatorze"


def _handler_factory(counters: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        counters[url] = counters.get(url, 0) + 1
        if url.endswith("journaux-speciaux.html"):
            return httpx.Response(200, text=INDEX_HTML)
        if url.endswith("congo-jo-2026-13.pdf"):
            return httpx.Response(200, content=PDF_13)
        if url.endswith("congo-jo-2026-14.pdf"):
            return httpx.Response(200, content=PDF_14)
        return httpx.Response(404)

    return handler


def _carnet(tmp_path: Path) -> Path:
    carnet = {
        "version": 1,
        "series": [
            {
                "id": "jo-recents",
                "type": "journal_officiel",
                "annees": [2026],
                "index_pages": ["https://www.sgg.cg/journaux-speciaux.html"],
                "enumeration": {"actif": False},
            }
        ],
        "textes": [],
    }
    path = tmp_path / "corpus-v1.yaml"
    path.write_text(yaml.safe_dump(carnet, allow_unicode=True), encoding="utf-8")
    return path


def _polite(counters: dict) -> PoliteClient:
    http = httpx.Client(transport=httpx.MockTransport(_handler_factory(counters)))
    return PoliteClient(client=http, sleep=lambda _: None, min_delay=0, max_delay=0)


def test_acquisition_telecharge_le_carnet_et_filtre_les_annees(tmp_path: Path):
    data = tmp_path / "data"
    counters: dict = {}
    report = run_acquire(_carnet(tmp_path), data, client=_polite(counters))

    result = report["series"]["jo-recents"]
    assert result["urls_decouvertes"] == 2  # le JO 2020 est filtré (hors carnet)
    assert result["telecharges"] == 2
    assert (data / "sources" / "sgg" / "JO" / "congo-jo-2026-13.pdf").read_bytes() == PDF_13

    manifest = Manifest(data / "manifests" / "sgg-jo.jsonl")
    entry = manifest.get("sgg-jo/congo-jo-2026-13")
    assert entry.source_url == "https://www.sgg.cg/JO/2026/congo-jo-2026-13.pdf"
    assert entry.fetched_at is not None
    assert entry.sha256 and entry.size_bytes == len(PDF_13)
    assert entry.retroactif is False


def test_relance_zero_retelechargement(tmp_path: Path):
    data = tmp_path / "data"
    counters: dict = {}
    run_acquire(_carnet(tmp_path), data, client=_polite(counters))
    assert counters["https://www.sgg.cg/JO/2026/congo-jo-2026-13.pdf"] == 1

    report = run_acquire(_carnet(tmp_path), data, client=_polite(counters))
    result = report["series"]["jo-recents"]
    assert result["telecharges"] == 0
    assert result["ignores_connus"] == 2
    # Les PDF n'ont pas été re-téléchargés (compteur inchangé).
    assert counters["https://www.sgg.cg/JO/2026/congo-jo-2026-13.pdf"] == 1
    assert counters["https://www.sgg.cg/JO/2026/congo-jo-2026-14.pdf"] == 1


def test_dry_run_ne_telecharge_rien(tmp_path: Path):
    data = tmp_path / "data"
    counters: dict = {}
    report = run_acquire(_carnet(tmp_path), data, dry_run=True, client=_polite(counters))

    result = report["series"]["jo-recents"]
    assert result["prevus"] == [
        "https://www.sgg.cg/JO/2026/congo-jo-2026-13.pdf",
        "https://www.sgg.cg/JO/2026/congo-jo-2026-14.pdf",
    ]
    assert not (data / "sources").exists()
    assert "https://www.sgg.cg/JO/2026/congo-jo-2026-13.pdf" not in counters


def test_repli_enumeration_si_autoindex_ferme(tmp_path: Path):
    """Autoindex fermé (404) pour cette année : repli sur l'énumération HEAD."""
    data = tmp_path / "data"
    counters: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        counters[url] = counters.get(url, 0) + 1
        if url == "https://www.sgg.cg/JO/2026/":
            return httpx.Response(404)  # autoindex fermé pour cette année
        if url == "https://www.sgg.cg/JO/2026/congo-jo-2026-1.pdf":
            return httpx.Response(200, content=PDF_13)
        return httpx.Response(404)

    carnet = {
        "version": 1,
        "series": [
            {
                "id": "jo-recents",
                "type": "journal_officiel",
                "annees": [2026],
                "enumeration": {"actif": True, "max_numero": 2, "stop_apres_absents": 1},
            }
        ],
        "textes": [],
    }
    path = tmp_path / "corpus-v1.yaml"
    path.write_text(yaml.safe_dump(carnet, allow_unicode=True), encoding="utf-8")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = PoliteClient(client=http, sleep=lambda _: None, min_delay=0, max_delay=0)
    report = run_acquire(path, data, client=client)

    result = report["series"]["jo-recents"]
    assert counters["https://www.sgg.cg/JO/2026/"] == 1  # autoindex tenté une fois
    assert result["urls_decouvertes"] == 1  # trouvé par l'énumération, filet
    assert result["telecharges"] == 1


def test_doublon_checksum_non_reintroduit(tmp_path: Path):
    """Un même contenu servi sous deux URLs ne crée qu'une entrée."""
    data = tmp_path / "data"
    counters: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        counters[url] = counters.get(url, 0) + 1
        if url.endswith(".html"):
            return httpx.Response(
                200,
                text='<a href="/JO/2026/congo-jo-2026-13.pdf">a</a>'
                '<a href="/JO/2026/congo-jo-2026-13-2.pdf">b</a>',
            )
        return httpx.Response(200, content=PDF_13)  # même contenu partout

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = PoliteClient(client=http, sleep=lambda _: None, min_delay=0, max_delay=0)
    report = run_acquire(_carnet(tmp_path), data, client=client)

    result = report["series"]["jo-recents"]
    assert result["telecharges"] == 1
    assert result["doublons"] == 1
    manifest = Manifest(data / "manifests" / "sgg-jo.jsonl")
    assert len(manifest) == 1
    entry = manifest.get("sgg-jo/congo-jo-2026-13")
    assert any(e.quoi == "doublon_checksum" for e in entry.evenements)
    # Le fichier doublon n'a pas été conservé sur disque.
    assert not (data / "sources" / "sgg" / "JO" / "congo-jo-2026-13-2.pdf").exists()
