"""Découverte par autoindex Apache + détection de variantes multiples.

Recon du 04/07/2026 (consignée dans la mémoire persistante) : sgg.cg expose un
autoindex Apache ouvert sur /JO/{annee}/, et certains numéros nominaux ont
plusieurs fichiers différents (re-publications, versions interactives,
corrections). L'usine télécharge toutes les variantes et les signale pour
arbitrage humain plutôt que d'en choisir une silencieusement.
"""

from pathlib import Path

import httpx

from src.acquisition.manifest import Manifest
from src.acquisition.politeness import PoliteClient
from src.acquisition.sgg import acquire_jo_urls, discover_by_autoindex

# Extrait simplifié, fidèle au format réel (icônes + colonnes Last modified/Size
# omises : seuls les hrefs comptent pour le parsing).
AUTOINDEX_HTML = """
<html>
 <head><title>Index of /JO/2026</title></head>
 <body>
<h1>Index of /JO/2026</h1>
<pre><a href="?C=N;O=D">Name</a> <a href="?C=M;O=A">Last modified</a> <a href="?C=S;O=A">Size</a>
<a href="/JO/">Parent Directory</a>                             -
<a href="congo-jo-2026-26.pdf">congo-jo-2026-26.pdf</a>    2026-06-25 16:44  399K
<a href="congo-jo-2026-26-2.pdf">congo-jo-2026-26-2.pdf</a>  2026-06-25 17:38  400K
<a href="congo-jo-2026-27.pdf">congo-jo-2026-27.pdf</a>    2026-07-02 19:11  411K
</pre>
</body></html>
"""


def _polite(handler) -> PoliteClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return PoliteClient(client=http, sleep=lambda _: None, min_delay=0, max_delay=0, max_retries=1)


def test_discover_by_autoindex_parse_le_listing_apache():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=AUTOINDEX_HTML)

    urls = discover_by_autoindex(_polite(handler), 2026)

    assert urls == [
        "https://www.sgg.cg/JO/2026/congo-jo-2026-26.pdf",
        "https://www.sgg.cg/JO/2026/congo-jo-2026-26-2.pdf",
        "https://www.sgg.cg/JO/2026/congo-jo-2026-27.pdf",
    ]


def test_discover_by_autoindex_replie_silencieusement_si_ferme():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    assert discover_by_autoindex(_polite(handler), 1946) == []


def test_discover_by_autoindex_replie_silencieusement_si_erreur_reseau():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)  # épuise les tentatives -> AcquisitionError

    assert discover_by_autoindex(_polite(handler), 2026) == []


def test_variantes_multiples_toutes_telechargees_et_signalees(tmp_path: Path):
    """Deux fichiers différents sous le même numéro nominal : les deux sont
    conservés (pas de choix arbitraire) et flagués pour arbitrage humain."""
    contenus = {
        "https://www.sgg.cg/JO/2026/congo-jo-2026-1.pdf": b"%PDF-1.4 v1",
        "https://www.sgg.cg/JO/2026/congo-jo-2026-1-2.pdf": b"%PDF-1.4 v1 bis",
        "https://www.sgg.cg/JO/2026/congo-jo-2026-2.pdf": b"%PDF-1.4 v2",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=contenus[str(request.url)])

    manifest = Manifest(tmp_path / "sgg-jo.jsonl")
    summary = acquire_jo_urls(
        _polite(handler),
        manifest,
        list(contenus),
        sources_dir=tmp_path / "sources",
        known_shas={},
    )

    assert summary["telecharges"] == 3
    assert summary["variantes_multiples"] == 1

    entree_1 = manifest.get("sgg-jo/congo-jo-2026-1")
    entree_1_2 = manifest.get("sgg-jo/congo-jo-2026-1-2")
    entree_2 = manifest.get("sgg-jo/congo-jo-2026-2")

    groupe_attendu = ["sgg-jo/congo-jo-2026-1", "sgg-jo/congo-jo-2026-1-2"]
    assert entree_1.variantes_multiples == groupe_attendu
    assert entree_1_2.variantes_multiples == groupe_attendu
    assert any(e.quoi == "variantes_multiples_detectees" for e in entree_1.evenements)
    assert any(e.quoi == "variantes_multiples_detectees" for e in entree_1_2.evenements)

    # Numéro distinct : pas de flag.
    assert entree_2.variantes_multiples is None
    assert not any(e.quoi == "variantes_multiples_detectees" for e in entree_2.evenements)


def test_variantes_multiples_pas_de_doublon_evenement_sur_relance(tmp_path: Path):
    """Relancer sans fichier nouveau ne ré-ajoute pas l'événement (idempotent)."""
    contenus = {
        "https://www.sgg.cg/JO/2026/congo-jo-2026-1.pdf": b"%PDF-1.4 v1",
        "https://www.sgg.cg/JO/2026/congo-jo-2026-1-2.pdf": b"%PDF-1.4 v1 bis",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=contenus[str(request.url)])

    manifest = Manifest(tmp_path / "sgg-jo.jsonl")
    known_shas: dict = {}
    acquire_jo_urls(
        _polite(handler),
        manifest,
        list(contenus),
        sources_dir=tmp_path / "sources",
        known_shas=known_shas,
    )
    nb_evenements_avant = len(manifest.get("sgg-jo/congo-jo-2026-1").evenements)

    summary = acquire_jo_urls(
        _polite(handler),
        manifest,
        list(contenus),
        sources_dir=tmp_path / "sources",
        known_shas=known_shas,
    )

    assert summary["telecharges"] == 0
    assert summary["variantes_multiples"] == 0
    assert len(manifest.get("sgg-jo/congo-jo-2026-1").evenements) == nb_evenements_avant
