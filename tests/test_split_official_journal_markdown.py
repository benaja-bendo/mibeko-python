"""Remédiation 2026-08-02 : `split_official_journal_markdown` (src/api/main.py)
croyait qu'une clause de clôture ordinaire du JO congolais — coupée par le
rendu markdown de sorte qu'une ligne se retrouve à commencer par un mot-clé
d'acte (« arrêté », « communiqué »…) — était le début d'un NOUVEL acte.

Confirmé en prod par un audit en lecture seule : un même lot d'ingestion a
produit 24 documents fantômes titrés « arrêté pourra faire l'objet d'une
suspension ou d'un » et 21 titrés « communiqué partout où besoin sera. » —
aucun n'est un acte réel, tous sont des fragments de phrase de clôture.

Avant ce correctif, seuls NOTE/RAPPORT (`_WEAK_ACT_KEYWORDS`) passaient par
`_looks_like_real_act_title` (numéro, date, ou libellé en MAJUSCULES) ; les
autres mots-clés — dont ARRÊTÉ et COMMUNIQUÉ — étaient acceptés sans aucune
vérification de plausibilité dès qu'une ligne portait « une suite » (chiffre
ou lettre). Le correctif applique ce contrôle à TOUS les mots-clés.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.main import split_official_journal_markdown  # noqa: E402


def _titres(md):
    return [a["titre"] for a in split_official_journal_markdown(md)]


# --- Cas réels confirmés en prod : clauses de clôture, pas de nouveaux actes --

def test_clause_de_cloture_arrete_nest_pas_un_nouvel_acte():
    """Cas réel exact (prod, 24 documents fantômes)."""
    md = "\n".join([
        "ARRÊTÉ N° 3831 DU 8 SEPTEMBRE 2025 PORTANT DIRECTIVES",
        "Article premier : Contenu réel de l'acte.",
        "Le titulaire s'expose à des sanctions et le présent",
        "arrêté pourra faire l'objet d'une suspension ou d'un retrait.",
        "Fait à Brazzaville, le 8 septembre 2025",
    ])
    titres = _titres(md)
    assert titres == ["ARRÊTÉ N° 3831 DU 8 SEPTEMBRE 2025 PORTANT DIRECTIVES"]


def test_clause_de_cloture_communique_nest_pas_un_nouvel_acte():
    """Cas réel exact (prod, 21 documents fantômes)."""
    md = "\n".join([
        "AVIS N° 12 DU 17 SEPTEMBRE 2025 PORTANT AGRÉMENT",
        "Article premier : Contenu réel de l'acte.",
        "Le présent avis sera publié et affiché et",
        "communiqué partout où besoin sera.",
    ])
    titres = _titres(md)
    assert titres == ["AVIS N° 12 DU 17 SEPTEMBRE 2025 PORTANT AGRÉMENT"]


# --- Non-régression : les vrais titres, avec ou sans N°, restent détectés ---

def test_vrai_titre_arrete_avec_numero_toujours_detecte():
    md = "\n".join([
        "ARRÊTÉ N° 3277 DU 28 AOÛT 2025 PORTANT ATTRIBUTION D'UNE LICENCE",
        "Article premier : Est attribuée la licence.",
    ])
    assert _titres(md) == ["ARRÊTÉ N° 3277 DU 28 AOÛT 2025 PORTANT ATTRIBUTION D'UNE LICENCE"]


def test_vrai_titre_sans_numero_mais_avec_date_toujours_detecte():
    md = "\n".join([
        "DÉCISION DU 10 OCTOBRE 1959 RELATIVE AU RÉGIME DES ARMES",
        "Article premier : Contenu.",
    ])
    assert _titres(md) == ["DÉCISION DU 10 OCTOBRE 1959 RELATIVE AU RÉGIME DES ARMES"]


def test_vrai_titre_tout_en_majuscules_sans_numero_ni_date_toujours_detecte():
    md = "\n".join([
        "COMMUNIQUÉ DU CONSEIL DES MINISTRES SUR LA SITUATION ÉCONOMIQUE",
        "Le conseil des ministres s'est réuni ce jour.",
    ])
    assert _titres(md) == ["COMMUNIQUÉ DU CONSEIL DES MINISTRES SUR LA SITUATION ÉCONOMIQUE"]


def test_deux_actes_reels_toujours_bien_separes():
    md = "\n".join([
        "LOI N° 1-2026 DU 3 JANVIER 2026 PORTANT CODE DU TRAVAIL",
        "Article premier : La présente loi régit les relations de travail.",
        "DÉCRET N° 45-2026 DU 5 JANVIER 2026 PORTANT NOMINATION",
        "Article 1 : Est nommé M. X au poste de Y.",
    ])
    assert _titres(md) == [
        "LOI N° 1-2026 DU 3 JANVIER 2026 PORTANT CODE DU TRAVAIL",
        "DÉCRET N° 45-2026 DU 5 JANVIER 2026 PORTANT NOMINATION",
    ]


def test_note_et_rapport_toujours_couverts_comme_avant():
    """Non-régression directe de l'ancien `_WEAK_ACT_KEYWORDS`."""
    md = "\n".join([
        "ARRÊTÉ N° 99 DU 1 JANVIER 2026 PORTANT X",
        "Article premier : Contenu.",
        "Conformément à la note ci-jointe, le service est informé.",
    ])
    assert _titres(md) == ["ARRÊTÉ N° 99 DU 1 JANVIER 2026 PORTANT X"]
