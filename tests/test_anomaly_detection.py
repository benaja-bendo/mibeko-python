"""
Tests de la détection d'anomalies de numérotation (analyze_article_sequence).

Objectif : RAPPEL sur les vrais défauts (compilation non segmentée, blocs
d'articles perdus à l'OCR) tout en restant ROBUSTE à un OCR désordonné (qui,
analysé séquentiellement, produirait des faux positifs « saut de 86 à 553 »).

Exécutable sans dépendance externe :  python3 tests/test_anomaly_detection.py
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.main import (  # noqa: E402
    analyze_article_sequence,
    count_series_restarts,
    find_missing_runs,
)


def _seq(numbers):
    """Construit une séquence (ordinal, article_id) depuis une liste de numéros."""
    return [(n, uuid.uuid4()) for n in numbers]


def _types(anomalies):
    return [a["type_probleme"] for a in anomalies]


# --- helpers purs --------------------------------------------------------------

def test_find_missing_runs_contiguous_is_empty():
    assert find_missing_runs([1, 2, 3, 4]) == []


def test_find_missing_runs_detects_gaps():
    assert find_missing_runs([1, 2, 10]) == [(3, 9, 7)]
    assert find_missing_runs([1, 4, 5]) == [(2, 3, 2)]


def test_find_missing_runs_edge_cases():
    assert find_missing_runs([]) == []
    assert find_missing_runs([5]) == []


def test_count_series_restarts():
    assert count_series_restarts([11, 12, 1, 2]) == 1
    assert count_series_restarts([1, 2, 3]) == 0
    assert count_series_restarts([11, 1, 11, 1]) == 2
    # Une montée normale (11→12) n'est pas un redémarrage.
    assert count_series_restarts([1, 2, 11, 12]) == 0


# --- analyze_article_sequence --------------------------------------------------

def test_clean_series_has_no_anomalies():
    assert analyze_article_sequence(_seq([1, 2, 3, 4, 5])) == []


def test_immediate_duplicate_flagged():
    anomalies = analyze_article_sequence(_seq([1, 2, 2, 3]))
    assert _types(anomalies) == ["article_doublon"]
    assert anomalies[0]["article_id"] is not None


def test_small_gap_in_single_series():
    anomalies = analyze_article_sequence(_seq([1, 2, 5, 6]))
    assert _types(anomalies) == ["article_manquant"]
    assert "3-4" in anomalies[0]["description"]


def test_large_block_flagged_even_without_compilation():
    # 4..19 absents (16 numéros), série croissante simple.
    anomalies = analyze_article_sequence(_seq([1, 2, 3, 20, 21]))
    assert _types(anomalies) == ["bloc_manquant"]
    assert "4-19" in anomalies[0]["description"]


def test_compilation_detected_and_small_gaps_suppressed():
    # Deux redémarrages → compilation ; le petit trou 4-10 ne doit PAS être signalé.
    anomalies = analyze_article_sequence(_seq([11, 12, 1, 11, 12, 1]))
    assert _types(anomalies) == ["compilation_suspectee"]


def test_compilation_still_flags_large_block():
    # Compilation + bloc long (13-99) dans la plage haute : le bloc reste signalé,
    # le petit trou bas (2-10) est supprimé.
    anomalies = analyze_article_sequence(_seq([11, 12, 1, 11, 12, 1, 100]))
    types = _types(anomalies)
    assert "compilation_suspectee" in types
    assert "bloc_manquant" in types
    assert "article_manquant" not in types
    bloc = next(a for a in anomalies if a["type_probleme"] == "bloc_manquant")
    assert "13-99" in bloc["description"]


def test_penal_code_like_scrambled_compilation():
    """Cas réaliste reproduisant le code pénal : couverture dense 1-486, bloc
    487-552 perdu, 559-569 perdu, deux actes encapsulés (redémarrages) et deux
    doublons immédiats, le tout dans le DÉSORDRE.

    Régression directe : l'analyse séquentielle naïve produirait des dizaines de
    faux « sauts ». Ici on attend exactement 5 flags, tous pertinents.
    """
    numbers = []
    for n in range(1, 487):           # 1..486 présents
        numbers.append(n)
        if n in (320, 334):           # deux doublons immédiats réels
            numbers.append(n)
    # Désordre OCR + actes encapsulés (redémarrages) + blocs réellement absents.
    numbers += [1, 2, 3, 553, 554, 1, 2, 555, 556, 557, 558, 570, 571, 572, 573, 574]

    anomalies = analyze_article_sequence(_seq(numbers))
    types = _types(anomalies)

    assert types.count("article_doublon") == 2
    assert "compilation_suspectee" in types
    assert "article_manquant" not in types  # petits trous supprimés en compilation
    blocs = [a for a in anomalies if a["type_probleme"] == "bloc_manquant"]
    descriptions = " ".join(a["description"] for a in blocs)
    assert "487-552" in descriptions
    assert "559-569" in descriptions
    assert len(anomalies) == 5  # 2 doublons + 1 compilation + 2 blocs, aucun bruit


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
