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
# Intitulés d'article perdus par l'OCR, rétablis sur l'image : (numéro, début exact du 1er alinéa).
INTITULES_PERDUS = [("73-8", "Le transporteur de travail temporaire est tenu, à tout moment, de justifier d'une caution")]
INTITULE = re.compile(r"^\s*(?:#+\s*)?(?:\*\*)?\s*(TITRE|CHAPITRE|SECTION|PARAGRAPHE|SOUS-SECTION)\b", re.I)
PAGE = re.compile(r"^\s*(?:\[\[MIBEKO_PAGE:\d+\]\]|-\s*\d+\s*-|\.{2,}\s*/+\s*\.*)\s*$")
MARQUE = re.compile(r"^\s*(?:\((?:Loi|loi) n°\s*[\d/-]+\)|Abrog[ée]s?\s*\((?:Loi|loi) n°\s*[\d/-]+\))\s*")


def nettoyer(lignes: list) -> str:
    texte = "\n".join(l for l in lignes if not PAGE.match(l)).replace("**", "")
    paras = [re.sub(r"\s*\n\s*", " ", p).strip() for p in re.split(r"\n\s*\n", texte)]
    return "\n\n".join(p for p in paras if p)


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
        texte = a["texte"]
        d = MARQUE.sub("", texte_da.get(n, ""))
        accord = difflib.SequenceMatcher(None, mots(texte), mots(d), autojunk=False).ratio() if d else None
        incertain = "*" in texte  # Mistral OCR marque en italique ce qu'il lit mal
        amendements.append({"numero": n, "effet": effet, "loi": "6-96", "date_effet": DATE_EFFET,
                            "paragraphe_remplace": int(a["paragraphe"]) if a.get("paragraphe") else None,
                            "texte_apres": texte.replace("*", ""), "accord_consolidation": accord,
                            "lecture_incertaine": incertain,
                            "ecarts": ecarts(texte, d) if d and (accord or 0) < 0.97 else []})
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
        "", "## Écarts avec la consolidation", "",
        f"- Marqués « 6-96 » par la consolidation mais absents du texte officiel lu : {absents or 'aucun'}",
        f"- Présents dans le texte officiel mais non marqués par la consolidation : {en_plus or 'aucun'}",
        "", "## À relire (accord < 97 %, absent de la consolidation, ou lecture OCR incertaine)", "",
    ]
    par_numero = {a["numero"]: a for a in amendements}
    for n, e, r, i in a_relire:
        lignes.append(f"- art. {n} ({e}) : accord {r}{' ; OCR incertain' if i else ''}")
        for x in par_numero[n]["ecarts"][:8]:
            lignes.append(f"  - loi (OCR) « {x['loi_ocr'][:90]} » / consolidation « {x['consolidation'][:90]} »")
    (ICI / "rapport_amendements_6_96.md").write_text("\n".join(lignes) + "\n", encoding="utf-8")
    print("\n".join(lignes))


if __name__ == "__main__":
    main()
