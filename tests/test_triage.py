"""Tests du triage natif vs MinerU (étage 2), sur de vrais PDF générés à la volée.

On utilise PyMuPDF pour construire les PDF de test (pas de mock du triage
lui-même) : un texte natif propre doit rester natif, un PDF sans couche texte
(scan) doit basculer vers MinerU même si le score de qualité serait neutre.
"""

from pathlib import Path

import fitz
import pytest

from src.parsing.triage import (
    MIN_CHARS_PER_PAGE,
    render_native_markdown,
    triage_pdf,
)

ARTICLE_TEXT = (
    "ARTICLE PREMIER : La presente loi regit les relations de travail dans\n"
    "le secteur prive et public. Les dispositions ci-apres s'appliquent a\n"
    "tous les travailleurs exercant leur activite professionnelle sur le\n"
    "territoire de la Republique du Congo, sans distinction d'origine, de\n"
    "sexe ou de religion. Le contrat de travail est regi par les principes\n"
    "generaux du droit des obligations et par les conventions collectives."
)


def _pdf_with_text(tmp_path: Path, text: str, pages: int = 1, name: str = "doc.pdf") -> Path:
    # insert_textbox (avec wrapping dans un rectangle) plutôt qu'insert_text :
    # une longue chaîne sans retour à la ligne dépasse la page et MuPDF n'en
    # extrait alors qu'une fraction — insert_textbox garantit un round-trip
    # fidèle (vérifié : len(get_text()) == len(text) sur ces fixtures).
    doc = fitz.open()
    rect = fitz.Rect(50, 50, 545, 792)
    for _ in range(pages):
        page = doc.new_page()
        page.insert_textbox(rect, text, fontsize=9)
    path = tmp_path / name
    doc.save(str(path))
    doc.close()
    return path


def _pdf_blank(tmp_path: Path, pages: int = 1, name: str = "scan.pdf") -> Path:
    """PDF sans aucune couche texte (simule un scan sans OCR)."""
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    path = tmp_path / name
    doc.save(str(path))
    doc.close()
    return path


def test_pdf_avec_texte_natif_propre_reste_natif(tmp_path: Path):
    pdf = _pdf_with_text(tmp_path, ARTICLE_TEXT, pages=2)
    result = triage_pdf(pdf)

    assert result.method == "native"
    assert result.num_pages == 2
    assert result.quality["score"] >= 0.9
    assert result.text is not None
    assert "[[MIBEKO_PAGE:1]]" in result.text
    assert "[[MIBEKO_PAGE:2]]" in result.text
    assert "ARTICLE PREMIER" in result.text


def test_pdf_scanne_sans_texte_bascule_vers_mineru(tmp_path: Path):
    """Un scan pur (couche texte vide) a un score neutre 1.0 — mais chars_per_page
    doit l'emporter et forcer la route MinerU (piège documenté dans triage.py).
    """
    pdf = _pdf_blank(tmp_path, pages=3)
    result = triage_pdf(pdf)

    assert result.method == "mineru"
    assert result.quality["score"] == 1.0  # score neutre, à ne pas utiliser seul
    assert result.chars_per_page == 0.0
    assert "scan" in result.reason.lower()
    assert result.text is None


def test_texte_degrade_sous_le_seuil_bascule_vers_mineru(tmp_path: Path):
    """Beaucoup d'artefacts OCR connus (L0I, Artide…) → score sous le seuil,
    malgré un volume de texte natif suffisant (chars_per_page > MIN). On
    évite ici le caractère U+FFFD : la police PDF de base ne sait pas le
    dessiner (substitué silencieusement), donc il ne survit pas au
    round-trip PDF → un artefact ASCII connu est un signal plus fiable
    pour un test bout-en-bout sur un vrai PDF."""
    degraded = ("Article 1.- Objet et champ d'application. L0I Artide L0I Artide " * 40)
    pdf = _pdf_with_text(tmp_path, degraded, pages=1, name="degrade.pdf")
    result = triage_pdf(pdf, threshold=0.60)

    assert result.chars_per_page >= MIN_CHARS_PER_PAGE
    assert result.method == "mineru"
    assert "qualité native insuffisante" in result.reason


def test_seuil_personnalise_est_respecte(tmp_path: Path):
    """Un texte à qualité intermédiaire (score ~0.43) bascule selon le seuil
    demandé : seuil bas → natif accepté, seuil haut → renvoyé vers MinerU."""
    mixed = "ARTICLE PREMIER : La presente loi regit les relations. " * 30
    mixed += " L0I Artide L0I Artide " * 6
    pdf = _pdf_with_text(tmp_path, mixed, name="mixed.pdf")

    permissif = triage_pdf(pdf, threshold=0.10)
    assert permissif.method == "native"

    strict = triage_pdf(pdf, threshold=0.90)
    assert strict.method == "mineru"


def test_render_native_markdown_numerote_a_partir_de_1():
    markdown = render_native_markdown(["page un", "page deux"])
    assert markdown.splitlines()[:2] == ["[[MIBEKO_PAGE:1]]", "page un"]
    assert markdown.splitlines()[2:] == ["[[MIBEKO_PAGE:2]]", "page deux"]


def test_triage_est_deterministe(tmp_path: Path):
    """Rejouer le triage sur le même PDF donne exactement le même résultat."""
    pdf = _pdf_with_text(tmp_path, ARTICLE_TEXT, pages=2)
    first = triage_pdf(pdf)
    second = triage_pdf(pdf)
    assert first.method == second.method
    assert first.quality == second.quality
    assert first.text == second.text
