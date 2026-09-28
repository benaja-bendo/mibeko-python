"""Découpe la consolidation Droit-Afrique (PDF texte, OIT/NATLEX COG-14546) en intitulés et articles.

Sortie : da_structure.json — liste ordonnée d'éléments
`{type: titre|article, niveau?, numero, texte, page_da}`.

Les césures de fin de ligne sont arbitrées par un vocabulaire (markdowns du pipeline + OCR
de 1975) : « dommages-/intérêts » garde son trait d'union, « appren-/tissage » le perd.

    python3 decouper_consolidation.py [dossier data/]   # défaut : $MIBEKO_DATA_DIR ou ../..
"""

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

import fitz

ICI = Path(__file__).parent
DATA = Path(sys.argv[1] if len(sys.argv) > 1 else os.getenv("MIBEKO_DATA_DIR", ICI.parents[1]))

MOT = r"[a-zà-ÿœ]+(?:-[a-zà-ÿœ]+)*"
ENTETE_PAGE = re.compile(r"^\s*(www\.Droit-Afrique\.com|Congo|Code du travail|\d+/53)\s*$")
INTITULE = re.compile(r"^\s*(Titre|Chapitre|Section|Sous-section|Paragraphe)\s+([\dIVX]+(?:er)?(?:\s*bis)?)\s*[-–.]\s*(.*)$")
ARTICLE = re.compile(r"^\s*Art\.\s*(\d+(?:-\d+)?(?:\s*bis)?(?:\s*à\s*\d+)?)\s*\.?-\s*(.*)$")


def vocabulaire() -> Counter:
    compte = Counter()
    for f in (DATA / "pipeline" / "md").glob("*.md"):
        compte.update(re.findall(MOT, f.read_text(encoding="utf-8", errors="ignore").lower()))
    ocr = ICI / "sgg-1975-mistral-ocr.md"
    if ocr.exists():
        compte.update(re.findall(MOT, ocr.read_text(encoding="utf-8").lower()))
    return compte


def main() -> None:
    voc = vocabulaire()

    def cesure(m: re.Match) -> str:
        gauche, droite = m.group(1), m.group(2)
        avec, sans = voc[f"{gauche}-{droite}".lower()], voc[f"{gauche}{droite}".lower()]
        return f"{gauche}-{droite}" if avec >= 1 and sans < avec else f"{gauche}{droite}"

    def joindre(lignes: list) -> str:
        t = "\n".join(lignes)
        t = re.sub(r"([A-Za-zÀ-ÿœ]+)-\s*\n\s*([a-zà-ÿœ]+)", cesure, t)
        t = re.sub(r"(?<![.:;!?])\n(?!\s*[-•–]\s)", " ", t)
        t = re.sub(r"\n", "\n\n", t)
        return re.sub(r"[ \t]+", " ", t).strip()

    pdf = fitz.open(DATA / "sources" / "natlex" / "COG-14546.pdf")
    lignes = [(n + 1, l) for n, page in enumerate(pdf) for l in page.get_text().split("\n")]
    lignes = [(p, l) for p, l in lignes if not ENTETE_PAGE.match(l)]
    debut = [i for i, (_, l) in enumerate(lignes) if re.match(r"\s*Titre 1\b", l)][1]  # après le sommaire

    elements, courant = [], None
    for page, ligne in lignes[debut:]:
        a, t = ARTICLE.match(ligne), INTITULE.match(ligne)
        if a:
            courant = {"type": "article", "numero": a.group(1).replace(" ", ""), "lignes": [a.group(2)], "page_da": page}
            elements.append(courant)
        elif t:
            courant = {"type": "titre", "niveau": t.group(1), "numero": t.group(2).replace(" ", ""),
                       "lignes": [t.group(3)], "page_da": page}
            elements.append(courant)
        elif courant is not None:
            courant["lignes"].append(ligne)

    for e in elements:
        e["texte"] = joindre(e.pop("lignes"))
        if e["type"] == "titre":
            e["texte"] = re.sub(r"\s+", " ", e["texte"])

    (ICI / "da_structure.json").write_text(json.dumps(elements, ensure_ascii=False, indent=1), encoding="utf-8")
    print(sum(e["type"] == "article" for e in elements), "articles,", sum(e["type"] == "titre" for e in elements), "intitulés")


if __name__ == "__main__":
    main()
