"""Construit la base historique du Code du travail (mibeko-dashboard#201).

Chaîne complète, dans l'ordre (chaque étape est rejouable) :
  ocr_mistral.py → decouper_ocr_1975.py → decouper_consolidation.py → construire_base_1975.py

Entrées (toutes dans ce dossier, sauf mention) :
- sgg-1975-mistral-articles.json : lecture Mistral OCR du scan officiel SGG (loi 45-75, 1975) ;
- da_structure.json : consolidation Droit-Afrique (à jour des lois 22-88 et 6-96), découpée ;
- minerU_1975.json : lecture MinerU du même scan (export du brouillon de prod, 27/09/2026).

Règles (décisions du 27/09/2026, docs/decisions.md) :
1. Article jamais modifié selon la consolidation : son texte de 1975 est celui de la
   consolidation (propre), SAUF sur les passages où les deux OCR indépendants du scan
   officiel concordent contre elle — l'original officiel gagne alors (liste `desaccords`).
2. Article modifié ou abrogé ensuite : son texte de 1975 ne peut venir que du scan
   (Mistral, contrôlé par MinerU ; relecture visuelle quand ils divergent).
3. L'amendement (texte après, loi, effet) est porté à part, pour être appliqué en version.

Sortie : base_1975.json + rapport_base_1975.md. Rejouable : python3 construire_base_1975.py
"""

import difflib
import json
import re
from collections import Counter
from pathlib import Path

ICI = Path(__file__).parent

MARQUE = re.compile(
    r"^\s*(?:\((?:Loi|loi) n°\s*(\d+)[/-](\d+)\)|Abrog[ée]s?\s*\((?:Loi|loi) n°\s*(\d+)[/-](\d+)\))\s*"
)

# Transcriptions visuelles (scan illisible pour les deux OCR, ou OCR inventif).
TRANSCRIPTIONS = {
    "175": (
        "Un arrêté du Ministre du Travail et de la Prévoyance Sociale pris après avis de la "
        "Commission Nationale Consultative du Travail fixe l'effectif minimum des travailleurs "
        "permanents à partir duquel des droits et prérogatives prévus par le présent code sont "
        "reconnus aux membres des bureaux syndicaux en matière d'éducation ouvrière et d'activité "
        "syndicale. Il détermine également les conditions dans lesquelles les membres des bureaux "
        "syndicaux exerceront leur mission dans l'entreprise.",
        "lu sur l'image, page 50",
    ),
    "176": (
        "Tout licenciement d'un membre du bureau syndical d'entreprise et de base envisagé par "
        "l'employeur ou son représentant doit être soumis à la décision de la Commission de litiges "
        "prévue à l'article 59.\n\n"
        "Toutefois, en cas de faute présumée lourde par l'employeur, celui-ci peut prononcer "
        "immédiatement la mise à pied provisoire du membre du bureau syndical en attendant la "
        "décision définitive de la Commission de litiges. Cette mise à pied n'entraîne pas "
        "suspension du paiement du salaire de base.\n\n"
        "Tout membre du bureau syndical s'estimant abusivement licencié saisit immédiatement le "
        "Tribunal du Travail qui cite sans délai les parties à comparaître.\n\n"
        "Pendant la procédure judiciaire, le membre du bureau syndical conserve le bénéfice de son "
        "salaire de base, sauf lorsque la Commission de litiges reconnaissant la faute lourde, "
        "décide la suspension du versement du salaire de base jusqu'au prononcé du jugement.\n\n"
        "En cas de licenciement reconnu abusif, le Tribunal ordonne à compter du prononcé du "
        "jugement, soit la réintégration du membre du bureau syndical dans ses fonctions au sein "
        "de l'entreprise, soit à titre de dommages-intérêts, le versement à son profit, à échéance "
        "mensuelle, du salaire de base pendant une durée de deux ans, sauf si à l'intérieur de "
        "cette période, l'intéressé exerce ou retrouve une activité lucrative.\n\n"
        "Dans le cas où la Commission de litiges aura décidé le maintien du versement du salaire "
        "pendant la procédure judiciaire, celui-ci reste acquis, quelle que soit l'issue du "
        "procès.\n\n"
        "Toutes les garanties ci-dessus sont applicables aux anciens membres du bureau syndical "
        "pendant une durée de six (6) mois à partir de l'expiration du mandat.",
        "lu sur l'image, pages 50-51 ; renvoi « article 59 » : chiffre dégradé, à confirmer",
    ),
}

# Corrections ponctuelles de la lecture Mistral, relues sur l'image.
CORRECTIONS_OCR = {
    "33": [("qu'en sortie", "ou sa sortie"), ("l'installat ", "l'installation ")],
}

# Pages du scan illisibles : l'écart avec la consolidation y vient de l'OCR, pas du droit.
PAGES_ILLISIBLES = {"24", "61", "105", "106", "108", "109", "111", "113", "119", "135"}


def marque(texte: str):
    m = MARQUE.match(texte or "")
    if not m:
        return None
    if m.group(1):
        return ("modifie", f"{m.group(1)}-{m.group(2)}")
    return ("abroge", f"{m.group(3)}-{m.group(4)}")


def nettoyer_ocr(texte: str) -> str:
    t = texte.replace("**", "")
    t = re.sub(r"^\s*\.\./?\s*$", "", t, flags=re.M)
    t = re.sub(r"^\s*\d{1,2}\s*$", "", t, flags=re.M)
    t = re.sub(r"-\s*\n\s*(?=[a-zà-ÿ])", "", t)
    paras = [re.sub(r"\s*\n\s*", " ", p).strip() for p in re.split(r"\n\s*\n", t)]
    return "\n\n".join(p for p in paras if p)


def mots(texte: str) -> list:
    t = re.sub(r"-\s*\n\s*", "", texte or "").replace("’", "'").lower()
    return re.findall(r"[\wà-ÿœ']+", t)


def ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, mots(a), mots(b), autojunk=False).ratio()


def desaccords_officiels(da: str, mistral: str, mineru: str) -> list:
    """Segments où les deux OCR du scan concordent entre eux contre la consolidation."""
    a, b, c = mots(da), mots(mistral), " ".join(mots(mineru))
    sortie = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        seg_ocr = " ".join(b[j1:j2])
        seg_da = " ".join(a[i1:i2])
        if seg_ocr and len(seg_ocr) > 2 and seg_ocr in c and seg_da not in c:
            sortie.append({"consolidation": seg_da, "scan_officiel": seg_ocr})
    return sortie


SIGLES = {"cfa": "CFA", "ii": "II", "iii": "III", "iv": "IV"}


def appliquer_officiel(da: str, desaccords: list) -> str:
    """Remplace dans le texte de la consolidation chaque passage contredit par le scan officiel.

    Le remplacement se fait sur les mots d'origine (casse conservée pour l'initiale), dans
    l'ordre du texte, et seulement à la première occurrence suivante — l'alignement a été
    calculé mot à mot, le texte n'est jamais réécrit au-delà du passage désigné.
    """
    texte, curseur = da, 0
    for e in desaccords:
        cible = e["consolidation"].split()
        if not cible:
            continue
        motif = r"\b" + r"[\s,;:.'’-]+".join(re.escape(m) for m in cible) + r"\b"
        m = re.compile(motif, re.IGNORECASE).search(texte, curseur)
        if not m:
            continue
        officiel = " ".join(SIGLES.get(w, w) for w in e["scan_officiel"].split())
        if m.group(0)[:1].isupper():
            officiel = officiel[:1].upper() + officiel[1:]
        texte = texte[: m.start()] + officiel + texte[m.end():]
        curseur = m.start() + len(officiel)
    return texte


def main() -> None:
    da = json.loads((ICI / "da_structure.json").read_text(encoding="utf-8"))
    mistral = json.loads((ICI / "sgg-1975-mistral-articles.json").read_text(encoding="utf-8"))["arts"]
    mistral["36"] = mistral.pop("35_dup")  # « ARTICLE 35 » lu deux fois : le second est le 36
    mineru = json.loads((ICI / "minerU_1975.json").read_text(encoding="utf-8"))

    rang = {"Titre": 1, "Chapitre": 2, "Section": 3, "Sous-section": 4, "Paragraphe": 4}
    noeuds, pile, place, texte_da, doublons_da = [], {}, {}, {}, {}
    courant = None
    for it in da:
        if it["type"] == "titre":
            niveau = rang[it["niveau"]]
            parent = next((pile[n] for n in range(niveau - 1, 0, -1) if n in pile), None)
            noeuds.append({"id": len(noeuds), "type_unite": it["niveau"], "numero": it["numero"],
                           "intitule": it["texte"], "parent": parent})
            pile = {k: v for k, v in pile.items() if k < niveau}
            pile[niveau] = courant = len(noeuds) - 1
            continue
        n = it["numero"]
        if n in texte_da:
            doublons_da[n] = it["texte"]
            continue
        texte_da[n], place[n] = it["texte"], courant
    for k in range(162, 168):
        place[str(k)] = place.get("162à167")

    articles, a_relire = [], []
    for n in (str(i) for i in range(1, 266)):
        d = texte_da.get(n) or (texte_da.get("162à167") if 162 <= int(n) <= 167 else None)
        mk = marque(d)
        if 162 <= int(n) <= 167:
            mk = ("abroge", "22-88")
        if n == "260":
            mk = ("modifie", "6-96")  # la consolidation porte l'ancienne ET la nouvelle rédaction
        if n == "248":
            mk = ("modifie", "non-attribue")  # phrase ajoutée sans marque ; absente du scan (vérifié p. 66)
        m = mistral.get(n)
        entree = {"numero": n, "noeud": place.get(n), "amendement": None, "desaccords": []}

        if n in TRANSCRIPTIONS:
            texte, controle = TRANSCRIPTIONS[n]
            entree.update(texte=texte, source="sgg-scan-transcription-visuelle", controle=controle)
        elif n in ("62", "63"):
            entree.update(texte=d, source="consolidation-da-controlee",
                          controle="absent du scan SGG (page manquante) ; non modifié depuis 1975 selon la consolidation")
        elif mk is None and m:
            r = ratio(m["texte"], d)
            ecarts = [] if n in PAGES_ILLISIBLES else desaccords_officiels(d, m["texte"], mineru.get(n, ""))
            controle = f"identique à {r:.0%} à la lecture OCR du scan SGG ; non modifié depuis 1975 selon la consolidation"
            if n in PAGES_ILLISIBLES:
                controle += " ; page du scan illisible, écart dû à l'OCR"
            if ecarts:
                controle += f" ; {len(ecarts)} passage(s) rétabli(s) selon le scan officiel"
            entree.update(texte=appliquer_officiel(d, ecarts), source="consolidation-da-controlee",
                          controle=controle, desaccords=ecarts)
            if r < 0.97 and n not in PAGES_ILLISIBLES:
                a_relire.append((n, round(r, 3)))
        else:
            texte = nettoyer_ocr(m["texte"])
            for avant, apres in CORRECTIONS_OCR.get(n, []):
                texte = texte.replace(avant, apres)
            accord = ratio(m["texte"], mineru.get(n, ""))
            entree.update(texte=texte, source="sgg-scan-ocr-mistral",
                          controle=("relu sur l'image" if n in CORRECTIONS_OCR else
                                    f"deux OCR indépendants (Mistral, MinerU) concordants à {accord:.0%}"))
            if accord < 0.95 and n not in CORRECTIONS_OCR:
                a_relire.append((n, round(accord, 3)))

        if mk:
            apres = None
            if mk[0] == "modifie":
                apres = MARQUE.sub("", texte_da[n]) if mk[1] != "non-attribue" else texte_da[n]
            entree["amendement"] = {"effet": mk[0], "loi": mk[1], "texte_apres": apres}
        articles.append(entree)

    crees = [{"numero": n, "noeud": place.get(n), "loi": (marque(t) or ("cree", "non-marque"))[1],
              "texte": MARQUE.sub("", t)}
             for n, t in texte_da.items() if not n.isdigit() and n != "162à167"]

    base = {
        "document": {
            "titre_officiel": "Code du travail",
            "reference": "Loi n° 45-75 du 15 mars 1975 instituant un code du travail de la République populaire du Congo",
            "date_publication": "1975-03-15",
            "document_role": "STOCK", "stock_code": "code-du-travail", "type_code": "CODE",
            "sources": {
                "original": {"url": "https://sgg.cg/codes/congo-code-1975-travail.pdf",
                             "sha256": "8e63d6c6e9d84efc969c0babbad3e17e089c21f16fb48e4fc7875d4c034feadb",
                             "manifeste": "sgg-codes/congo-code-1975-travail"},
                "consolidation": {"url": "https://natlex.ilo.org/dyn/natlex2/natlex2/files/download/14546/COG-14546.pdf",
                                  "sha256": "5857b7c14be14d13f3d6e0f4500092b36a293cf244c4bbec85646083c2cdeaf5",
                                  "manifeste": "natlex/cog-14546-code-du-travail-consolide",
                                  "a_jour_au": "1996-03-06"},
            },
        },
        "noeuds": noeuds,
        "articles_1975": articles,
        "articles_crees_ulterieurement": crees,
    }
    (ICI / "base_1975.json").write_text(json.dumps(base, ensure_ascii=False, indent=1), encoding="utf-8")

    sources = Counter(a["source"] for a in articles)
    effets = Counter((a["amendement"]["effet"], a["amendement"]["loi"]) for a in articles if a["amendement"])
    avec_desaccord = [a for a in articles if a["desaccords"]]
    lignes = [
        "# Code du travail — base historique de 1975 (mibeko-dashboard#201)",
        "",
        f"- Nœuds (arborescence de la consolidation) : {len(noeuds)}",
        f"- Articles de 1975 : {len(articles)} — " + ", ".join(f"{k} : {v}" for k, v in sources.items()),
        "- Amendements à appliquer : " + ", ".join(f"{e} par {l} : {v}" for (e, l), v in sorted(effets.items())),
        f"- Articles créés après 1975 : {len(crees)} — " + ", ".join(f"{k} : {v}" for k, v in Counter(c['loi'] for c in crees).items()),
        f"- Articles dont la consolidation diverge du scan officiel (deux OCR concordants) : {len(avec_desaccord)}",
        "",
        "## Passages où l'original officiel contredit la consolidation",
        "",
    ]
    for a in avec_desaccord:
        for e in a["desaccords"]:
            lignes.append(f"- art. {a['numero']} : consolidation « {e['consolidation']} » / scan officiel « {e['scan_officiel']} »")
    lignes += ["", "## À relire sur l'image", ""] + [f"- art. {n} (score {r})" for n, r in a_relire]
    (ICI / "rapport_base_1975.md").write_text("\n".join(lignes) + "\n", encoding="utf-8")
    print("\n".join(lignes[:8]))
    print(f"désaccords : {sum(len(a['desaccords']) for a in avec_desaccord)} passages ; à relire : {a_relire}")


if __name__ == "__main__":
    main()
