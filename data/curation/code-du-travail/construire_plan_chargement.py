"""Assemble le plan de chargement du Code du travail historique (mibeko-dashboard#201).

Entrées : base_1975.json (texte d'origine), amendements_6_96.json (loi 6-96 officielle, arbitrée),
da_structure.json (arborescence et place de chaque article). Sortie : plan_chargement.json,
que charger_texte_historique.py rejoue par l'API Laravel, à l'identique sur la copie de la
production puis en production.

Chaque article porte la liste chronologique de ses versions : `{debut, texte, loi}`.
- Article du texte d'origine : première version au 15 mars 1975, sans texte modificateur.
- Article réécrit en 1988 (33) ou abrogé en 1988 (162 à 167) : seconde version au 17 septembre
  1988, rattachée à la loi n° 22/88.
- Article réécrit ou abrogé en 1996 : seconde version au 6 mars 1996, rattachée à la loi 6-96.
- Article créé par la loi 6-96 : une seule version, au 6 mars 1996, rattachée à la loi 6-96.

La liste des articles créés vient de la loi officielle, jamais de la consolidation (qui invente
un « 252-2 »).

Loi n° 22/88 (décision du 28/09/2026, dashboard#201) : son texte n'est publié nulle part. Sur
NATLEX, elle et les lois 3/85 et 1/86 qu'elle modifie sont des « tirés à part » sans fichier
(fiches 40940 à 40942, vérifiées le 27/09). Ses effets sur le Code sont donc tirés de la
consolidation Droit-Afrique (COG-14546, source de praticien, règle 5 du projet) : art. 33 réécrit,
art. 162 à 167 abrogés. Le document de la loi est créé sans article, et la source de praticien est
nommée dans la provenance du Code. Date d'effet retenue : celle de la loi, faute de mieux (sa date
d'entrée en vigueur n'est pas connue). La phrase ajoutée à l'art. 248 reste en attente : son
origine n'est pas établie.

    python3 construire_plan_chargement.py
"""

import json
import re
from pathlib import Path

ICI = Path(__file__).parent
DEBUT_1975 = "1975-03-15"
DATE_22_88 = "1988-09-17"
DATE_6_96 = "1996-03-06"
TYPES_NOEUD = {"Titre": "TITRE", "Chapitre": "CHAPITRE", "Section": "SECTION",
               "Sous-section": "SOUS_SECTION", "Paragraphe": "PARAGRAPHE"}
TEXTE_ABROGATION_172 = "Abrogé par l'article 264 nouveau de la loi n° 6-96 du 6 mars 1996."
TEXTE_ABROGATION_22_88 = "Abrogé par la loi n° 22/88 du 17 septembre 1988."
ABROGES_22_88 = [str(n) for n in range(162, 168)]
NOTICE_22_88 = "https://natlex.ilo.org/dyn/natlex2/r/natlex/fe/details?p3_isn=40942"


def article_33_en_1988(texte_da: str) -> str:
    """Rédaction de l'art. 33 issue de la loi n° 22/88, d'après la consolidation.

    `da_structure.json` aplatit l'article sur une ligne ; le PDF de la consolidation (COG-14546,
    p. 6) le présente en un alinéa, une liste à puces de quatre vérifications, puis l'alinéa final.
    On retire la marque d'éditeur « (loi n°22/88) » et les crochets de « [Populaire] » : en 1988,
    le pays s'appelait bien République populaire du Congo, les crochets signalent seulement que le
    mot a disparu depuis. Chaque découpe est vérifiée, jamais supposée.
    """
    marque, liste, final = "(loi n°22/88) ", "Le visa n’est accordé qu’après avoir : ", " Si le visa prévu"
    if not texte_da.startswith(marque) or texte_da.count(liste) != 1 or texte_da.count(final) != 1:
        raise ValueError("art. 33 : la rédaction de 1988 n'a plus la forme attendue dans la consolidation")
    texte = texte_da[len(marque):].replace("République [Populaire] du Congo", "République Populaire du Congo")
    premier, reste = texte.split(" " + liste)
    enumeration, dernier = reste.split(final)
    items = enumeration.split(" ; ")
    if len(items) != 4 or not premier.endswith("l’ONEMO.") or not items[-1].endswith("prévu au contrat."):
        raise ValueError(f"art. 33 : {len(items)} vérifications au lieu de 4, ou alinéas inattendus")
    puces = [f"- {item} ;" for item in items[:-1]] + [f"- {items[-1]}"]
    return "\n\n".join([premier, liste.rstrip(), *puces, "Si le visa prévu" + dernier])


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
    textes_da = {e["numero"]: e["texte"] for e in da if e["type"] == "article"}
    textes_1988 = {"33": article_33_en_1988(textes_da["33"])}
    textes_1988.update({n: TEXTE_ABROGATION_22_88 for n in ABROGES_22_88})
    if textes_da["162à167"] != "Abrogés (loi n°22/88)":
        raise ValueError("art. 162 à 167 : la consolidation ne les dit plus abrogés par la loi 22/88")
    # Les versions sont clés par rang dans le journal du chargeur (`a:33:v2`) : une version de 1988
    # insérée avant une version de 1996 déjà chargée décalerait les rangs. Aucun article touché par
    # la 22/88 ne l'est par la 6-96 ; on le vérifie.
    touches_deux_fois = sorted(set(textes_1988) & (set(amendements) | {"55", "172"}))
    if touches_deux_fois:
        raise ValueError(f"articles modifiés par la 22/88 et la 6-96 : {touches_deux_fois}")

    noeuds, articles, pile, courant, deja = [], [], {}, None, set()
    rang = {"Titre": 1, "Chapitre": 2, "Section": 3, "Sous-section": 4, "Paragraphe": 4}

    def ajouter_article(numero: str) -> None:
        if numero in deja:
            return  # la consolidation porte deux fois l'art. 260 (ancienne et nouvelle rédaction)
        deja.add(numero)
        if numero in textes_1975:
            versions = [{"debut": DEBUT_1975, "texte": textes_1975[numero], "loi": None}]
            amendement = amendements.get(numero)
            if numero in textes_1988:
                versions.append({"debut": DATE_22_88, "texte": textes_1988[numero], "loi": "22-88"})
            elif numero == "172":
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
                           "date_entree_vigueur_inconnue": True,
                           # « Consolidée au » : la date du dernier texte intégré (loi 6-96), pas celle de l'import.
                           "consolidation_as_of": DATE_6_96},
                 "sources": base["document"]["sources"],
                 "provenance": {"source_url": base["document"]["sources"]["original"]["url"],
                                "fetched_at": "2025-10-25T00:00:00+00:00",
                                # Règle 5 : la source de praticien est nommée sur la page (le site
                                # affiche `autorite` sous « Source officielle »).
                                "autorite": "Secrétariat général du Gouvernement (sgg.cg) — loi n° 45-75 du 15 mars 1975 ; "
                                            "art. 33 et 162 à 167 selon la loi n° 22/88, d'après la consolidation "
                                            "Droit-Afrique (texte officiel de cette loi non publié)"}},
        "lois": {"22-88": {
            # Titre de la notice NATLEX COG-1988-L-40942 (« no » y est l'abréviation de « numéro »).
            "titre_officiel": "Loi n° 22/88 du 17 septembre 1988 portant modification de la loi n° 1/86 du 22 février "
                              "1986 remplaçant et complétant la loi n° 3/85 du 14 février 1985 portant création de "
                              "l'Office national de l'emploi et de la main-d'œuvre (ONEMO) et modification du Code du "
                              "travail",
            # Abrogée par la loi n° 7-2019 du 9 avril 2019 (ACPE, JO n° 17 du 25/04/2019, p. 303) ; ses
            # modifications du Code, elles, y restent (abroger une loi modificative ne fait pas revivre
            # ce qu'elle avait abrogé).
            "type_code": "LOI", "statut": "abroge",
            "date_signature": DATE_22_88, "date_entree_vigueur": None,
            "source": {"manifeste": "natlex/cog-14546-code-du-travail-consolide", "notice": NOTICE_22_88},
            "provenance": {"source_url": NOTICE_22_88, "fetched_at": "2026-09-27T23:05:00+00:00",
                           "autorite": "Organisation internationale du Travail (NATLEX) — notice seule, texte non "
                                       "publié ; effets sur le Code d'après la consolidation Droit-Afrique"},
            "articles": [],
        }, "6-96": {
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
            "248": "phrase sur la grève ajoutée par la consolidation, absente du scan de 1975 et de la loi 6-96 : origine à établir",
        },
    }
    (ICI / "plan_chargement.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")

    nb_versions = sum(len(a["versions"]) for a in articles)
    par_loi = {code: sum(1 for a in articles for v in a["versions"][1:] if v["loi"] == code) for code in ("22-88", "6-96")}
    print(f"{len(noeuds)} nœuds, {len(articles)} articles "
          f"({sum(1 for a in articles if a['versions'][0]['debut'] == DEBUT_1975)} de 1975, "
          f"{sum(1 for a in articles if a['versions'][0]['debut'] == DATE_6_96)} créés en 1996), "
          f"{nb_versions} versions dont {nb_versions - len(articles)} amendements "
          f"({par_loi['22-88']} de 1988, {par_loi['6-96']} de 1996)")


if __name__ == "__main__":
    main()
