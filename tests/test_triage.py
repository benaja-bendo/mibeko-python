"""Tests du triage natif vs MinerU (étage 2), sur de vrais PDF générés à la volée.

On utilise PyMuPDF pour construire les PDF de test (pas de mock du triage
lui-même) : un texte natif propre doit rester natif, un PDF sans couche texte
(scan) doit basculer vers MinerU même si le score de qualité serait neutre.
"""

from pathlib import Path

import fitz
import pytest

from src.extractor.text_quality import compute_ocr_quality
from src.parsing.triage import (
    MAX_SINGLE_LETTER_WORD_RATIO,
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


def _pdf_with_pages(tmp_path: Path, texts: list[str], name: str) -> Path:
    """Comme `_pdf_with_text`, mais un texte DIFFÉRENT par page (pour tester
    un signal localisé à une seule page, dilué dans le reste du document)."""
    doc = fitz.open()
    rect = fitz.Rect(50, 50, 545, 792)
    for text in texts:
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
    """Un texte à qualité intermédiaire bascule selon le seuil demandé : seuil
    bas → natif accepté, seuil haut → renvoyé vers MinerU. Artefacts choisis
    SANS lettre isolée (DÉCRÊT/ARRETÊ, pas L0I/Artide) pour ne pas aussi
    déclencher le nouveau garde-fou MAX_SINGLE_LETTER_WORD_RATIO, testé à
    part — ce test-ci porte uniquement sur le seuil de score."""
    mixed = "ARTICLE PREMIER : La presente loi regit les relations. " * 30
    mixed += " DÉCRÊT ARRETÊ DÉCRÊT ARRETÊ " * 6
    pdf = _pdf_with_text(tmp_path, mixed, name="mixed.pdf")

    permissif = triage_pdf(pdf, threshold=0.10)
    assert permissif.method == "native"

    strict = triage_pdf(pdf, threshold=0.90)
    assert strict.method == "mineru"


def test_seuil_de_caracteres_par_page_est_300(tmp_path: Path):
    """mibeko-python#22 (absorbe #6) : un texte sous 300 car./page (mais
    au-dessus de l'ancien seuil de 50) bascule vers l'OCR — cas réel cité par
    docs/pipeline/protocole-validation.md : un JO à 69 car./page, qui n'était
    que ses numéros de page, avait été classé « natif exploitable »."""
    assert MIN_CHARS_PER_PAGE == 300
    # ~150 car./page : au-dessus de l'ancien seuil (50), en dessous du nouveau.
    faible = "Page numero. " * 11
    pdf = _pdf_with_text(tmp_path, faible, name="faible-densite.pdf")

    result = triage_pdf(pdf)

    assert result.chars_per_page < 300
    assert result.chars_per_page > 50
    assert result.method == "mineru"


def test_ratio_mots_une_lettre_bascule_vers_ocr_meme_a_score_correct(tmp_path: Path):
    """mibeko-python#22 : un texte dense en « mots » d'une lettre (colonnes de
    tableau M/F, sigles pointés…) route vers l'OCR même si le score global
    reste au-dessus du seuil de qualité — les deux garde-fous sont
    indépendants (cf. congo-jo-2007-17, rapport de viabilité du 14/09/2026 :
    79 % des mots d'une lettre y sont une colonne SEXE, pas du bruit OCR, et
    pourtant Mistral OCR y restitue un tableau bien mieux que l'extraction
    native — le réOCR reste bénéfique). Deux pages (MIN_DEGRADED_PAGES) : une
    seule page dégradée ne suffit pas (cf. test suivant, bruit d'échantillon)."""
    # Volume de texte propre suffisant pour un score global correct, mais un
    # motif "M F" répété pousse le ratio de mots d'une lettre au-delà de 6 %.
    corps = "Le contrat de travail engage les deux parties devant la loi. " * 20
    corps += "M F " * 30
    pdf = _pdf_with_text(tmp_path, corps, pages=2, name="tableau-mf.pdf")

    result = triage_pdf(pdf)

    per_page_quality = compute_ocr_quality(corps)
    ratio = per_page_quality["signals"]["single_letter_word_count"] / per_page_quality["signals"]["total_words"]
    assert ratio > MAX_SINGLE_LETTER_WORD_RATIO
    assert result.method == "mineru"
    assert "mots d'une lettre" in result.reason



# "M" est dans la liste blanche de mots d'une lettre légitimes de
# compute_ocr_quality (élision « m' »), pas "F" : seuls les "F" comptent comme
# mots d'une lettre. 40 répétitions -> 81 mots, dont 40 "F" (49 %), largement
# au-dessus de MAX_SINGLE_LETTER_WORD_RATIO (6 %) et de MIN_WORDS_FOR_PAGE_RATIO
# (50 mots) — un texte trop court serait ignoré comme bruit d'échantillon.
_PAGE_DEGRADEE = "M F " * 40 + "Texte."


def test_une_seule_page_degradee_ne_suffit_pas(tmp_path: Path):
    """mibeko-python#22 : une unique page dégradée parmi beaucoup de pages
    propres NE bascule PAS — MIN_DEGRADED_PAGES=2 filtre le bruit d'une page
    isolée (souvent un tableau/une liste légitime, pas un symptôme d'OCR
    dégradé sur le document). Complète le test « au moins 2 pages » ci-dessus
    et celui de dilution ci-dessous : les trois bornent le comportement exact."""
    propre = "Le contrat de travail engage les deux parties devant la loi. " * 15
    textes = [propre] * 19 + [_PAGE_DEGRADEE]  # 1 seule page dégradée sur 20
    pdf = _pdf_with_pages(tmp_path, textes, name="une-page-degradee.pdf")

    result = triage_pdf(pdf)

    assert result.method == "native"


def test_page_isolee_degradee_bascule_meme_diluee_dans_un_gros_document(tmp_path: Path):
    """mibeko-python#22 : le cas nommé par #6 (`Penal-Code-1836.pdf`, score
    agrégé 1.0, « natif exploitable ») — un document où quelques pages sur
    beaucoup sont dégradées (pages 2 et 4 du vrai document, 13,3 % et 4,2 %).
    Un ratio calculé sur le texte concaténé dilue le signal sous le seuil ;
    calculé page par page (>= MIN_DEGRADED_PAGES pages), il ne le rate pas."""
    propre = "Le contrat de travail engage les deux parties devant la loi. " * 15
    textes = [propre] * 18 + [_PAGE_DEGRADEE] * 2  # 2 pages dégradées sur 20
    pdf = _pdf_with_pages(tmp_path, textes, name="deux-pages-degradees.pdf")

    result = triage_pdf(pdf)

    # Le ratio agrégé (dilué sur 20 pages) reste sous le seuil...
    sig = result.quality["signals"]
    ratio_agrege = sig["single_letter_word_count"] / sig["total_words"]
    assert ratio_agrege < MAX_SINGLE_LETTER_WORD_RATIO
    # ...mais le triage route quand même vers l'OCR, grâce au calcul par page.
    assert result.method == "mineru"
    assert "2 pages" in result.reason
    assert "page 19" in result.reason or "page 20" in result.reason


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
