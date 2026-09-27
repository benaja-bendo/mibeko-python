"""Découpe la lecture Mistral OCR du scan SGG de 1975 (sgg-1975-mistral-ocr.md) en articles.

Sortie : sgg-1975-mistral-articles.json — `{order: [...], arts: {numero: {numero, page, texte}}}`.
Un numéro lu deux fois est suffixé `_dup` (cas connu : « ARTICLE 35 » lu pour le 36, rectifié
par construire_base_1975.py). Les intitulés coupent l'article en cours.

    python3 decouper_ocr_1975.py
"""

import json
import re
from pathlib import Path

ICI = Path(__file__).parent
ARTICLE = re.compile(
    r"^\s*(?:#+\s*)?(?:\*\*)?\s*ART[I1l]CLE\s+(PREMIER|1er|\d+)\s*(?:\*\*)?\s*[:.\-–]?\s*(?:\*\*)?\s*(.*)$", re.I
)
INTITULE = re.compile(r"^\s*(?:#+\s*)?(?:\*\*)?\s*(TITRE|CHAPITRE|SECTION|PARAGRAPHE|SOUS-SECTION)\b", re.I)
PAGE = re.compile(r"\[\[MIBEKO_PAGE:(\d+)\]\]")


def main() -> None:
    markdown = (ICI / "sgg-1975-mistral-ocr.md").read_text(encoding="utf-8")
    page, arts, ordre, courant = 1, {}, [], None
    for ligne in markdown.split("\n"):
        p = PAGE.search(ligne)
        if p:
            page = int(p.group(1))
            continue
        a = ARTICLE.match(ligne)
        if a:
            numero = "1" if a.group(1).lower() in ("premier", "1er") else a.group(1)
            if numero in arts:
                numero += "_dup"
            courant = {"numero": numero, "page": page, "lignes": [a.group(2)]}
            arts[numero] = courant
            ordre.append(numero)
            continue
        if INTITULE.match(ligne):
            courant = None
            continue
        if courant is not None:
            courant["lignes"].append(ligne)
    for a in arts.values():
        a["texte"] = re.sub(r"\n{3,}", "\n\n", "\n".join(a.pop("lignes")).strip())
    (ICI / "sgg-1975-mistral-articles.json").write_text(
        json.dumps({"order": ordre, "arts": arts}, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(len(ordre), "articles ; doublons :", [n for n in ordre if n.endswith("_dup")])


if __name__ == "__main__":
    main()
