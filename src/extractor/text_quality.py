"""Heuristique de qualité/lisibilité de texte — extrait de src/api/main.py
le 5 juillet 2026 pour être réutilisable sans dépendre de FastAPI/SQLAlchemy
(l'étage 2 « triage » de l'usine à textes en a besoin en dehors de l'API).

`src/api/main.py` ré-importe ces symboles (compatibilité des tests/appelants
existants) : c'est la même fonction, un seul et même calcul.
"""

import os
import re
from typing import Any, Dict, Tuple

# Artefacts OCR fréquents (confusions caractères) et leur correction. Servent à
# DEUX usages : (1) la réparation par sanitize_legal_text avant découpage ;
# (2) le calcul de l'indicateur de qualité OCR (compute_ocr_quality) qui mesure
# leur DENSITÉ dans le texte AVANT réparation — même source de vérité, pas de
# divergence entre « ce qu'on répare » et « ce qu'on mesure ».
OCR_ARTIFACT_REPLACEMENTS: Tuple[Tuple[str, str], ...] = (
    (r"\bL0I\b", "LOI"),
    (r"\bArtide\b", "Article"),
    (r"\bDÉCRÊT\b", "DECRET"),
    (r"\bARRETÊ\b", "ARRETE"),
    (r"\bN°\s*o\b", "N°"),
    # Ligature 'ﬁ' (U+FB01) mal décomposée par certains PDF (polices custom
    # d'export Word→PDF) : le glyphe compose ressort avec un espace parasite
    # ('ﬁ xe' au lieu de 'fixe') — constaté sur la loi n°33-2023 (gestion
    # durable de l'environnement). Toujours substituable : 'ﬁ' seul n'existe
    # dans aucun mot français, l'espace qui suit est l'artefact, pas une vraie
    # coupure de mots.
    (r"ﬁ\s?", "fi"),
    (r"ﬂ\s?", "fl"),
)

_OCR_ARTIFACT_PATTERNS: Tuple[re.Pattern, ...] = tuple(
    re.compile(pattern, flags=re.IGNORECASE) for pattern, _ in OCR_ARTIFACT_REPLACEMENTS
)


def sanitize_legal_text(content: str) -> str:
    """Corrige quelques artefacts OCR fréquents avant découpage ou parsing."""

    result = content
    for (pattern, replacement), compiled in zip(OCR_ARTIFACT_REPLACEMENTS, _OCR_ARTIFACT_PATTERNS):
        result = compiled.sub(replacement, result)
    return result


# Seuil (0..1) sous lequel un document est signalé « qualité OCR faible ». Ce
# n'est PAS un blocage dur : le flag émis est de sévérité 'warning' (informe
# l'éditeur, ne gèle pas la publication — cf. curation_flags.severity). Un seuil
# dur sur une heuristique gèlerait du contenu légitime (faux négatifs graves).
# 0.60 par défaut : calibré pour laisser passer un OCR propre (score ~0.9-1.0) et
# n'alerter que sur une dégradation nette (nombreux U+FFFD / fragmentation).
OCR_QUALITY_WARN_THRESHOLD = float(os.getenv("OCR_QUALITY_WARN_THRESHOLD", "0.60"))

# Mots d'une seule lettre légitimes en français (ne comptent PAS comme
# fragmentation OCR). « c » et « d » couvrent les élisions c'/d' déjà découpées.
_FRENCH_SINGLE_LETTER_WORDS = {"a", "à", "y", "l", "d", "c", "n", "s", "j", "m", "t"}
# Caractère de remplacement Unicode (U+FFFD) : inséré au décodage/OCR quand un
# glyphe n'a pas pu être reconnu. Signal le plus fiable d'une source dégradée.
_REPLACEMENT_CHAR = "�"


def compute_ocr_quality(text_content: str) -> Dict[str, Any]:
    """Estime la LISIBILITÉ d'un texte OCRisé à partir de signaux réels et mesurables.

    ATTENTION — ce que ce score EST et n'est PAS :
      - c'est une heuristique de lisibilité (« le texte est-il propre ? ») ;
      - ce n'est PAS une mesure de justesse juridique (« le contenu est-il
        exact ? ») : un texte parfaitement lisible peut être juridiquement faux,
        et l'inverse. Il ne remplace pas la relecture d'un juriste.

    Le score ∈ [0, 1] (1 = propre, 0 = fortement dégradé) part de 1.0 et applique
    des PÉNALITÉS proportionnelles à la densité de quatre signaux de dégradation
    (chacun documenté, chacun mesurable de façon déterministe) :

      - ``replacement_char`` : caractères U+FFFD (glyphe non reconnu au décodage) ;
      - ``control_char``     : autres caractères de contrôle/non imprimables
        (hors \\n, \\t, espaces) — bruit binaire dans le texte ;
      - ``ocr_artifact``     : artefacts de confiance connus (les mêmes que
        sanitize_legal_text : L0I, Artide, DÉCRÊT…), par 1000 caractères ;
      - ``fragmentation``    : proportion de « mots » réduits à une seule lettre
        non légitime — l'OCR fragmente les mots quand il hésite.

    Renvoie un dict sérialisable JSON destiné à ``extraction_runs.meta.ocr_quality``
    (score + composantes brutes, pour audit et affichage éditeur).

    ATTENTION (triage, étage 2) : un texte VIDE (total_chars=0) reçoit un score
    neutre de 1.0 — « pas de dégradation mesurable » n'est pas « pas de
    problème ». Un PDF scanné sans couche texte donne un texte vide : ne PAS
    utiliser ce score seul pour décider qu'un extrait natif est exploitable,
    croiser avec `total_chars` (cf. `src/parsing/triage.py`).
    """
    text = text_content or ""
    total_chars = len(text)

    # Comptages bruts.
    replacement_count = text.count(_REPLACEMENT_CHAR)
    control_count = sum(
        1 for ch in text
        if ord(ch) < 0x20 and ch not in ("\n", "\t", "\r")
    )
    artifact_count = sum(len(pattern.findall(text)) for pattern in _OCR_ARTIFACT_PATTERNS)

    words = re.findall(r"[^\W\d_]+", text, flags=re.UNICODE)
    total_words = len(words)
    single_letter_bad = sum(
        1 for w in words
        if len(w) == 1 and w.lower() not in _FRENCH_SINGLE_LETTER_WORDS
    )

    # Densités (bornées à des références lisibles). Un texte vide n'a aucune
    # dégradation MESURABLE : on ne peut pas prétendre le contraire → score 1.0,
    # neutre (le garde-fou « document vide/sans article » est traité ailleurs).
    if total_chars == 0:
        replacement_ratio = control_ratio = artifact_density = fragmentation_ratio = 0.0
    else:
        replacement_ratio = replacement_count / total_chars
        control_ratio = control_count / total_chars
        artifact_density = artifact_count / (total_chars / 1000.0)
        fragmentation_ratio = (single_letter_bad / total_words) if total_words else 0.0

    # Pénalités pondérées. U+FFFD est le signal le plus dur (×40 : ~2,5 % de
    # U+FFFD suffit à faire chuter le score sous le seuil). Les autres sont plus
    # tolérants (bruit possible sur du texte légitime).
    penalty_replacement = min(1.0, replacement_ratio * 40.0)
    penalty_control = min(1.0, control_ratio * 30.0)
    penalty_artifact = min(1.0, artifact_density * 0.05)      # ~20 artefacts/1000 car. = pénalité max
    penalty_fragmentation = min(1.0, max(0.0, fragmentation_ratio - 0.05) * 4.0)

    # Le score retient la pénalité la plus forte (le pire signal domine) plutôt
    # que leur somme : deux signaux indépendants ne doivent pas s'additionner en
    # double peine. Un OCR très dégradé sur UN axe suffit à mériter l'alerte.
    worst_penalty = max(
        penalty_replacement, penalty_control, penalty_artifact, penalty_fragmentation
    )
    score = round(max(0.0, 1.0 - worst_penalty), 4)

    return {
        "score": score,
        "total_chars": total_chars,
        "signals": {
            "replacement_char_count": replacement_count,
            "control_char_count": control_count,
            "ocr_artifact_count": artifact_count,
            "single_letter_word_count": single_letter_bad,
            "total_words": total_words,
        },
        "penalties": {
            "replacement": round(penalty_replacement, 4),
            "control": round(penalty_control, 4),
            "artifact": round(penalty_artifact, 4),
            "fragmentation": round(penalty_fragmentation, 4),
        },
    }
