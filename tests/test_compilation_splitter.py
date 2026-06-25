"""
Tests de la segmentation markdown des compilations (P4).

suggest_markdown_boundaries doit repérer les débuts d'actes encapsulés
(LOI/ORDONNANCE/DÉCRET/ARRÊTÉ/CODE) dans un recueil, en restant PERMISSIF mais
sans confondre une citation inline (« Vu la loi n° X ») ou un en-tête structurel
(LIVRE/TITRE/CHAPITRE) avec un début d'acte.

Exécutable sans dépendance externe :  python3 tests/test_compilation_splitter.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.extractor.compilation_splitter import (  # noqa: E402
    slice_markdown_by_boundaries,
    suggest_markdown_boundaries,
)


def test_detects_each_act_family():
    md = "\n".join([
        "# CODE PENAL",
        "Article 1. - ...",
        "# LOI N° 1-98 du 31 octobre 1998 portant X",
        "Article 1. - ...",
        "# ORDONNANCE N° 62-6 du 28 juillet 1962",
        "Article 1. - ...",
        "# DÉCRET N° 2025-418 du 10 octobre 2025",
        "Article 1. - ...",
        "# ARRÊTÉ N° 4652 du 10 octobre 2025",
        "Article 1. - ...",
    ])
    types = [b["type_code"] for b in suggest_markdown_boundaries(md)]
    assert types == ["CODE", "LOI", "ORD", "DEC", "ARR"], types


def test_ignores_inline_citation_and_structure_headings():
    md = "\n".join([
        "# CODE PENAL",
        "Vu la loi n° 5-2000 du 23 février 2000 portant création du conseil ;",  # citation inline
        "# LIVRE PREMIER DES PEINES",       # structure, pas un acte
        "# TITRE SIXIEME",                  # structure
        "# CHAPITRE PREMIER",               # structure
        "Article 1. - ...",
    ])
    boundaries = suggest_markdown_boundaries(md)
    # Seul « CODE PENAL » est une borne ; ni la citation ni les niveaux structurels.
    assert [b["type_code"] for b in boundaries] == ["CODE"]
    assert boundaries[0]["start_line"] == 1


def test_ocr_tolerant_num_variants():
    md = "\n".join([
        "# ORDONNANCE No 62-6 du 28 juillet 1962",   # « No » au lieu de « N° »
        "# DECRET N0 2025-418",                       # accents absents + « N0 »
    ])
    types = [b["type_code"] for b in suggest_markdown_boundaries(md)]
    assert types == ["ORD", "DEC"], types


def test_slice_splits_and_reports_ranges():
    md = "\n".join([
        "couverture du recueil",            # 1 (avant 1re borne → ignoré)
        "# CODE PENAL",                     # 2
        "Article 1. - alpha",               # 3
        "# ORDONNANCE N° 62-6",             # 4
        "Article 1. - beta",                # 5
        "Fait à Brazzaville.",              # 6
    ])
    boundaries = suggest_markdown_boundaries(md)
    slices = slice_markdown_by_boundaries(md, boundaries)

    assert len(slices) == 2
    (code_b, code_text), (ord_b, ord_text) = slices

    assert code_b["_mibeko_split"]["line_start"] == 2
    assert code_b["_mibeko_split"]["line_end"] == 3
    assert "alpha" in code_text and "beta" not in code_text
    # La couverture (ligne 1) n'est dans aucune tranche.
    assert "couverture" not in code_text

    assert ord_b["_mibeko_split"]["type_code"] == "ORD"
    assert "beta" in ord_text and "Fait à Brazzaville." in ord_text


def test_empty_when_no_act():
    md = "# DISPOSITIONS PRELIMINAIRES\nArticle 1. - ...\n# LIVRE PREMIER\n"
    assert suggest_markdown_boundaries(md) == []
    assert slice_markdown_by_boundaries(md, []) == []


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
