"""Constructeur de la requête `replace-extraction` (mibeko-python#45).

La fonction `construire_cible` est pure : elle apparie la structure courante d'un
brouillon de production (snapshot de l'API) à la structure reconstruite en dev, sans
base ni réseau. Les cas reprennent ceux du code minier (loi n° 1-2026) :
« Chapitre 1 » répété sous chaque titre, articles 114 et 116 à créer, fausses feuilles
DISPOSITION_N à retirer.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from build_remplacement_extraction import (  # noqa: E402
    CibleInvalide,
    construire_cible,
    parent_depuis_chemin,
)

PDF = {"filename": "congo-jo-2026-6-7.pdf", "sha256": "8" * 64, "size": 1165135}


def _courant_node(i, type_, number, parent=None, order=0, title="t"):
    return {"key": i, "id": i, "parent": parent, "type": type_, "number": number, "title": title, "order": order}


def _courant_article(i, number, parent=None, order=0, content="x", locator=None):
    return {
        "id": i, "number": number, "parent": parent, "order": order,
        "content": content, "source_locator": locator if locator is not None else {"page": 1},
    }


def _dev_node(i, type_, number, parent=None, order=0, title="t"):
    return {"id": i, "type": type_, "number": number, "title": title, "order": order, "parent": parent}


def _dev_article(number, parent=None, order=0, content="x", locator=None):
    return {
        "number": number, "parent": parent, "order": order, "content": content,
        "source_locator": locator if locator is not None else {"page": 1},
    }


def _snapshot(nodes, articles):
    return {"schema_version": 1, "document_id": "doc", "source_pdf": PDF, "nodes": nodes, "articles": articles}


def test_un_article_garde_son_id_par_son_numero_et_les_nouveaux_numeros_sont_crees():
    snap = _snapshot([], [_courant_article("a2", "2", order=1), _courant_article("a113", "113", order=2)])
    cible, rapport = construire_cible(
        snap, [], [_dev_article("2", order=1), _dev_article("113", order=2), _dev_article("114", order=3)]
    )

    par_numero = {a["number"]: a for a in cible["articles"]}
    assert par_numero["2"]["id"] == "a2"
    assert par_numero["113"]["id"] == "a113"
    assert "id" not in par_numero["114"]
    assert (rapport["articles_reutilises"], rapport["articles_crees"], rapport["articles_retires"]) == (2, 1, 0)


def test_les_articles_sans_equivalent_sont_comptes_comme_retires():
    snap = _snapshot([], [
        _courant_article("a1", "1", order=1),
        _courant_article("d1", "DISPOSITION_1", order=2),
        _courant_article("d2", "DISPOSITION_2", order=3),
    ])
    cible, rapport = construire_cible(snap, [], [_dev_article("1", order=1)])

    assert rapport["articles_retires"] == 2
    assert rapport["numeros_retires"] == ["DISPOSITION_1", "DISPOSITION_2"]
    assert [a["number"] for a in cible["articles"]] == ["1"]


def test_les_contenus_et_reperes_modifies_sont_comptes():
    snap = _snapshot([], [
        _courant_article("a1", "1", order=1, content="ancien", locator={"page": 3}),
        _courant_article("a2", "2", order=2, content="identique", locator={"page": 4}),
    ])
    _, rapport = construire_cible(snap, [], [
        _dev_article("1", order=1, content="nouveau", locator={"page": 3}),
        _dev_article("2", order=2, content="identique", locator={"page": 5}),
    ])

    assert rapport["contenus_modifies"] == 1
    assert rapport["reperes_modifies"] == 1


def test_un_chapitre_repete_sous_chaque_titre_est_apparie_dans_l_ordre():
    """« Chapitre 1 » existe sous le titre I et sous le titre II : l'alignement se
    fait sur la suite (type, numéro), jamais sur le numéro seul."""
    snap = _snapshot(
        [
            _courant_node("t1", "TITRE", "I", order=1),
            _courant_node("c1a", "CHAPITRE", "1", parent="t1", order=2),
            _courant_node("t2", "TITRE", "II", order=4),
            _courant_node("c1b", "CHAPITRE", "1", parent="t2", order=5),
        ],
        [_courant_article("a1", "1", parent="c1a", order=3), _courant_article("a2", "2", parent="c1b", order=6)],
    )
    dev = [
        _dev_node("D-t1", "TITRE", "I", order=1),
        _dev_node("D-c1a", "CHAPITRE", "1", parent="D-t1", order=2),
        _dev_node("D-t2", "TITRE", "II", order=4),
        _dev_node("D-c1b", "CHAPITRE", "1", parent="D-t2", order=5),
    ]
    cible, rapport = construire_cible(
        snap, dev, [_dev_article("1", parent="D-c1a", order=3), _dev_article("2", parent="D-c1b", order=6)]
    )

    ids = {n["key"]: n.get("id") for n in cible["nodes"]}
    assert ids == {"t1": "t1", "c1a": "c1a", "t2": "t2", "c1b": "c1b"}
    parents = {a["number"]: a["parent"] for a in cible["articles"]}
    assert parents == {"1": "c1a", "2": "c1b"}
    assert (rapport["divisions_reutilisees"], rapport["divisions_creees"], rapport["divisions_retirees"]) == (4, 0, 0)


def test_une_fausse_division_de_la_production_est_retiree_sans_decaler_les_autres():
    """« PARTIE L » (le mot « partiel ») disparaît ; les divisions voisines gardent leur id."""
    snap = _snapshot(
        [
            _courant_node("t1", "TITRE", "I", order=1),
            _courant_node("faux", "PARTIE", "l", parent="t1", order=2),
            _courant_node("c1", "CHAPITRE", "1", parent="t1", order=3),
        ],
        [_courant_article("a1", "1", parent="c1", order=4)],
    )
    dev = [
        _dev_node("D-t1", "TITRE", "I", order=1),
        _dev_node("D-c1", "CHAPITRE", "1", parent="D-t1", order=3),
    ]
    cible, rapport = construire_cible(snap, dev, [_dev_article("1", parent="D-c1", order=4)])

    assert [n.get("id") for n in cible["nodes"]] == ["t1", "c1"]
    assert rapport["divisions_retirees"] == 1


def test_une_division_nouvelle_recoit_une_cle_sans_id_et_ses_enfants_la_referencent():
    snap = _snapshot([], [])
    cible, rapport = construire_cible(
        snap,
        [_dev_node("D-t1", "TITRE", "I", order=1)],
        [_dev_article("1", parent="D-t1", order=2)],
    )

    noeud = cible["nodes"][0]
    assert "id" not in noeud and noeud["key"] == "nouveau-D-t1"
    assert cible["articles"][0]["parent"] == "nouveau-D-t1"
    assert rapport["divisions_creees"] == 1


def test_les_ordres_doivent_etre_uniques_sur_les_divisions_et_les_articles():
    with pytest.raises(CibleInvalide, match="ordres non uniques"):
        construire_cible(
            _snapshot([], []),
            [_dev_node("D-t1", "TITRE", "I", order=1)],
            [_dev_article("1", parent="D-t1", order=1)],
        )


def test_un_numero_d_article_en_double_dans_la_production_est_refuse():
    snap = _snapshot([], [_courant_article("a", "7", order=1), _courant_article("b", "7", order=2)])

    with pytest.raises(CibleInvalide, match="en double"):
        construire_cible(snap, [], [_dev_article("7", order=1)])


def test_la_cible_reprend_le_document_et_le_pdf_du_snapshot():
    snap = _snapshot([], [_courant_article("a1", "1", order=1)])
    cible, _ = construire_cible(snap, [], [_dev_article("1", order=1)])

    assert cible["schema_version"] == 1
    assert cible["document_id"] == "doc"
    assert cible["source_pdf"] == PDF


def test_le_parent_d_une_division_se_lit_dans_son_chemin_ltree():
    chemin = "n_f5cff31e_def4_4431_8b2f_9c4aeb7ad940.n_bb6c9cb9_9836_42fb_82b3_5deec43a01aa.n_8a08ae2e_0811_4bde_a908_6ea1bc973b90"

    assert parent_depuis_chemin(chemin) == "bb6c9cb9-9836-42fb-82b3-5deec43a01aa"
    assert parent_depuis_chemin("n_f5cff31e_def4_4431_8b2f_9c4aeb7ad940") is None
    assert parent_depuis_chemin("") is None
