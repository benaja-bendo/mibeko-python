"""
Tests de l'annotation des pages JO (build_json_page_index + annotate_markdown_with_pages).

Le découpage reste sur le markdown ; le JSON ne sert qu'à tamponner les pages PDF.
Propriété clé : un curseur avançant résout les lignes répétées d'un acte à l'autre
(« Vu la Constitution ») à la BONNE page.

Exécutable sans backend : python3 tests/test_page_annotation.py
(les modules MinIO/MinerU sont neutralisés pour pouvoir importer src.api.main.)
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _name, _attr in [("src.services.minio_service", "minio_service"),
                     ("src.services.mineru_service", "mineru_service")]:
    _mod = types.ModuleType(_name)
    setattr(_mod, _attr, object())
    sys.modules[_name] = _mod

from src.api.main import (  # noqa: E402
    annotate_markdown_with_pages,
    build_json_page_index,
)
from src.extractor.parser import LegalDocumentParser  # noqa: E402


def _line(text: str):
    return {"spans": [{"content": text}]}


JSON_PAYLOAD = {
    "pdf_info": [
        {"page_idx": 0, "para_blocks": [{"lines": [
            _line("La ministre des transports"),
            _line("Vu la Constitution ;"),
            _line("Article premier : Premier contenu de l'acte un."),
        ]}]},
        {"page_idx": 1, "para_blocks": [{"lines": [
            _line("Article 2 : Le present arrete sera publie au journal."),
            _line("La ministre des transports"),
            _line("Vu la Constitution ;"),
            _line("Article premier : Premier contenu de l'acte deux."),
        ]}]},
    ]
}


def test_build_index_pages_1based():
    index = build_json_page_index(JSON_PAYLOAD)
    pages = {text: page for text, page in index}
    assert pages["la ministre des transports"] in (1, 2)  # apparaît p.1 ET p.2
    # page_idx 0 -> page 1, page_idx 1 -> page 2
    assert index[0] == ("la ministre des transports", 1)
    assert ("article 2 : le present arrete sera publie au journal.", 2) in index


def test_cursor_resout_lignes_repetees_a_la_bonne_page():
    index = build_json_page_index(JSON_PAYLOAD)
    cursor = [0]

    act1 = "\n".join([
        "La ministre des transports",
        "Vu la Constitution ;",
        "Article premier : Premier contenu de l'acte un.",
        "Article 2 : Le present arrete sera publie au journal.",
    ])
    annotated1 = annotate_markdown_with_pages(act1, index, cursor)
    roots1 = LegalDocumentParser(text_content=annotated1).parse_hierarchy()
    pages1 = {n["number"]: n["page"] for n in roots1 if n["type"] == "ARTICLE"}
    assert pages1["premier"] == 1
    assert pages1["2"] == 2  # l'article 2 bascule en page 2

    # Acte 2 : mêmes lignes « La ministre / Vu la Constitution » mais page 2.
    act2 = "\n".join([
        "La ministre des transports",
        "Vu la Constitution ;",
        "Article premier : Premier contenu de l'acte deux.",
    ])
    annotated2 = annotate_markdown_with_pages(act2, index, cursor)
    roots2 = LegalDocumentParser(text_content=annotated2).parse_hierarchy()
    preamble2 = next(n for n in roots2 if n["type"] == "PREAMBULE")
    art2 = next(n for n in roots2 if n["type"] == "ARTICLE")
    assert preamble2["page"] == 2, f"préambule acte 2 attendu p.2, obtenu {preamble2['page']}"
    assert art2["page"] == 2


def test_sans_json_aucun_marqueur():
    # page_index vide -> annotate ne doit rien injecter (chemin sans JSON).
    content = "La ministre,\nArticle premier : Contenu."
    out = annotate_markdown_with_pages(content, [], [0])
    assert "[[MIBEKO_PAGE" not in out
    assert out == content


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
