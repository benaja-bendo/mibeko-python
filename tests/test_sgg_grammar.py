"""Tests de la grammaire des noms/URLs de Journaux officiels sgg.cg.

Les cas couvrent les variantes réellement observées sur les 68 JO de
data/sources/sgg/JO/ (relevé Phase 0) : simples, éditions spéciales -sp,
volumes romains du JO 2025-5 (avec sous-suffixes), doublons -2.
"""

import pytest

from src.acquisition.sgg import build_jo_url, extract_jo_links, parse_jo_filename


@pytest.mark.parametrize(
    "filename, annee, numero, special",
    [
        ("congo-jo-2026-13.pdf", 2026, "13", False),
        ("congo-jo-1958-1.pdf", 1958, "1", False),
        ("congo-jo-2023-05-sp.pdf", 2023, "5", True),
        ("congo-jo-1961-07-sp.pdf", 1961, "7", True),
        ("congo-jo-2022-24-2.pdf", 2022, "24", False),
        ("congo-jo-2025-5-volume-ix-2.pdf", 2025, "5", False),
        ("congo-jo-2025-5-volume-xvii-4.pdf", 2025, "5", False),
        ("congo-jo-2025-5-volume-vii-2025-2.pdf", 2025, "5", False),
        ("congo-jo-2025-5-volume-xxvi.pdf", 2025, "5", False),
    ],
)
def test_grammaire_variantes_reelles(filename, annee, numero, special):
    parsed = parse_jo_filename(filename)
    assert parsed is not None, filename
    assert parsed["annee"] == annee
    assert parsed["numero"] == numero
    assert parsed["special"] is special


@pytest.mark.parametrize(
    "filename",
    [
        "congo-code-1975-travail.pdf",       # code, pas un JO
        "congo-jo-2026-13.md",               # mauvaise extension
        "jo-2026-13.pdf",                    # préfixe manquant
        "congo-jo-26-13.pdf",                # année sur 2 chiffres
        "ACTE-UNIFORME-MEDIATION.pdf",
    ],
)
def test_grammaire_rejette_hors_motif(filename):
    assert parse_jo_filename(filename) is None


def test_url_reconstruite():
    assert (
        build_jo_url("congo-jo-2026-13.pdf")
        == "https://www.sgg.cg/JO/2026/congo-jo-2026-13.pdf"
    )
    assert (
        build_jo_url("congo-jo-2025-5-volume-ix-2.pdf")
        == "https://www.sgg.cg/JO/2025/congo-jo-2025-5-volume-ix-2.pdf"
    )
    assert build_jo_url("pas-un-jo.pdf") is None


def test_extraction_des_liens_dune_page_index():
    html = """
    <ul>
      <li><a href="/JO/2026/congo-jo-2026-13.pdf">JO n°13</a></li>
      <li><a href="https://www.sgg.cg/JO/2025/congo-jo-2025-33.pdf">JO n°33</a></li>
      <li><a href='//www.sgg.cg/JO/2023/congo-jo-2023-05-sp.pdf'>Spécial</a></li>
      <li><a href="/autre/page.html">pas un JO</a></li>
      <li><a href="/JO/2026/congo-jo-2026-13.pdf">doublon</a></li>
    </ul>
    """
    links = extract_jo_links(html)
    assert links == [
        "https://www.sgg.cg/JO/2026/congo-jo-2026-13.pdf",
        "https://www.sgg.cg/JO/2025/congo-jo-2025-33.pdf",
        "https://www.sgg.cg/JO/2023/congo-jo-2023-05-sp.pdf",
    ]
