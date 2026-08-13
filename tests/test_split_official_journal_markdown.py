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
    de l'acte suivant.

    Amendé le 2026-08-10 (garde-fou de continuité) : ce cas est la borne haute
    du prédicat `_coupure_autorisee_par_la_continuite`. La ligne qui précède le
    décret finit sur « portant », donc sans ponctuation forte — la formulation
    naïve du prédicat (« pas de ponctuation forte ⇒ pas de coupure ») ferait
    disparaître ici un acte parfaitement réel. C'est pourquoi le prédicat ne
    refuse la coupure que sur une classe fermée de mots grammaticaux (« et la »,
    « de l' », césure « créa- ») : « portant », « fixant », « relatif à » et les
    autres amorces d'objet d'un titre en sont délibérément absents, et la suite
    du titre est gérée par `_continuer_titre_multiligne`, pas par ce prédicat.
    """
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


# --- Un texte CITÉ n'est pas un acte (2026-08-10) ----------------------------
# Un visa énumère des textes avec leur numéro et leur date, c'est-à-dire
# exactement les marqueurs auxquels `_looks_like_real_act_title` reconnaît un
# vrai titre. Quand la colonne du JO coupe un visa, la ligne suivante commence
# par « Loi n° … » et franchit tous les contrôles de plausibilité. La
# continuité de la phrase précédente est le garde-fou qui tranche.


def test_visa_coupe_en_deux_ne_devient_pas_un_faux_acte():
    """Cas réel, mot pour mot : data/pipeline/md/sgg-jo/congo-jo-1958-01.md.

    Le visa « VU le Décret n° 46.2374 … et la / Loi n° 52.130 du 6 Février 1952
    relative à la formation des / Assemblées Locales … » produisait un huitième
    acte fantôme titré « Loi n° 52.130 … ». La ligne d'avant s'interrompt sur
    « et la » : la phrase reste en suspens, donc la coupure est refusée.
    """
    md = "\n".join([
        "ARRETE N° 4107/CAB 3 DU 28 NOVEMBRE 1958 ",
        "PROMULGUANT LA DELIBERATION N° 112/58",
        "DU 28 NOVEMBRE 1958 DE L’ASSEMBLEE",
        "TERRITORIALE DU MOYEN-CONGO PAR LAQUELLE",
        "CELLE-CI DECLARE OPTER POUR LE STATUT",
        "D’ETAT MEMBRE DE  LA COMMUNAUTE",
        "ET PROCLAMANT LA REPUBLIQUE DU CONGO",
        "LE CHEF DU TERRITOIRE DU MOYEN-CONGO",
        "Officier de la Légion d’Honneur,",
        "VU la Constitution, et notamment ses articles, 76, 79 et 91,",
        "VU l’Ordonnance 58.973 du 6 Octobre 1958, et notam-",
        "ment son article premier,",
        "VU le Décret n° 46.2374 du 25 Octobre 1946 portant créa-",
        "tion d’Assemblées Représentatives Territoriales en A.E.F. et la",
        "Loi n° 52.130 du 6 Février 1952 relative à la formation des",
        "Assemblées Locales d’A.O.F. du TOGO, d’A.E.F. du CAME-",
        "ROUN et de MADAGASCAR,",
        "ARRETE :",
        "ARTICLE PREMIER. - Est promulguée la Délibération n°",
        "112/58 du 28 Novembre 1958 de l’Assemblée Territoriale du",
        "Moyen-Congo.",
    ])
    actes = split_official_journal_markdown(md)
    assert [a["titre"] for a in actes] == [
        "ARRETE N° 4107/CAB 3 DU 28 NOVEMBRE 1958 "
        "PROMULGUANT LA DELIBERATION N° 112/58 "
        "DU 28 NOVEMBRE 1958 DE L’ASSEMBLEE "
        "TERRITORIALE DU MOYEN-CONGO PAR LAQUELLE "
        "CELLE-CI DECLARE OPTER POUR LE STATUT "
        "D’ETAT MEMBRE DE  LA COMMUNAUTE "
        "ET PROCLAMANT LA REPUBLIQUE DU CONGO"
    ]
    # Le visa reste dans le corps de l'acte : il n'est pas perdu, seulement
    # rendu à ce qu'il est.
    assert "Loi n° 52.130 du 6 Février 1952" in actes[0]["contenu"]


def test_un_visa_sans_porte_de_sortie_n_avale_pas_les_actes_suivants():
    """Non-régression du garde-fou RETIRÉ le 10/08/2026.

    Un drapeau « bloc de visas ouvert » avait été ajouté puis retiré : les
    actes en abrégé n'ont ni verbe de dispositif isolé ni « ARTICLE PREMIER »,
    donc rien ne le refermait et il étouffait la fin du JO. Cas réel mesuré :
    congo-jo-2013-21 tombait de 14 actes à 2. Ici, une ligne « Vu … » précède
    deux actes en abrégé qui ne ferment jamais rien — les deux doivent sortir.
    """
    md = "\n".join([
        "ARRÊTÉ N° 5791 DU 16 MAI 2013 PORTANT AUTORISATION",
        "Vu la Constitution ;",
        "La société MAUD Congo s.a. est autorisée à prospecter.",
        "Arrêté n° 5792 du 16 mai 2013. La société Bikonga Mining s.a. est autorisée.",
        "Arrêté n° 5793 du 16 mai 2013. La société Alector Congo s.a.r.l est autorisée.",
    ])
    assert _titres(md) == [
        "ARRÊTÉ N° 5791 DU 16 MAI 2013 PORTANT AUTORISATION",
        "Arrêté n° 5792 du 16 mai 2013. La société Bikonga Mining s.a. est autorisée.",
        "Arrêté n° 5793 du 16 mai 2013. La société Alector Congo s.a.r.l est autorisée.",
    ]


def test_formule_de_legalisation_n_avale_pas_l_acte_suivant():
    """Second mode d'échec du drapeau retiré : « Vu pour la légalisation de la
    signature » figure en FIN d'acte, donc juste avant le titre suivant. Six
    lois de ratification disparaissaient ainsi dans congo-jo-2022-03-sp."""
    md = "\n".join([
        "LOI N° 13-2022 DU 4 MAI 2022 AUTORISANT LA RATIFICATION",
        "Article premier : La ratification est autorisée.",
        "Vu pour la légalisation de la signature",
        "Le Secrétaire général",
        "LOI N° 14-2022 DU 4 MAI 2022 AUTORISANT LA RATIFICATION",
        "Article premier : La ratification est autorisée.",
    ])
    assert _titres(md) == [
        "LOI N° 13-2022 DU 4 MAI 2022 AUTORISANT LA RATIFICATION",
        "LOI N° 14-2022 DU 4 MAI 2022 AUTORISANT LA RATIFICATION",
    ]


def test_citation_hors_visas_est_bloquee_par_la_continuite():
    """Symétrique du précédent : hors de tout bloc de visas, c'est la phrase
    laissée en suspens (« fixées par la ») qui interdit la coupure."""
    md = "\n".join([
        "ARRÊTÉ N° 1 DU 2 JANVIER 2026 PORTANT ORGANISATION",
        "Article premier : Les modalités d'application sont fixées par la",
        "loi n° 4-2005 du 11 avril 2005 portant code minier.",
    ])
    assert _titres(md) == ["ARRÊTÉ N° 1 DU 2 JANVIER 2026 PORTANT ORGANISATION"]


def test_trois_actes_a_visas_s_enchainent_sans_s_avaler():
    """Le risque exactement inverse de celui qu'on corrige : les visas du
    premier acte ne doivent pas étouffer les suivants. Chaque acte porte ici
    ses propres visas et doit être retrouvé."""
    md = "\n".join([
        "LOI N° 1-2026 DU 3 JANVIER 2026 PORTANT CODE DU TRAVAIL",
        "Vu la Constitution ;",
        "Vu la loi n° 5-2020 du 1er mars 2020 ;",
        "Décrète :",
        "Article premier : La présente loi régit les relations de travail.",
        "DÉCRET N° 45-2026 DU 5 JANVIER 2026 PORTANT NOMINATION",
        "Vu la Constitution ;",
        "Arrête :",
        "Article premier : Est nommé M. X au poste de Y.",
        "ARRÊTÉ N° 7-2026 DU 6 JANVIER 2026 PORTANT AGRÉMENT",
        "Vu la Constitution ;",
        "Article premier : La société Z est agréée.",
    ])
    assert _titres(md) == [
        "LOI N° 1-2026 DU 3 JANVIER 2026 PORTANT CODE DU TRAVAIL",
        "DÉCRET N° 45-2026 DU 5 JANVIER 2026 PORTANT NOMINATION",
        "ARRÊTÉ N° 7-2026 DU 6 JANVIER 2026 PORTANT AGRÉMENT",
    ]


def test_deliberation_sans_verbe_de_dispositif_n_avale_pas_l_acte_suivant():
    """Délibérations et lois promulguées passent des visas au premier article
    sans verbe de dispositif — cas où un garde-fou fondé sur ce verbe aurait
    laissé le premier acte avaler le second."""
    md = "\n".join([
        "DELIBERATION N° 112/58 ERIGEANT LE TERRITOIRE EN ETAT MEMBRE",
        "VU le décret du 25 Octobre 1946,",
        "ARTICLE PREMIER. - Le territoire est érigé en Etat membre.",
        "ARRETE N° 4107 DU 28 NOVEMBRE 1958 PROMULGUANT LA DELIBERATION",
        "ARTICLE PREMIER. - Est promulguée la délibération susvisée.",
    ])
    assert _titres(md) == [
        "DELIBERATION N° 112/58 ERIGEANT LE TERRITOIRE EN ETAT MEMBRE",
        "ARRETE N° 4107 DU 28 NOVEMBRE 1958 PROMULGUANT LA DELIBERATION",
    ]


# --- Risque symétrique : les « actes en abrégé » doivent survivre ------------
# Ces actes (nominations, pensions, agréments) n'ont ni visas ni dispositif :
# ils s'enchaînent directement, et rien ne garantit que le précédent se termine
# par une ponctuation forte. C'est le corpus qui a tranché la formulation du
# prédicat de continuité — mesure sur les 1 436 markdowns de data/pipeline/md/ :
# la version « toute ligne sans ponctuation forte bloque » faisait tomber le
# découpage de 54 249 à 37 195 actes.


def test_actes_en_abrege_qui_s_enchainent_restent_separes():
    """Cas réel, mot pour mot : congo-jo-2021-41.md (nominations militaires)."""
    md = "\n".join([
        "Arrêté n° 21600 du 24 septembre 2021. ",
        "Le commandant NDJILA MAYAMOU (Cyr Freddy), ",
        "est nommé chef de division du personnel.",
        "Le présent arrêté prend effet à compter de la date de ",
        "prise de fonctions par l’intéressé.",
        "Arrêté n° 21601 du 24 septembre 2021. ",
        "Le capitaine NGAKOSSO (Auguste Lazare) est nommé ",
        "chef de division de la recherche.",
    ])
    titres = _titres(md)
    assert len(titres) == 2
    assert titres[0].startswith("Arrêté n° 21600 du 24 septembre 2021.")
    assert titres[1].startswith("Arrêté n° 21601 du 24 septembre 2021.")


def test_acte_en_abrege_precede_d_une_ligne_sans_ponctuation_reste_detecte():
    """Cas réel, mot pour mot : congo-jo-2008-41.md (concessions de pension).

    L'acte précédent se termine sur « /mois », sans le moindre point : une
    ligne peut parfaitement finir sans ponctuation sans pour autant laisser une
    phrase en suspens. Seule une marque grammaticale d'attente (« et la »,
    « de l’ », césure) bloque la coupure.
    """
    md = "\n".join([
        "Arrêté n° 6243 du 1er octobre 2008. Est concédée sur",
        "la Caisse de retraite des fonctionnaires, la pension à M. MAHOUKOU.",
        "Observations : bénéficie d’une majoration de pension pour",
        "famille nombreuse de 20% p/c du 1-2-2006 soit 28.606 frs",
        "/mois",
        "Arrêté n° 6265 du 2 octobre 2008. Est concédée sur",
        "la Caisse de retraite des fonctionnaires, la pension à M. GOMA",
        "(Gilbert).",
    ])
    titres = _titres(md)
    assert len(titres) == 2
    assert titres[1].startswith("Arrêté n° 6265 du 2 octobre 2008.")


def test_rubrique_du_sommaire_ne_bloque_pas_l_acte_qui_la_suit():
    """Cas réel : le JO annonce ses rubriques par des lignes sans ponctuation
    (« PARTIE OFFICIELLE », « - LOI - »). Elles séparent les actes, elles ne
    les enchaînent pas."""
    md = "\n".join([
        "Du jeudi 14 octobre 2021",
        "PARTIE OFFICIELLE",
        "- LOI -",
        "Loi n° 41-2021 du 29 septembre 2021 fixant ",
        "le droit d’asile et le statut de réfugié",
        "L’Assemblée nationale et le Sénat",
        "ont delibéré et adopté ;",
    ])
    assert _titres(md) == [
        "Loi n° 41-2021 du 29 septembre 2021 fixant le droit d’asile et le statut de réfugié"
    ]


def test_un_saut_de_page_ne_bloque_jamais_un_acte():
    """Un marqueur de page, un folio ou l'en-tête répétée du JO ne finissent
    jamais par une ponctuation : les traiter comme une phrase en suspens
    coûterait un acte à chaque changement de page."""
    md = "\n".join([
        "Arrêté n° 10443 du 20 décembre 2010. La société ALPHA est agréée",
        "1108",
        "Journal officiel de la République du Congo",
        "[[MIBEKO_PAGE:29]]",
        "Arrêté n° 10444 du 20 décembre 2010. La société DELTA MARINE est agréée.",
    ])
    titres = _titres(md)
    assert len(titres) == 2
    assert titres[1].startswith("Arrêté n° 10444 du 20 décembre 2010.")


# --- Sommaire du JO : des titres alignés sans corps ne sont pas des actes -----
#
# Constat prod du 13/08/2026 : 25 des 27 documents `extraction_status='failed'`
# sont des entrées de sommaire promues en actes. Le sommaire congolais aligne
# « titre … numéro de page » sans parenthèses, donc hors de portée de
# `toc_entry_regex` ; chaque ligne ouvrait un acte que la ligne suivante
# refermait aussitôt, à zéro article. Rejoué sur `congo-jo-1959-29.md` :
# 42 actes dont 6 vides avant, 36 actes dont 0 vide après.


def test_bloc_de_sommaire_ne_produit_aucun_acte_vide():
    """Extrait réel de congo-jo-1959-29.md (bloc sommaire, pages 727-730)."""
    md = "\n".join([
        "Arrêté n° 5067/AEFE./AE. du 12novembre 1959 fixant la date et les modalités des élections aux chambres de commerce 727",
        "",
        "Arrêté n° 5073/AE. du 17 novembre 1959 fixant les prix maxima applicables à la vente du pau au détail au Congo 728",
        "",
        "Décret n° 59-233 du 13 novembre 1959 portant application, pour les travaillleurs relevant du code du travail 729",
        "",
        "Décret n° 59-234 du 13 novembre 1959 fixant les dispositions particulières de la durée du travail 730",
        "",
        "Art. 1er. — Le présent décret sera publié au Journal officiel.",
    ])
    actes = split_official_journal_markdown(md)
    # Seule la dernière ligne du sommaire, celle qui précède le corps, survit.
    assert [a["titre"] for a in actes] == [
        "Décret n° 59-234 du 13 novembre 1959 fixant les dispositions particulières de la durée du travail 730"
    ]
    assert all(a["contenu"].strip() for a in actes)


def test_un_acte_reel_a_titre_finissant_par_un_nombre_est_conserve():
    """Le filtre porte sur le contenu vide, jamais sur le numéro de page final :
    mesuré sur les 60 JO de `data/pipeline/md/`, 129 actes RÉELS finissent par
    un nombre. Les écarter détruirait du texte."""
    md = "\n".join([
        "Arrêté n° 10443 du 20 décembre 2010 portant agrément de la société ALPHA 1108",
        "",
        "LE MINISTRE DES FINANCES,",
        "",
        "Art. 1er. — La société ALPHA est agréée.",
    ])
    actes = split_official_journal_markdown(md)
    assert len(actes) == 1
    assert "La société ALPHA est agréée." in actes[0]["contenu"]
