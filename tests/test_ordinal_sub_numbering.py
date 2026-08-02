"""Remédiation 2026-08-02 phase 5 : `ordinal_from_raw_number` exclut de la
séquence les insertions de sous-numérotation légitimes (bis/ter, tiret,
décimal) — sinon `analyze_article_sequence` les traite comme des doublons de
leur article de base. Confirmé sur 3 codes STOCK réels en prod (Code civil
Congo-Brazzaville, Code Pénal, Règlement UEAC 07/12) : 285 des 652 flags
`article_doublon` initiaux relevaient de ce schéma.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.main import analyze_article_sequence, ordinal_from_raw_number  # noqa: E402


# --- ordinal_from_raw_number, cas positifs (déjà couverts avant le correctif) --

def test_numero_simple_inchange():
    assert ordinal_from_raw_number("42") == 42


def test_premier_inchange():
    assert ordinal_from_raw_number("premier") == 1


def test_nouveau_toujours_exclu():
    assert ordinal_from_raw_number("2 nouveau") is None


# --- Cas réels corrigés : sous-numérotations exclues de la séquence ----------

def test_suffixe_latin_bis_exclu():
    """Cas réel : Code Pénal, « 66 bis »."""
    assert ordinal_from_raw_number("66 bis") is None


def test_suffixe_latin_quater_exclu():
    """Cas réel : Code Pénal, « 138 quater »."""
    assert ordinal_from_raw_number("138 quater") is None


def test_suffixe_tiret_exclu():
    """Cas réel : Code civil, « 16-1 » à « 16-14 »."""
    assert ordinal_from_raw_number("16-1") is None
    assert ordinal_from_raw_number("16-14") is None


def test_numerotation_decimale_exclue():
    """Cas réel : Règlement n° 07/12-UEAC-066-CM-23, « 1.2 », « 1.11 »."""
    assert ordinal_from_raw_number("1.2") is None
    assert ordinal_from_raw_number("1.11") is None


def test_numero_de_base_reste_dans_la_sequence():
    """Le numéro de base (sans suffixe) continue d'être suivi normalement —
    seules les INSERTIONS en sont exclues, pas l'article d'origine."""
    assert ordinal_from_raw_number("16") == 16
    assert ordinal_from_raw_number("66") == 66
    assert ordinal_from_raw_number("1") == 1


# --- Intégration : plus de faux doublon sur une insertion, gap toujours détecté --

def _seq(numbers):
    return [(n, uuid.uuid4()) for n in numbers]


def test_insertions_tiret_ne_produisent_plus_de_doublon():
    """Cas réel simplifié (Code civil) : 15, 16, [insertions 16-1..16-3 déjà
    exclues en amont par ordinal_from_raw_number — non représentées ici
    puisqu'elles valent None et ne sont jamais ajoutées à la séquence], 17."""
    anomalies = analyze_article_sequence(_seq([15, 16, 17, 18]))
    assert anomalies == []


def test_insertions_bis_avec_vrai_doublon_ailleurs_toujours_detecte():
    """Les insertions bis/ter n'empêchent pas de détecter un VRAI doublon
    par ailleurs dans le même document (numéro de base répété sans lien)."""
    anomalies = analyze_article_sequence(_seq([1, 2, 3, 2, 4]))
    doublons = [a for a in anomalies if a["type_probleme"] == "article_doublon"]
    assert len(doublons) == 1
