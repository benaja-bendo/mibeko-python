"""Tests du connecteur juricaf.org (mibeko-python#19).

Fixtures HTML calquées sur le balisage réel observé le 06/09/2026 (extrait
minimal, sans toucher au réseau — httpx.MockTransport comme
test_acquisition_politeness.py).
"""

from pathlib import Path

import httpx

from src.acquisition.juricaf import (
    ArretRef,
    discover_arrets,
    fetch_arret_html,
    next_page_url,
    parse_listing_page,
    run_juricaf_acquire,
)
from src.acquisition.manifest import Manifest
from src.acquisition.politeness import PoliteClient

PAGE_1_HTML = """
<div class="card bloc-search-item">
  <p class="card-header fs-5"><a class="a-unstyled" href="/arret/OHADA-COURCOMMUNEDEJUSTICEETDARBITRAGEOHADA-20230713-1632023">OHADA, Cour commune de justice et d'arbitrage (ohada), 13 juillet 2023, 163/2023</a></p>
  <div class="card-body" data-link=/arret/OHADA-COURCOMMUNEDEJUSTICEETDARBITRAGEOHADA-20230713-1632023>
    <p class="card-text text-justify">Extrait du premier arrêt...</p>
  </div>
</div>
<div class="card bloc-search-item">
  <p class="card-header fs-5"><a class="a-unstyled" href="/arret/OHADA-COURCOMMUNEDEJUSTICEETDARBITRAGEOHADA-20230713-1642023">OHADA, Cour commune de justice et d'arbitrage (ohada), 13 juillet 2023, 164/2023</a></p>
  <div class="card-body" data-link=/arret/OHADA-COURCOMMUNEDEJUSTICEETDARBITRAGEOHADA-20230713-1642023>
    <p class="card-text text-justify">Extrait du deuxième arrêt...</p>
  </div>
</div>
<ul class="pagination justify-content-center">
  <li class="page-item"><a href="/recherche/+/facet_juridiction%3ACour_commune_de_justice_et_d%27arbitrage%2Cfacet_pays%3AOHADA?tri=DESC&amp;pays=OHADA&amp;page=2">Suivant <i class="bi bi-chevron-right"></i></a></li>
</ul>
"""

PAGE_2_HTML = """
<div class="card bloc-search-item">
  <p class="card-header fs-5"><a class="a-unstyled" href="/arret/OHADA-COURCOMMUNEDEJUSTICEETDARBITRAGEOHADA-20230713-1652023">OHADA, Cour commune de justice et d'arbitrage (ohada), 13 juillet 2023, 165/2023</a></p>
  <div class="card-body" data-link=/arret/OHADA-COURCOMMUNEDEJUSTICEETDARBITRAGEOHADA-20230713-1652023>
    <p class="card-text text-justify">Extrait du troisième arrêt...</p>
  </div>
</div>
<ul class="pagination justify-content-center"></ul>
"""

ARRET_HTML = """
<article>ORGANISATION POUR L'HARMONISATION EN AFRIQUE DU DROIT DES AFFAIRES...
l'article 301 de l'Acte uniforme portant sur le droit commercial général...
</article>
"""


def test_parse_listing_page_extrait_slug_titre_numero_et_date():
    resultats = parse_listing_page(PAGE_1_HTML)

    assert len(resultats) == 2
    premier = resultats[0]
    assert premier.slug == "OHADA-COURCOMMUNEDEJUSTICEETDARBITRAGEOHADA-20230713-1632023"
    assert premier.url == "https://juricaf.org/arret/OHADA-COURCOMMUNEDEJUSTICEETDARBITRAGEOHADA-20230713-1632023"
    assert premier.numero == "163/2023"
    assert premier.date_decision == "2023-07-13"
    assert resultats[1].numero == "164/2023"


def test_parse_listing_page_sans_resultat():
    assert parse_listing_page("<p>Rien ici</p>") == []


def test_next_page_url_present_puis_absent():
    assert next_page_url(PAGE_1_HTML) == (
        "https://juricaf.org/recherche/+/facet_juridiction%3ACour_commune_de_justice_et_d%27arbitrage"
        "%2Cfacet_pays%3AOHADA?tri=DESC&pays=OHADA&page=2"
    )
    assert next_page_url(PAGE_2_HTML) is None


def test_discover_arrets_traverse_la_pagination(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if "page=2" in str(request.url):
            return httpx.Response(200, text=PAGE_2_HTML)
        return httpx.Response(200, text=PAGE_1_HTML)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = PoliteClient(client=http, sleep=lambda _: None, min_delay=0, max_delay=0)
    try:
        resultats = list(discover_arrets(client))
    finally:
        client.close()

    assert [r.numero for r in resultats] == ["163/2023", "164/2023", "165/2023"]
    assert calls["n"] == 2  # deux pages, arrêt sur pagination vide


def test_discover_arrets_respecte_max_pages():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=PAGE_1_HTML)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = PoliteClient(client=http, sleep=lambda _: None, min_delay=0, max_delay=0)
    try:
        resultats = list(discover_arrets(client, max_pages=1))
    finally:
        client.close()

    # PAGE_1_HTML pointe vers une page 2 qui n'est jamais requêtée : max_pages coupe avant.
    assert [r.numero for r in resultats] == ["163/2023", "164/2023"]


def test_fetch_arret_html_ecrit_le_fichier_et_calcule_le_sha256(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=ARRET_HTML)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = PoliteClient(client=http, sleep=lambda _: None, min_delay=0, max_delay=0)
    arret = ArretRef(
        slug="OHADA-COURCOMMUNEDEJUSTICEETDARBITRAGEOHADA-20230713-1632023",
        url="https://juricaf.org/arret/OHADA-COURCOMMUNEDEJUSTICEETDARBITRAGEOHADA-20230713-1632023",
        titre="OHADA, Cour commune de justice et d'arbitrage (ohada), 13 juillet 2023, 163/2023",
        numero="163/2023",
        date_decision="2023-07-13",
    )

    try:
        sha256, size_bytes, dest = fetch_arret_html(client, arret, tmp_path)
    finally:
        client.close()

    assert dest == tmp_path / f"{arret.slug}.html"
    assert dest.read_text(encoding="utf-8") == ARRET_HTML
    assert size_bytes == len(ARRET_HTML.encode("utf-8"))
    assert len(sha256) == 64


def _client_servant(page_html: str, arret_html: str) -> PoliteClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/recherche/" in str(request.url):
            return httpx.Response(200, text=page_html)
        return httpx.Response(200, text=arret_html)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return PoliteClient(client=http, sleep=lambda _: None, min_delay=0, max_delay=0)


def test_run_juricaf_acquire_telecharge_et_manifeste(tmp_path):
    client = _client_servant(PAGE_2_HTML, ARRET_HTML)

    report = run_juricaf_acquire(tmp_path, client=client)

    assert report["telecharges"] == [
        "https://juricaf.org/arret/OHADA-COURCOMMUNEDEJUSTICEETDARBITRAGEOHADA-20230713-1652023"
    ]
    assert report["deja_connus"] == 0
    assert report["erreurs"] == []

    manifest = Manifest(tmp_path / "manifests" / "juricaf.jsonl")
    entry = manifest.get("juricaf/OHADA-COURCOMMUNEDEJUSTICEETDARBITRAGEOHADA-20230713-1652023")
    assert entry is not None
    assert entry.type_source == "jurisprudence_ccja"
    assert (tmp_path / entry.fichier).read_text(encoding="utf-8") == ARRET_HTML


def test_run_juricaf_acquire_ignore_ce_qui_est_deja_au_manifeste(tmp_path):
    client = _client_servant(PAGE_2_HTML, ARRET_HTML)
    premier = run_juricaf_acquire(tmp_path, client=client)
    assert len(premier["telecharges"]) == 1

    client_second_run = _client_servant(PAGE_2_HTML, ARRET_HTML)
    second = run_juricaf_acquire(tmp_path, client=client_second_run)

    assert second["telecharges"] == []
    assert second["deja_connus"] == 1


def test_run_juricaf_acquire_respecte_limit(tmp_path):
    client = _client_servant(PAGE_1_HTML, ARRET_HTML)

    report = run_juricaf_acquire(tmp_path, client=client, limit=1)

    assert len(report["telecharges"]) == 1
