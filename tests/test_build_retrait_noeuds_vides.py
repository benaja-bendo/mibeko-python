"""Constructeur de la requête qui retire les divisions de sommaire vides (mibeko-dashboard#221).

`retirer_noeuds_vides` est pure : elle travaille sur le snapshot de l'API, sans base ni
réseau. Les cas reprennent le JO n° 1-2011 spécial : lignes de sommaire à points de
conduite (divisions sans article), vraies divisions qui n'ont aucun article direct mais
des descendants qui en portent, et la ligne « LIVRE IX . . . . » du sommaire qui abrite
le vrai chapitre préliminaire, donc non vide : elle se désigne, elle ne se détecte pas.
"""

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from build_retrait_noeuds_vides import (  # noqa: E402
    CibleInvalide,
    construire_requete,
    construire_requete_retour,
    retirer_noeuds_vides,
)

DOC = "4b4c6a21-737f-45de-bfa7-c369f5aafcda"
PDF = {"filename": "congo-jo-2011-1.pdf", "sha256": "a" * 64, "size": 1}


def noeud(cle, type_, ordre, parent=None, numero=None):
    return {"key": cle, "id": cle, "parent": parent, "type": type_, "number": numero, "title": f"{type_} {numero}", "order": ordre}


def article(numero, ordre, parent=None, contenu="texte"):
    return {"id": f"a-{numero}", "number": numero, "parent": parent, "order": ordre, "content": contenu, "source_locator": {"page": 1}}


def cible():
    """Un JO réduit : sommaire (3 lignes vides + le Livre IX piégé), puis le corps."""
    return {
        "schema_version": 1,
        "document_id": DOC,
        "source_pdf": PDF,
        "nodes": [
            # Sommaire : lignes à points de conduite, sans aucun article.
            noeud("toc-livre-1", "LIVRE", 1, numero="I"),
            noeud("toc-chap-1", "CHAPITRE", 2, numero="1"),
            noeud("toc-livre-9", "LIVRE", 3, numero="IX"),          # piégé : abrite le vrai chapitre
            noeud("chap-prelim", "CHAPITRE", 4, parent="toc-livre-9", numero="PRELIM"),
            # Corps : de vraies divisions.
            noeud("livre-1", "LIVRE", 10, numero="I"),
            noeud("titre-1", "TITRE", 11, parent="livre-1", numero="1"),    # sans article direct
            noeud("chap-1", "CHAPITRE", 12, parent="titre-1", numero="1"),  # porte les articles
            noeud("livre-2", "LIVRE", 20, numero="II"),                      # sans aucun article : vide
        ],
        "articles": [
            article("1", 5, parent="chap-prelim"),
            article("2", 13, parent="chap-1"),
            article("3", 14, parent="chap-1"),
            article("PREAMBULE", 0, parent=None),
        ],
    }


def cles(c):
    return [n["key"] for n in c["nodes"]]


def test_retire_les_lignes_de_sommaire_vides_et_le_livre_vide():
    nouvelle, rapport = retirer_noeuds_vides(cible(), dissoudre=["toc-livre-9"])

    assert cles(nouvelle) == ["chap-prelim", "livre-1", "titre-1", "chap-1"]
    assert rapport["noeuds_avant"] == 8
    assert rapport["noeuds_apres"] == 4
    assert rapport["noeuds_retires"] == 4
    assert rapport["dont_vides"] == 3
    assert rapport["dont_dissous_non_vides"] == 1


def test_une_division_sans_article_direct_mais_avec_des_descendants_qui_en_portent_reste():
    nouvelle, _ = retirer_noeuds_vides(cible(), dissoudre=["toc-livre-9"])

    assert "titre-1" in cles(nouvelle)
    assert "livre-1" in cles(nouvelle)


def test_le_noeud_non_vide_ne_se_retire_que_designe():
    nouvelle, rapport = retirer_noeuds_vides(cible())

    # Sans désignation, le Livre IX piégé reste : on ne retire pas une division à cause de sa typographie.
    assert "toc-livre-9" in cles(nouvelle)
    assert "chap-prelim" in cles(nouvelle)
    assert rapport["dont_dissous_non_vides"] == 0


def test_le_fils_du_noeud_dissous_remonte_d_un_niveau():
    nouvelle, rapport = retirer_noeuds_vides(cible(), dissoudre=["toc-livre-9"])

    prelim = next(n for n in nouvelle["nodes"] if n["key"] == "chap-prelim")
    assert prelim["parent"] is None
    assert rapport["noeuds_remontes"] == 1


def test_le_fils_remonte_chez_le_premier_ancetre_conserve():
    c = cible()
    c["nodes"].append(noeud("enveloppe", "PARTIE", 30, numero="A"))
    c["nodes"][c["nodes"].index(next(n for n in c["nodes"] if n["key"] == "toc-livre-9"))]["parent"] = "enveloppe"

    nouvelle, _ = retirer_noeuds_vides(c, dissoudre=["toc-livre-9"])

    prelim = next(n for n in nouvelle["nodes"] if n["key"] == "chap-prelim")
    assert prelim["parent"] == "enveloppe"


def test_deux_noeuds_dissous_en_chaine_remontent_jusqu_a_l_ancetre_conserve():
    c = cible()
    c["nodes"].append(noeud("milieu", "SECTION", 6, parent="toc-livre-9"))
    for n in c["nodes"]:
        if n["key"] == "chap-prelim":
            n["parent"] = "milieu"

    nouvelle, _ = retirer_noeuds_vides(c, dissoudre=["toc-livre-9", "milieu"])

    prelim = next(n for n in nouvelle["nodes"] if n["key"] == "chap-prelim")
    assert prelim["parent"] is None


def test_la_designation_accepte_l_id_comme_la_cle():
    c = cible()
    for n in c["nodes"]:
        if n["key"] == "toc-livre-9":
            n["key"] = "cle-interne"
            n["id"] = "uuid-du-livre-9"
        if n["parent"] == "toc-livre-9":
            n["parent"] = "cle-interne"

    nouvelle, rapport = retirer_noeuds_vides(c, dissoudre=["uuid-du-livre-9"])

    assert "cle-interne" not in cles(nouvelle)
    assert rapport["dont_dissous_non_vides"] == 1


def test_articles_contenus_et_ordres_restent_strictement_identiques():
    avant = cible()
    nouvelle, rapport = retirer_noeuds_vides(copy.deepcopy(avant), dissoudre=["toc-livre-9"])

    assert nouvelle["articles"] == avant["articles"]
    assert rapport["articles"] == 4 and rapport["articles_retires"] == 0
    ordres = [n["order"] for n in nouvelle["nodes"]] + [a["order"] for a in nouvelle["articles"]]
    assert len(ordres) == len(set(ordres)), "les ordres restent uniques"


def test_la_cible_d_entree_n_est_pas_modifiee():
    c = cible()
    reference = copy.deepcopy(c)

    retirer_noeuds_vides(c, dissoudre=["toc-livre-9"])

    assert c == reference


def test_aucun_article_ne_perd_son_parent():
    nouvelle, _ = retirer_noeuds_vides(cible(), dissoudre=["toc-livre-9"])

    restantes = set(cles(nouvelle))
    assert all(a["parent"] is None or a["parent"] in restantes for a in nouvelle["articles"])


def test_refuse_si_tous_les_noeuds_sont_vides():
    c = cible()
    c["articles"] = [article("PREAMBULE", 0, parent=None)]

    with pytest.raises(CibleInvalide, match="reconstruction"):
        retirer_noeuds_vides(c)


def test_refuse_un_noeud_a_dissoudre_qui_porte_des_articles_directs():
    with pytest.raises(CibleInvalide, match="directement"):
        retirer_noeuds_vides(cible(), dissoudre=["chap-1"])


def test_refuse_un_noeud_a_dissoudre_inconnu():
    with pytest.raises(CibleInvalide, match="n'existe pas"):
        retirer_noeuds_vides(cible(), dissoudre=["fantome"])


def test_refuse_quand_rien_n_est_a_retirer():
    c = cible()
    c["nodes"] = [n for n in c["nodes"] if n["key"] not in ("toc-livre-1", "toc-chap-1", "livre-2")]
    c["nodes"] = [n for n in c["nodes"] if n["key"] != "toc-livre-9"]
    for n in c["nodes"]:
        if n["key"] == "chap-prelim":
            n["parent"] = None

    with pytest.raises(CibleInvalide, match="rien à retirer"):
        retirer_noeuds_vides(c)


def test_refuse_un_arbre_incoherent():
    doublon = cible()
    doublon["nodes"].append(noeud("livre-1", "LIVRE", 99))
    with pytest.raises(CibleInvalide, match="non uniques"):
        retirer_noeuds_vides(doublon)

    orphelin = cible()
    orphelin["nodes"].append(noeud("perdu", "CHAPITRE", 98, parent="n-existe-pas"))
    with pytest.raises(CibleInvalide, match="n'existe pas"):
        retirer_noeuds_vides(orphelin)


# --- la requête construite à partir de la réponse de l'API -----------------------------------

def donnees(c=None, empreinte="e" * 64):
    return {"expected_fingerprint": empreinte, "semantic_fingerprint": "s" * 64, "target": c or cible(), "counts": {}}


MOTIF = "Retirer les lignes de sommaire prises pour des divisions (dashboard#221)"


def test_la_requete_est_une_simulation_avec_l_empreinte_du_snapshot():
    requete, _ = construire_requete(donnees(), DOC, MOTIF, ["toc-livre-9"])

    assert requete["execute"] is False
    assert requete["expected_fingerprint"] == "e" * 64
    assert requete["motif"] == MOTIF
    assert cles(requete["target"]) == ["chap-prelim", "livre-1", "titre-1", "chap-1"]
    assert "confirm_deletions" not in requete


def test_refuse_un_snapshot_d_un_autre_document():
    with pytest.raises(CibleInvalide, match="ne concerne pas ce document"):
        construire_requete(donnees(), "11111111-1111-1111-1111-111111111111", MOTIF, ["toc-livre-9"])


@pytest.mark.parametrize("motif", ["court", "x" * 1001, "   " + "a" * 5 + "   "])
def test_refuse_un_motif_hors_bornes(motif):
    with pytest.raises(CibleInvalide, match="motif"):
        construire_requete(donnees(), DOC, motif, ["toc-livre-9"])


def test_le_retour_arriere_prend_la_cible_d_avant_et_l_empreinte_d_apres():
    avant = donnees()
    apres_cible, _ = retirer_noeuds_vides(cible(), ["toc-livre-9"])
    apres = donnees(apres_cible, empreinte="f" * 64)

    requete = construire_requete_retour(avant, apres, DOC, MOTIF)

    assert requete["execute"] is False
    assert requete["expected_fingerprint"] == "f" * 64
    assert requete["target"] == avant["target"]
    assert len(requete["target"]["nodes"]) == 8


def test_le_retour_arriere_refuse_deux_snapshots_identiques():
    avant = donnees()

    with pytest.raises(CibleInvalide, match="même empreinte"):
        construire_requete_retour(avant, donnees(), DOC, MOTIF)
