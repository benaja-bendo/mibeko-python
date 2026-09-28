"""Tire les amendements de la loi n° 6-96 de son texte officiel (OIT/NATLEX COG-43085, Mistral OCR).

La loi 6-96 réécrit des articles du Code (« Article 39 nouveau »), en crée (« Article 39-2 »)
et en abroge (« Article 264 nouveau : Les articles 55 alinéas 4, 5, 6, 7 et 172 … sont
abrogés »). Son article 3 fixe la date d'effet : « La présente loi qui prend effet à compter de
la date de signature » — le 6 mars 1996. La date n'est donc pas déduite, elle est écrite.

Chaque amendement est comparé à la consolidation Droit-Afrique (da_structure.json) : l'accord
des deux sources valide le texte ; un désaccord part en relecture (rapport_amendements_6_96.md).
Le texte retenu est celui de la loi officielle.

Sorties : amendements_6_96.json + rapport_amendements_6_96.md.  python3 construire_amendements_6_96.py
"""

import difflib
import json
import re
from collections import Counter
from pathlib import Path

ICI = Path(__file__).parent
DATE_EFFET = "1996-03-06"
ENTETE = re.compile(
    r"^\s*(?:#+\s*)?(?:\*\*)?\s*Article\s+(\d+(?:-\d+)?(?:\s*bis)?|1er)"
    r"(?:\s*-\s*paragraphe\s+(\d+))?\s*(\(?nouveaux?\)?)?\s*(?:\*\*)?\s*:\s*(?:\*\*)?\s*(.*)$",
    re.I,
)
# Arbitrages sur l'image (27-28/09/2026) : chaque écart OCR ↔ consolidation a été tranché en
# lisant le scan officiel. Seules les erreurs de l'OCR sont corrigées ici ; partout ailleurs la
# lecture OCR était conforme à l'image et c'est la consolidation qui divergeait (omissions,
# modernisations « francs CFA » → « FCFA », coquilles). Les fautes d'orthographe du texte
# officiel lui-même (« régistres », « recidive », « obstention », « liciement », « concervé »)
# sont conservées : on reproduit la lettre de la loi.
ARBITRAGES = {
    "47-5": [("des fins invoqués", "des faits invoqués", "mot surimprimé sur le scan (p. 7) ; lu « faits » d'après le contexte et la consolidation")],
    "47-6": [("que sont réservés de", "que sous réserve de", "passage surimprimé (p. 7) ; « sous réserve » d'après le contexte et la consolidation")],
    "73-7": [("qui permet le contrat", "qui rompt le contrat", "lu sur l'image (p. 11)"),
             ("proposer au qui-ci", "proposer à celui-ci", "lu sur l'image (p. 11)")],
    "73-8": [("Le transporteur de travail temporaire est tenu", "Tout entrepreneur de travail temporaire est tenu",
              "début de l'article effacé sur le scan officiel (p. 11) ; repris de la consolidation"),
             ("de sa part, le paiement:", "de sa part, le payement:", "lettre de la loi (p. 11)"),
             ("sont issues de fournir", "sont tenues de fournir", "lu sur l'image (p. 11)")],
    "176": [("En cas de licitement irrégulier", "En cas de liciement irrégulier", "lettre de la loi, coquille comprise (p. 21)")],
    "248-6": [("intégralement concerné", "intégralement concervé", "lettre de la loi, coquille comprise (p. 27)")],
    "257": [("quel conque", "quelconque", "mot coupé en fin de ligne (p. 32)")],
    "232": [("Mention de cette délivrance, des dates et de son heure", "Mention de cette délivrance, de sa date et de son heure",
             "lu sur l'image (p. 25 : « desa date »)")],
}
# Divergences notées sans correction : la loi est reproduite telle quelle.
NOTES = {
    "73": "la loi renvoie à « l'article 32-3 » (la liste des cas de recours est à l'article 32-2) ; lettre de la loi conservée",
    "143": "« Article 143 nouveau » remplace l'article entier ; la seconde phrase que garde la consolidation n'est pas dans la loi",
    "47-16": "la consolidation ajoute une note d'éditeur (« NB suite à l'insertion… ») absente de la loi",
}
# Articles que la consolidation attribue à la loi 6-96 mais que la loi ne contient pas.
FANTOMES = {
    "252-2": "doublon mal numéroté de l'art. 259-2 nouveau (même texte) ; aucun « Article 252-2 » dans la loi (p. 29-30)",
    "55": "pas d'« Article 55 nouveau » : la loi abroge seulement ses alinéas 4 à 7 (art. 264 nouveau)",
}

# Intitulés d'article perdus par l'OCR, rétablis sur l'image : (numéro, début exact du 1er alinéa).
INTITULES_PERDUS = [("73-8", "Le transporteur de travail temporaire est tenu, à tout moment, de justifier d'une caution")]
INTITULE = re.compile(r"^\s*(?:#+\s*)?(?:\*\*)?\s*(TITRE|CHAPITRE|SECTION|PARAGRAPHE|SOUS-SECTION)\b", re.I)
PAGE = re.compile(r"^\s*(?:\[\[MIBEKO_PAGE:\d+\]\]|-\s*\d+\s*-|\.{2,}\s*/+\s*\.*)\s*$")
MARQUE = re.compile(r"^\s*(?:\((?:Loi|loi) n°\s*[\d/-]+\)|Abrog[ée]s?\s*\((?:Loi|loi) n°\s*[\d/-]+\))\s*")


def nettoyer(lignes: list) -> str:
    texte = "\n".join(l for l in lignes if not PAGE.match(l)).replace("**", "")
    # Césure de fin de ligne (« administra-/tion ») : recollée, sauf les composés « ceux-ci », « celle-là ».
    texte = re.sub(r"([A-Za-zÀ-ÿ]+)-[ \t]*\n\s*([a-zà-ÿ]+)",
                   lambda m: m[1] + ("-" if m[2] in ("ci", "là") else "") + m[2], texte)
    paras = [re.sub(r"\s*\n\s*", " ", p).strip() for p in re.split(r"\n\s*\n", texte)]
    # Numéros de page ou marques « //… » de fin de page lus comme un alinéa (« 6 - », « 22 », « 11 »).
    return "\n\n".join(p for p in paras if p and not re.fullmatch(r"[-\s]*\d{1,2}[-\s]*", p))


def arbitrer(numero: str, texte: str) -> tuple:
    """Applique les arbitrages sur l'image ; échoue bruyamment si un passage attendu manque."""
    faits = []
    for avant, apres, motif in ARBITRAGES.get(numero, []):
        if avant not in texte:
            raise ValueError(f"art. {numero} : passage à arbitrer introuvable : {avant!r}")
        texte = texte.replace(avant, apres, 1)
        faits.append({"avant": avant, "apres": apres, "motif": motif})
    return texte, faits


def mots(texte: str) -> list:
    return re.findall(r"[\wà-ÿœ']+", (texte or "").replace("’", "'").lower())


def ecarts(officiel: str, consolidation: str) -> list:
    """Passages où la lecture OCR de la loi officielle et la consolidation divergent, mot à mot."""
    a, b = mots(officiel), mots(consolidation)
    return [{"loi_ocr": " ".join(a[i1:i2]), "consolidation": " ".join(b[j1:j2])}
            for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
            if op != "equal"]


def main() -> None:
    markdown = (ICI / "loi-6-96-mistral-ocr.md").read_text(encoding="utf-8")
    articles, courant = [], None
    for ligne in markdown.split("\n"):
        perdu = next((n for n, debut in INTITULES_PERDUS if ligne.startswith(debut)), None)
        if perdu:
            courant = {"numero_loi": perdu, "nouveau": False, "paragraphe": None, "lignes": [ligne]}
            articles.append(courant)
            continue
        m = ENTETE.match(ligne)
        if m:
            courant = {"numero_loi": m.group(1).replace(" ", ""), "paragraphe": m.group(2),
                       "nouveau": bool(m.group(3)), "lignes": [m.group(4)]}
            articles.append(courant)
        elif INTITULE.match(ligne):
            courant = None
        elif courant is not None:
            courant["lignes"].append(ligne)
    for a in articles:
        a["texte"] = nettoyer(a.pop("lignes"))

    # Les articles 1er, 2 et 3 de la loi elle-même encadrent les dispositions insérées dans le Code :
    # le 1er les introduit, le 2 (dernier) abroge les dispositions contraires, le 3 fixe la date d'effet.
    propres_a_la_loi = [a for a in articles if not a["nouveau"] and "-" not in a["numero_loi"]]
    code = [a for a in articles if a not in propres_a_la_loi]

    da = [e for e in json.loads((ICI / "da_structure.json").read_text(encoding="utf-8")) if e["type"] == "article"]
    texte_da = {}
    for e in da:
        texte_da.setdefault(e["numero"], e["texte"])

    base = {a["numero"]: a for a in json.loads((ICI / "base_1975.json").read_text(encoding="utf-8"))["articles_1975"]}

    amendements, a_relire = [], []
    for a in code:
        n = a["numero_loi"]
        effet = "modifie" if n in base else "cree"
        texte, arbitrages = arbitrer(n, a["texte"].replace("*", ""))
        d = MARQUE.sub("", texte_da.get(n, ""))
        accord = difflib.SequenceMatcher(None, mots(texte), mots(d), autojunk=False).ratio() if d else None
        incertain = "*" in texte  # Mistral OCR marque en italique ce qu'il lit mal
        amendements.append({"numero": n, "effet": effet, "loi": "6-96", "date_effet": DATE_EFFET,
                            "paragraphe_remplace": int(a["paragraphe"]) if a.get("paragraphe") else None,
                            "texte_apres": texte, "accord_consolidation": accord,
                            "lecture_incertaine": incertain, "arbitrages": arbitrages, "note": NOTES.get(n),
                            "ecarts": ecarts(texte, d) if d else []})
        if accord is None or accord < 0.97 or incertain:
            a_relire.append((n, effet, None if accord is None else round(accord, 3), incertain))

    # Abrogations de l'article 264 nouveau : articles entiers et alinéas.
    art264 = next(a for a in code if a["numero_loi"] == "264")
    abrogations = {"texte_source": art264["texte"], "articles": ["172"], "alineas": {"55": [4, 5, 6, 7]}}

    # Articles marqués « modifiés par la 6-96 » dans la consolidation, absents du texte officiel lu.
    lus = {a["numero"] for a in amendements}
    marques_da = {e["numero"] for e in da if re.match(r"\s*\((?:Loi|loi) n°\s*6[/-]96\)", e["texte"])}
    absents = sorted(marques_da - lus, key=lambda x: [int(p) for p in re.findall(r"\d+", x)])
    en_plus = sorted(lus - marques_da - {"264"}, key=lambda x: [int(p) for p in re.findall(r"\d+", x)])

    sortie = {"loi": {"reference": "Loi n° 6-96 du 6 mars 1996 modifiant et complétant certaines dispositions "
                                   "de la loi n° 45/75 du 15 mars 1975 instituant un Code du Travail",
                      "date_signature": DATE_EFFET, "date_effet": DATE_EFFET,
                      "fondement_date_effet": "article 3 : « La présente loi qui prend effet à compter de la date de signature »",
                      "source": {"manifeste": "natlex/cog-43085-loi-6-96",
                                 "url": "https://natlex.ilo.org/dyn/natlex2/natlex2/files/download/43085/COG-43085.pdf"},
                      "articles_propres": [{"numero": a["numero_loi"], "texte": a["texte"]} for a in propres_a_la_loi]},
              "amendements": amendements, "abrogations": abrogations}
    (ICI / "amendements_6_96.json").write_text(json.dumps(sortie, ensure_ascii=False, indent=1), encoding="utf-8")

    effets = Counter(a["effet"] for a in amendements)
    lignes = [
        "# Loi n° 6-96 — amendements tirés du texte officiel (mibeko-dashboard#201)", "",
        f"- Dispositions insérées dans le Code : {len(amendements)} — " + ", ".join(f"{k} : {v}" for k, v in effets.items()),
        f"- Abrogations (art. 264 nouveau) : articles {abrogations['articles']}, alinéas {abrogations['alineas']}",
        f"- Date d'effet : {DATE_EFFET} ({sortie['loi']['fondement_date_effet']})",
        f"- Accord avec la consolidation ≥ 97 % : {sum(1 for a in amendements if (a['accord_consolidation'] or 0) >= 0.97)}",
        f"- Écarts mot à mot avec la consolidation : {sum(len(a['ecarts']) for a in amendements)}, tranchés : sur l'image pour tout écart "
        "qui touche au sens (27 pages lues sur 35) ; sur les pages 2, 6, 8, 10, 12, 15, 26 et 35, non relues, les écarts ne "
        "portent que sur la forme (pluriels, accents, montants) et la lecture OCR est retenue",
        "", "## Écarts avec la consolidation", "",
        f"- Marqués « 6-96 » par la consolidation mais absents de la loi : {absents or 'aucun'}",
        *[f"  - {n} : {FANTOMES[n]}" for n in absents if n in FANTOMES],
        f"- Présents dans la loi mais non marqués par la consolidation : {en_plus or 'aucun'}",
        "", "## Erreurs de l'OCR corrigées sur l'image", "",
    ]
    for a in amendements:
        for x in a["arbitrages"]:
            lignes.append(f"- art. {a['numero']} : « {x['avant']} » → « {x['apres']} » ({x['motif']})")
    lignes += ["", "## Divergences notées, lettre de la loi conservée", ""]
    lignes += [f"- art. {n} : {t}" for n, t in NOTES.items()]
    lignes += ["", "## Ailleurs, la lecture OCR était conforme à l'image : ce que la consolidation avait de différent", "",
               "Omissions de phrases entières : 73-2, 73-13, 141-3, 173-2, 179, 260 ; mots déformés (« hommages », "
               "« moins » pour « mois », « 1577 », « consultation » pour « contestation », « modification » pour "
               "« notification ») ; montants modernisés (« francs CFA » → « FCFA ») ; phrases ajoutées (92, 143) ; "
               "note d'éditeur (47-16).",
               "", "Scores d'accord < 97 % (pour mémoire) : "
               + ", ".join(f"{n} ({r})" for n, _, r, _ in a_relire)]
    (ICI / "rapport_amendements_6_96.md").write_text("\n".join(lignes) + "\n", encoding="utf-8")
    print("\n".join(lignes))


if __name__ == "__main__":
    main()
