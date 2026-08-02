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
    derive_numero_origine,
    find_embedded_series_runs,
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


# --- derive_numero_origine (backfill des articles déjà renommés) --------------

def test_derive_numero_origine_suffixe_simple():
    assert derive_numero_origine("14_doublon_1") == "14"


def test_derive_numero_origine_suffixe_a_deux_chiffres():
    assert derive_numero_origine("14_doublon_11") == "14"


def test_derive_numero_origine_numero_non_purement_numerique():
    assert derive_numero_origine("PREAMBULE_doublon_2") == "PREAMBULE"
    assert derive_numero_origine("6 bis_doublon_1") == "6 bis"


def test_derive_numero_origine_non_suffixe_renvoie_none():
    assert derive_numero_origine("14") is None
    assert derive_numero_origine("PREAMBULE") is None


# --- audit docs/audit-ingestion-2026-08-02.md, phase 2 : doublons non consécutifs ---

def test_doublon_non_consecutif_detecte_hors_compilation():
    """« 1 » réapparaît bien plus loin dans une série non compilée (pas de
    redémarrage) : avant la phase 2, seuls les doublons CONSÉCUTIFS étaient
    détectés — celui-ci était invisible."""
    anomalies = analyze_article_sequence(_seq([1, 2, 3, 4, 1, 6]))
    doublons = [a for a in anomalies if a["type_probleme"] == "article_doublon"]
    assert len(doublons) == 1


def test_plusieurs_doublons_non_consecutifs_tous_comptes():
    anomalies = analyze_article_sequence(_seq([1, 2, 3, 1, 4, 2, 5]))
    doublons = [a for a in anomalies if a["type_probleme"] == "article_doublon"]
    assert len(doublons) == 2  # « 1 » et « 2 » réapparaissent chacun une fois


def test_nombre_de_flags_egale_nombre_de_collisions_consecutives_et_non():
    """Mission « corrections post-audit » phase 2 — critère explicite : le
    nombre de flags doit égaler le nombre de collisions, ni plus ni moins."""
    # 3 collisions : "2" consécutif, "5" non consécutif, "1" non consécutif.
    anomalies = analyze_article_sequence(_seq([1, 2, 2, 3, 5, 4, 5, 1]))
    doublons = [a for a in anomalies if a["type_probleme"] == "article_doublon"]
    assert len(doublons) == 3


def test_compilation_n_etend_pas_les_doublons_aux_reapparitions_non_consecutives():
    """En compilation, seuls les doublons CONSÉCUTIFS comptent : la
    réapparition d'un numéro sur un acte encapsulé différent (numérotation qui
    redémarre) reste le signal normal d'une nouvelle série, pas une erreur —
    comportement inchangé par l'extension de la phase 2."""
    anomalies = analyze_article_sequence(_seq([11, 12, 1, 2, 11, 12, 1, 2]))
    assert [a for a in anomalies if a["type_probleme"] == "article_doublon"] == []
    assert "compilation_suspectee" in _types(anomalies)


def test_already_flagged_exclut_les_articles_deja_signales_ailleurs():
    """`already_flagged` (articles déjà couverts par un flag de collision de
    chaîne posé par `unique_article_number`) ne doit pas produire un second
    flag pour la même collision vue sous l'angle ordinal."""
    seq = _seq([1, 2, 2, 3])
    id_du_second_2 = seq[2][1]

    sans_exclusion = analyze_article_sequence(seq)
    assert len([a for a in sans_exclusion if a["type_probleme"] == "article_doublon"]) == 1

    avec_exclusion = analyze_article_sequence(seq, already_flagged={id_du_second_2})
    assert [a for a in avec_exclusion if a["type_probleme"] == "article_doublon"] == []


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


# --- find_embedded_series_runs / remédiation audit 2026-08-02 phase 4 --------
# Acte principal COURT (loi de ratification, arrêté bref) + annexe qui
# renumérote à partir de 1 (traité ratifié, cahier des charges) : le sommet
# atteint avant la chute ne dépasse jamais `restart_prev_min` de
# `count_series_restarts`, donc `analyze_article_sequence` ne classait pas ces
# documents en compilation et signalait chaque article de l'annexe comme
# doublon individuel (652 signalements sur 70 documents, dont la majorité
# suivait ce schéma sur 6 échantillons réels vérifiés, de 1959 à 2025).


def test_find_embedded_series_runs_detects_short_main_body_plus_annex():
    # Corps principal [1, 2] (ex. loi de ratification à 2 articles), puis une
    # annexe qui redémarre à 1 et reprend une longue série ascendante.
    ordinals = [1, 2, 1, 2, 3, 4, 5, 6, 7, 8]
    assert find_embedded_series_runs(ordinals) == [(2, 9)]


def test_find_embedded_series_runs_ignores_immediate_duplicate():
    # « 2, 2 » consécutifs : ce n'est pas une chute (valeurs égales, pas
    # `prev > number`) — reste un doublon normal, pas une série incrustée.
    ordinals = [1, 2, 2, 3, 5, 4, 5, 1]
    assert find_embedded_series_runs(ordinals) == []


def test_find_embedded_series_runs_requires_sustained_run():
    # La chute vers « 1 » n'est suivie que d'une seule valeur (6) : pas assez
    # long pour confirmer une série secondaire (audit phase 2 : ce cas doit
    # rester un doublon isolé signalé normalement).
    ordinals = [1, 2, 3, 4, 1, 6]
    assert find_embedded_series_runs(ordinals) == []


def test_find_embedded_series_runs_requires_strict_drop():
    # Chute vers un numéro qui n'est pas strictement inférieur au précédent :
    # pas une chute réelle.
    ordinals = [1, 2, 3, 3, 4, 5, 6]
    assert find_embedded_series_runs(ordinals) == []


def test_embedded_series_suppresses_doublons_and_flags_once():
    """Cas réel (Loi n° 12-2025 du 28 mai 2025, prod) : loi de ratification à 2
    articles + le traité ratifié en annexe (14 articles, renumérotés à 1).
    Avant le correctif : 2 `article_doublon`. Après : 0, remplacés par un seul
    `compilation_suspectee` informatif."""
    ordinals = [1, 2, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    anomalies = analyze_article_sequence(_seq(ordinals))
    assert _types(anomalies) == ["compilation_suspectee"]


def test_isolated_erratum_like_duplicate_still_flagged():
    """Cas réel (Décret n° 59-248 du 3 décembre 1959, prod) : un « 2 » isolé
    réapparaît (erratum d'époque, « Au lieu de… Lire… ») sans rien relancer
    derrière lui — doit rester un doublon normal, pas une série incrustée."""
    ordinals = [1, 2, 3, 4, 5, 2]
    anomalies = analyze_article_sequence(_seq(ordinals))
    assert _types(anomalies) == ["article_doublon"]


def test_embedded_series_does_not_affect_already_flagged_compilation():
    # ≥2 redémarrages : la branche `is_compilation` existante est inchangée,
    # `find_embedded_series_runs` n'est même pas appelée dans ce cas.
    anomalies = analyze_article_sequence(_seq([11, 12, 1, 11, 12, 1]))
    assert _types(anomalies) == ["compilation_suspectee"]


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
