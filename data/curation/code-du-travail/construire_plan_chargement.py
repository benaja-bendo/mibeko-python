"""Assemble le plan de chargement du Code du travail historique (mibeko-dashboard#201).

Entrées : base_1975.json (texte d'origine), amendements_6_96.json (loi 6-96 officielle, arbitrée),
da_structure.json (arborescence et place de chaque article). Sortie : plan_chargement.json,
que charger_texte_historique.py rejoue par l'API Laravel, à l'identique sur la copie de la
production puis en production.

Chaque article porte la liste chronologique de ses versions : `{debut, texte, loi}`.
- Article du texte d'origine : première version au 15 mars 1975, sans texte modificateur.
- Article réécrit ou abrogé en 1996 : seconde version au 6 mars 1996, rattachée à la loi 6-96.
- Article créé par la loi 6-96 : une seule version, au 6 mars 1996, rattachée à la loi 6-96.

La liste des articles créés vient de la loi officielle, jamais de la consolidation (qui invente
un « 252-2 »). Les effets de la loi 22-88 (art. 33 ; abrogation des art. 162 à 167) et la phrase
ajoutée à l'art. 248 attendent leur texte source : ils sont listés dans `en_attente`.

    python3 construire_plan_chargement.py
"""

import json
import re
from pathlib import Path

ICI = Path(__file__).parent
DEBUT_1975 = "1975-03-15"
DATE_6_96 = "1996-03-06"
TYPES_NOEUD = {"Titre": "TITRE", "Chapitre": "CHAPITRE", "Section": "SECTION",
               "Sous-section": "SOUS_SECTION", "Paragraphe": "PARAGRAPHE"}
TEXTE_ABROGATION_172 = "Abrogé par l'article 264 nouveau de la loi n° 6-96 du 6 mars 1996."


def numero_affiche(numero: str) -> str:
    return re.sub(r"(\d)bis$", r"\1 bis", numero)


def article_55_en_1996(texte_1975: str) -> str:
    """Retire les alinéas 4 à 7 de l'art. 55 (art. 264 nouveau de la loi 6-96).

    La liste à tirets appartient à l'alinéa « Les éléments d'appréciation… » : les alinéas 4 à 7
    de la loi sont donc les blocs 4 à 8 du texte, exactement ceux que la même loi reprend dans
    l'art. 192 bis nouveau. Les débuts attendus sont vérifiés, jamais supposés.
    """
    blocs = texte_1975.split("\n\n")
    attendus = {3: "Le caractère représentatif", 4: "Les éléments d'appréciation", 6: "La décision du Ministre",
                7: "Les dispositions qui précèdent", 8: "Si une commission mixte"}
    for rang, debut in attendus.items():
        if not blocs[rang].startswith(debut):
            raise ValueError(f"art. 55 : le bloc {rang + 1} devait commencer par « {debut} »")
    return "\n\n".join(blocs[:3] + blocs[8:])


def main() -> None:
    base = json.loads((ICI / "base_1975.json").read_text(encoding="utf-8"))
    loi = json.loads((ICI / "amendements_6_96.json").read_text(encoding="utf-8"))
    da = json.loads((ICI / "da_structure.json").read_text(encoding="utf-8"))

    textes_1975 = {a["numero"]: a["texte"] for a in base["articles_1975"]}
    amendements = {a["numero"]: a for a in loi["amendements"]}
    crees = {n for n, a in amendements.items() if a["effet"] == "cree"}

    noeuds, articles, pile, courant, deja = [], [], {}, None, set()
    rang = {"Titre": 1, "Chapitre": 2, "Section": 3, "Sous-section": 4, "Paragraphe": 4}

    def ajouter_article(numero: str) -> None:
        if numero in deja:
            return  # la consolidation porte deux fois l'art. 260 (ancienne et nouvelle rédaction)
        deja.add(numero)
        if numero in textes_1975:
            versions = [{"debut": DEBUT_1975, "texte": textes_1975[numero], "loi": None}]
            amendement = amendements.get(numero)
            if numero == "172":
                versions.append({"debut": DATE_6_96, "texte": TEXTE_ABROGATION_172, "loi": "6-96"})
            elif numero == "55":
                versions.append({"debut": DATE_6_96, "texte": article_55_en_1996(textes_1975["55"]), "loi": "6-96"})
            elif amendement and amendement["effet"] == "modifie":
                versions.append({"debut": DATE_6_96, "texte": amendement["texte_apres"], "loi": "6-96"})
        elif numero in crees:
            versions = [{"debut": DATE_6_96, "texte": amendements[numero]["texte_apres"], "loi": "6-96"}]
        else:
            return  # présent dans la consolidation seulement (ex. « 252-2 ») : n'existe pas dans la loi
        articles.append({"cle": f"a:{numero}", "numero": numero_affiche(numero), "noeud": courant,
                         "ordre": len(articles), "versions": versions})

    for element in da:
        if element["type"] == "titre":
            niveau = rang[element["niveau"]]
            parent = next((pile[n] for n in range(niveau - 1, 0, -1) if n in pile), None)
            cle = f"n:{len(noeuds)}"
            noeuds.append({"cle": cle, "parent": parent, "type_unite": TYPES_NOEUD[element["niveau"]],
                           "numero": element["numero"], "titre": element["texte"], "ordre": len(noeuds)})
            pile = {k: v for k, v in pile.items() if k < niveau}
            pile[niveau] = courant = cle
        elif element["numero"] == "162à167":
            for n in range(162, 168):
                ajouter_article(str(n))
        else:
            ajouter_article(element["numero"])

    manquants_1975 = sorted(set(textes_1975) - deja, key=int)
    manquants_crees = sorted(crees - deja)
    if manquants_1975 or manquants_crees:
        raise ValueError(f"articles non placés dans l'arborescence : 1975 {manquants_1975}, 1996 {manquants_crees}")

    propres = {a["numero"]: a["texte"] for a in loi["loi"]["articles_propres"]}
    plan = {
        "schema": 1,
        # La loi 45-75 dit seulement « sera exécutée comme loi de l'État » : sa date d'entrée en
        # vigueur n'est pas connue. On le déclare, plutôt que d'en inventer une.
        "code": {"patch": {"titre_officiel": "Code du travail", "date_signature": DEBUT_1975, "statut": "vigueur",
                           "date_entree_vigueur_inconnue": True},
                 "sources": base["document"]["sources"],
                 "provenance": {"source_url": base["document"]["sources"]["original"]["url"],
                                "fetched_at": "2025-10-25T00:00:00+00:00",
                                "autorite": "Secrétariat général du Gouvernement (sgg.cg) — loi n° 45-75 du 15 mars 1975"}},
        "lois": {"6-96": {
            "titre_officiel": loi["loi"]["reference"], "type_code": "LOI", "statut": "vigueur",
            "date_signature": DATE_6_96, "date_entree_vigueur": DATE_6_96,
            "source": loi["loi"]["source"],
            "provenance": {"source_url": loi["loi"]["source"]["url"], "fetched_at": "2026-09-27T21:05:00+00:00",
                           "autorite": "Organisation internationale du Travail (NATLEX) — texte officiel de la loi n° 6-96"},
            "articles": [{"numero": "1er", "texte": propres["1er"]}, {"numero": "2", "texte": propres["2"]},
                         {"numero": "3", "texte": propres["3"]}],
        }},
        "noeuds": noeuds,
        "articles": articles,
        "en_attente": {
            "22-88": {"modifie": ["33"], "abroge": ["162", "163", "164", "165", "166", "167"],
                      "motif": "texte de la loi n° 22/88 du 17 septembre 1988 non encore obtenu (NATLEX p_isn=40942)"},
            "248": "phrase sur la grève ajoutée par la consolidation, absente du scan de 1975 et de la loi 6-96 : origine à établir",
        },
    }
    (ICI / "plan_chargement.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")

    nb_versions = sum(len(a["versions"]) for a in articles)
    print(f"{len(noeuds)} nœuds, {len(articles)} articles "
          f"({sum(1 for a in articles if a['versions'][0]['debut'] == DEBUT_1975)} de 1975, "
          f"{sum(1 for a in articles if a['versions'][0]['debut'] == DATE_6_96)} créés en 1996), "
          f"{nb_versions} versions dont {nb_versions - len(articles)} amendements de 1996")


if __name__ == "__main__":
    main()
