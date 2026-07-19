"""
Tests de l'indicateur de qualité OCR (compute_ocr_quality) et de son garde-fou.

L'indicateur est une heuristique de LISIBILITÉ (pas de justesse juridique) : il
part de signaux réels et mesurables (U+FFFD, artefacts OCR connus, fragmentation,
caractères de contrôle) et produit un score ∈ [0, 1] (1 = propre). Le garde-fou
n'est PAS bloquant : il émet un flag de sévérité 'warning'.

Objectifs de test :
  - un texte propre marque haut et n'est jamais signalé (pas de faux positif) ;
  - un texte dégradé marque bas et franchit le seuil (rappel) ;
  - le score reste déterministe et borné à [0, 1] ;
  - le calcul ne surinterprète pas un texte vide ou un U+FFFD isolé et rare.

Exécutable sans base :  python3 tests/test_ocr_quality.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.main import (  # noqa: E402
    OCR_QUALITY_WARN_THRESHOLD,
    compute_ocr_quality,
    sanitize_legal_text,
)


CLEAN_TEXT = """ARTICLE PREMIER : La présente loi régit les relations de travail.
Article 2.- Les dispositions du présent code s'appliquent à tous les travailleurs
exerçant leur activité professionnelle sur le territoire national. Le contrat de
travail est régi par les principes généraux du droit des obligations.
Fait à Brazzaville, le 18 avril 2026."""

# Texte fortement dégradé : U+FFFD (glyphes non reconnus), artefacts OCR connus
# et fragmentation lettre à lettre — reproduit un OCR ~65 % (cas code pénal).
DEGRADED_TEXT = """L0I n� 12 du 3 j�nvier. Art�cle pr�m�er d e l a p r � s e n t e
DÉCRÊT ARRETÊ l e c o n t r � t d e t r a v � i l e s t r � g i.
A r t i d e 2 l e s d i s p o s i t i o n s s � p p l i q u e n t."""


# --- score borné et déterministe ---------------------------------------------

def test_score_is_bounded_and_deterministic():
    for txt in (CLEAN_TEXT, DEGRADED_TEXT, "", "abc", "�" * 100):
        q1 = compute_ocr_quality(txt)
        q2 = compute_ocr_quality(txt)
        assert 0.0 <= q1["score"] <= 1.0
        assert q1["score"] == q2["score"]  # déterministe


def test_clean_text_scores_high_and_not_flagged():
    q = compute_ocr_quality(CLEAN_TEXT)
    assert q["score"] >= 0.95
    assert q["score"] >= OCR_QUALITY_WARN_THRESHOLD  # jamais signalé
    assert q["signals"]["replacement_char_count"] == 0
    assert q["signals"]["ocr_artifact_count"] == 0


def test_degraded_text_scores_low_and_flagged():
    q = compute_ocr_quality(DEGRADED_TEXT)
    assert q["score"] < OCR_QUALITY_WARN_THRESHOLD  # franchit le seuil d'alerte
    assert q["signals"]["replacement_char_count"] >= 5
    assert q["signals"]["single_letter_word_count"] >= 10


# --- pas de faux positif sur des cas légitimes --------------------------------

def test_empty_text_is_neutral():
    # Un texte vide n'a aucune dégradation MESURABLE : score neutre (1.0), pas
    # d'alerte. Le cas « document vide/sans article » est traité ailleurs.
    q = compute_ocr_quality("")
    assert q["score"] == 1.0
    assert q["total_chars"] == 0


def test_rare_isolated_replacement_char_not_flagged():
    # Un seul U+FFFD noyé dans un long texte propre reste tolérable : un glyphe
    # exotique isolé ne doit pas condamner tout un document.
    long_clean = "Les dispositions du présent code s'appliquent à tous. " * 40
    q = compute_ocr_quality(long_clean + "un glyphe � isolé.")
    assert q["score"] >= OCR_QUALITY_WARN_THRESHOLD
    assert q["signals"]["replacement_char_count"] == 1


def test_legitimate_french_single_letter_words_not_fragmentation():
    # « à », « y », « a », « l' » (élisions) sont des mots d'une lettre légitimes :
    # ils ne doivent PAS être comptés comme fragmentation OCR.
    txt = "Il y a lieu à statuer. L'affaire a été jugée. On y reviendra."
    q = compute_ocr_quality(txt)
    assert q["signals"]["single_letter_word_count"] == 0
    assert q["score"] == 1.0


# --- signaux individuels ------------------------------------------------------

def test_replacement_char_density_drives_score_down():
    # ~3 % de U+FFFD suffit à faire chuter le score sous le seuil.
    txt = ("x" * 32 + "�") * 100
    q = compute_ocr_quality(txt)
    assert q["score"] < OCR_QUALITY_WARN_THRESHOLD
    assert q["penalties"]["replacement"] > q["penalties"]["fragmentation"]


def test_known_ocr_artifacts_are_counted():
    # Les artefacts détectés sont EXACTEMENT ceux que sanitize_legal_text répare
    # (même source de vérité) : L0I, Artide, DÉCRÊT, ARRETÊ, N°o.
    txt = "L0I Artide DÉCRÊT ARRETÊ N°o"
    q = compute_ocr_quality(txt)
    assert q["signals"]["ocr_artifact_count"] == 5


def test_artifact_set_matches_sanitize_repair():
    # Garde-fou anti-divergence : ce que l'on MESURE (artefacts) correspond à ce
    # que l'on RÉPARE. Après réparation, plus aucun artefact ne doit être compté.
    raw = "L0I fondamentale, Artide 1, DÉCRÊT, ARRETÊ, N°o 5."
    before = compute_ocr_quality(raw)["signals"]["ocr_artifact_count"]
    after = compute_ocr_quality(sanitize_legal_text(raw))["signals"]["ocr_artifact_count"]
    assert before == 5
    assert after == 0


def test_control_chars_are_penalised():
    # Bruit binaire (caractères de contrôle hors \n\t\r) : pénalisé, mais \n et \t
    # légitimes ne le sont pas.
    clean_ws = "Ligne 1.\n\tLigne 2 indentée.\nLigne 3."
    assert compute_ocr_quality(clean_ws)["signals"]["control_char_count"] == 0
    noisy = "Texte\x00avec\x07du\x1fbruit binaire " * 20
    q = compute_ocr_quality(noisy)
    assert q["signals"]["control_char_count"] >= 40
    assert q["penalties"]["control"] > 0.0


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"  ✓ {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"  ✗ {test.__name__} : {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests passés")
    sys.exit(1 if failures else 0)
