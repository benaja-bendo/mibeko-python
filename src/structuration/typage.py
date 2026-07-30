"""Déduction du type_code d'un document juridique (référentiel document_types).

Pourquoi c'est critique : la recherche publique (ArticleSearchController, côté
Laravel) fait un INNER JOIN sur document_types — un document publié dont le
type_code est NULL est **invisible** du public, sans erreur ni avertissement.
Le pipeline doit donc toujours poser un type, quitte à retomber sur TEXTE
(« Texte Juridique (Générique) ») quand rien de plus précis ne se déduit.

La déduction est volontairement déterministe (nature du manifeste d'abord,
motif du titre ensuite) : pas de LLM ici — un typage doit être rejouable à
l'identique et vérifiable d'un coup d'œil.
"""

import re

# Le type JO n'existe pas dans le seeder historique : les journaux officiels
# ingérés par le pipeline sont les premiers documents de ce genre. La commande
# de backfill crée la ligne si elle manque (firstOrCreate, comme le seeder).
TYPE_JO = ("JO", "Journal officiel", 110)

# nature (canonique du pipeline, cf. NATURE_PAR_TYPE_SOURCE) → code référentiel.
TYPE_CODE_PAR_NATURE = {
    "journal officiel": "JO",
    "code": "CODE",
    "loi": "LOI",
    "décret": "DEC",
    "arrêté": "ARR",
    "ordonnance": "ORD",
    "constitution": "CONST",
    "acte uniforme": "AU",
}

# Motifs de titre, testés dans l'ordre — le premier qui matche gagne. Ancrés en
# début de titre pour ne pas typer « Loi » un décret *modifiant* une loi.
_MOTIFS_TITRE = (
    (re.compile(r"^journal\s+officiel", re.IGNORECASE), "JO"),
    (re.compile(r"^constitution|constitution\s+\d{4}", re.IGNORECASE), "CONST"),
    (re.compile(r"^acte\s+uniforme", re.IGNORECASE), "AU"),
    (re.compile(r"^loi\b", re.IGNORECASE), "LOI"),
    (re.compile(r"^d[eé]cret\b", re.IGNORECASE), "DEC"),
    (re.compile(r"^arr[eê]t[eé]\b", re.IGNORECASE), "ARR"),
    (re.compile(r"^ordonnance\b", re.IGNORECASE), "ORD"),
    (re.compile(r"^code\b|\bcode\s+(de|du|des|p[eé]nal|civil)\b", re.IGNORECASE), "CODE"),
    (re.compile(r"^convention\s+collective", re.IGNORECASE), "CONV"),
)


def deduire_type_code(nature: str | None, titre_officiel: str | None) -> str:
    """Déduit le code du référentiel document_types. Ne renvoie jamais None.

    La nature du manifeste prime (provenance connue), le titre sert de repli,
    TEXTE est le filet : un document sans type est un document invisible.
    """
    if nature:
        code = TYPE_CODE_PAR_NATURE.get(nature.strip().lower())
        if code:
            return code

    if titre_officiel:
        for motif, code in _MOTIFS_TITRE:
            if motif.search(titre_officiel.strip()):
                return code

    return "TEXTE"


def planifier_backfill(documents: list) -> dict:
    """Répartit des (id, titre_officiel) par type déduit. Fonction pure.

    Ne reçoit que des documents dont le type_code est NULL : un type déjà posé
    (à la main dans l'éditeur, par exemple) ne se réécrit jamais.
    """
    par_code: dict = {}
    for doc_id, titre in documents:
        code = deduire_type_code(None, titre)
        par_code.setdefault(code, []).append((doc_id, titre))
    return par_code
