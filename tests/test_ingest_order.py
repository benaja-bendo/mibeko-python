"""
Tests de assign_dfs_order : l'ordre d'affichage doit être GLOBAL (pré-ordre DFS)
sur tout le document, pas seulement au sein d'un groupe de frères.

Régression : avant le correctif, ordre_affichage/sort_order repartaient à 0 sur
chaque branche, donc un listing à plat `ORDER BY ordre_affichage` ressortait
mélangé (collisions d'ordre entre branches).

Exécutable sans dépendance externe :  python3 tests/test_ingest_order.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.main import assign_dfs_order  # noqa: E402


def _sample_hierarchy():
    return [
        {"type": "LIVRE", "number": "I", "children": [
            {"type": "ARTICLE", "number": "1", "children": []},
            {"type": "CHAPITRE", "number": "1", "children": [
                {"type": "ARTICLE", "number": "2", "children": []},
                {"type": "ARTICLE", "number": "3", "children": []},
            ]},
        ]},
        {"type": "LIVRE", "number": "II", "children": [
            {"type": "ARTICLE", "number": "4", "children": []},
        ]},
    ]


def _flatten(nodes):
    out = []
    for node in nodes:
        out.append(node)
        out.extend(_flatten(node.get("children", [])))
    return out


def test_order_is_global_preorder():
    """Chaque nœud reçoit un _order unique, croissant en pré-ordre DFS."""
    hierarchy = _sample_hierarchy()
    assign_dfs_order(hierarchy)

    flat = _flatten(hierarchy)
    orders = [n["_order"] for n in flat]

    # 7 nœuds, ordres 0..6 dans l'ordre de lecture (parent avant enfants).
    assert orders == [0, 1, 2, 3, 4, 5, 6], f"obtenu {orders}"


def test_orders_are_globally_unique():
    """Aucune collision d'ordre entre branches (le bug d'origine)."""
    hierarchy = _sample_hierarchy()
    assign_dfs_order(hierarchy)

    orders = [n["_order"] for n in _flatten(hierarchy)]
    assert len(orders) == len(set(orders)), f"collisions: {orders}"


def test_articles_flatten_in_reading_order():
    """Trier les articles par _order rend l'ordre de lecture du document."""
    hierarchy = _sample_hierarchy()
    assign_dfs_order(hierarchy)

    articles = [n for n in _flatten(hierarchy) if n["type"] == "ARTICLE"]
    by_order = sorted(articles, key=lambda n: n["_order"])
    numbers = [n["number"] for n in by_order]

    assert numbers == ["1", "2", "3", "4"], f"obtenu {numbers}"


def test_parent_precedes_children_and_next_sibling():
    """Un parent précède ses enfants, qui précèdent le frère suivant du parent."""
    hierarchy = _sample_hierarchy()
    assign_dfs_order(hierarchy)

    livre_i = hierarchy[0]
    chapitre = livre_i["children"][1]
    art_in_chapitre = chapitre["children"][0]
    livre_ii = hierarchy[1]

    assert livre_i["_order"] < chapitre["_order"] < art_in_chapitre["_order"]
    assert art_in_chapitre["_order"] < livre_ii["_order"]


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
