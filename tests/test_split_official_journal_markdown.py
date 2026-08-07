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


# --- Titre coupé sur plusieurs lignes physiques par la mise en page du PDF ---
# Constaté en conditions réelles le 07/08/2026 sur la loi n° 33-2023 (gestion
# durable de l'environnement, JO 2023-48) : le titre s'arrêtait à « portant »,
# le reste (« gestion durable de l'environnement en République du Congo »)
# se retrouvait en tête de contenu. Identique sur décrets et arrêtés réels du
# même JO — extraits ci-dessous, mot pour mot.

def test_titre_de_loi_coupe_sur_trois_lignes_est_reconstitue():
    md = "\n".join([
        "Loi n° 33-2023 du 17 novembre 2023 portant ",
        "gestion durable de l’environnement en République du ",
        "Congo",
        "L’Assemblée nationale et le Sénat ",
        "ont délibéré et adopté ;",
    ])
    actes = split_official_journal_markdown(md)
    assert actes[0]["titre"] == (
        "Loi n° 33-2023 du 17 novembre 2023 portant "
        "gestion durable de l’environnement en République du Congo"
    )
    assert actes[0]["contenu"].startswith("L’Assemblée nationale et le Sénat")


def test_titre_de_decret_coupe_est_reconstitue_jusqu_au_president():
    md = "\n".join([
        "Décret n° 2023-1756 du 17 novembre 2023 ",
        "portant organisation du ministère de l’environnement, ",
        "du développement durable et du bassin du Congo ",
        "Le Président de la République,",
        "Vu la Constitution ;",
    ])
    actes = split_official_journal_markdown(md)
    assert actes[0]["titre"] == (
        "Décret n° 2023-1756 du 17 novembre 2023 "
        "portant organisation du ministère de l’environnement, "
        "du développement durable et du bassin du Congo"
    )
    assert actes[0]["contenu"].startswith("Le Président de la République,")


def test_titre_d_arrete_coupe_est_reconstitue_jusqu_au_ministre():
    """Le rôle générique « Le ministre » suffit à arrêter la continuation,
    sans connaître l'intitulé exact du portefeuille (variable à chaque
    remaniement)."""
    md = "\n".join([
        "Arrêté n° 14531 du 14 novembre 2023 ",
        "portant nomination des membres de la commission ",
        "mixte chargée de la négociation de la convention ",
        "collective spécifique aux sociétés de catering pétrolier",
        "Le ministre d’Etat, ministre de la fonction publique, ",
        "du travail et de la sécurité sociale,",
        "Vu la Constitution ;",
    ])
    actes = split_official_journal_markdown(md)
    assert actes[0]["titre"] == (
        "Arrêté n° 14531 du 14 novembre 2023 "
        "portant nomination des membres de la commission "
        "mixte chargée de la négociation de la convention "
        "collective spécifique aux sociétés de catering pétrolier"
    )
    assert actes[0]["contenu"].startswith("Le ministre d’Etat")


def test_titre_sur_une_seule_ligne_reste_inchange():
    """Non-régression : un titre déjà complet sur une ligne ne doit RIEN
    avaler de plus, même si la ligne suivante est anodine."""
    md = "\n".join([
        "Loi n° 4-2005 du 11 avril 2005 portant code minier",
        "Vu la Constitution ;",
        "Article premier : Contenu.",
    ])
    assert _titres(md) == ["Loi n° 4-2005 du 11 avril 2005 portant code minier"]


def test_continuation_s_arrete_a_un_nouvel_acte_sans_formule_d_autorite():
    """Si un acte s'enchaîne directement après un autre sans ligne d'autorité
    identifiable (cas rare), la continuation ne doit jamais avaler le titre
    de l'acte suivant."""
    md = "\n".join([
        "Arrêté n° 1 du 2 janvier 2026 portant ",
        "Décret n° 2 du 3 janvier 2026 portant nomination",
        "Article premier : Contenu.",
    ])
    assert _titres(md) == [
        "Arrêté n° 1 du 2 janvier 2026 portant",
        "Décret n° 2 du 3 janvier 2026 portant nomination",
    ]


def test_continuation_s_arrete_au_saut_de_page_sans_avaler_l_entete():
    """Cas réel (JO 2010-52) : un « acte en abrégé » sans structure Vu/
    Considérant n'a aucune formule d'autorité pour arrêter la continuation —
    sans ce garde-fou, le pied de page et l'en-tête répétée du JO entraient
    dans le titre."""
    md = "\n".join([
        "Arrêté n ° 10444 du 20 décembre 2010. La",
        "société DELTA MARINE SERVICES, B.P. : 1343,",
        "1108",
        "Journal officiel de la République du Congo",
        "N° 52-2010",
        "[[MIBEKO_PAGE:29]]",
        "Pointe-Noire, est agréée pour l’exercice de l’activité",
    ])
    actes = split_official_journal_markdown(md)
    assert actes[0]["titre"] == (
        "Arrêté n ° 10444 du 20 décembre 2010. La "
        "société DELTA MARINE SERVICES, B.P. : 1343,"
    )
    assert "1108" not in actes[0]["titre"]
    assert "Journal officiel" not in actes[0]["titre"]


def test_continuation_plafonnee_si_aucun_marqueur_d_arret_trouve():
    """Filet de sécurité : sans marqueur reconnu, la continuation s'arrête
    au bout de 6 lignes plutôt que d'avaler tout le reste de l'acte."""
    md = "\n".join([
        "Arrêté n° 1 du 2 janvier 2026 portant",
        "ligne 1", "ligne 2", "ligne 3", "ligne 4", "ligne 5", "ligne 6",
        "ligne 7 qui ne doit pas être avalée",
    ])
    actes = split_official_journal_markdown(md)
    assert actes[0]["titre"].count("ligne") == 6
    assert "ligne 7" not in actes[0]["titre"]
