"""Remédiation 2026-08-02 phase 5, suite : deux conventions de numérotation
composée que `ARTICLE_PATTERN` tronquait après le premier segment, créant de
faux `article_doublon` sur des sous-numéros pourtant DISTINCTS.

- CEMAC/OHADA — numérotation décimale à 3 niveaux (Titre.Chapitre.Article) :
  « Article 1.1.2 Directives… » ne capturait que « 1 », le reste
  (« .1.2 Directives… ») versé dans le contenu (Règlement n° 07/12-UEAC).
- Code Pénal congolais — sous-paragraphes lettrés d'un même article :
  « Article 14 a) : », « Article 14 b) : »… ne capturaient que « 14 », la
  lettre (qui fait partie du numéro réel dans le texte source) perdue —
  y compris sans parenthèse fermante (« Article 150 a : »).
- Code Pénal congolais — suffixes latins étendus : « sexies »/« septies »
  n'étaient pas reconnus (seuls bis/ter/quater/quinquies l'étaient).
- Code Pénal congolais — citation singulière par virgule : « article 182,
  183 et 184. » en tête de ligne (repli OCR) capturé comme numéro nu « 182 »
  au lieu de rester du contenu — pendant singulier du garde-fou pluriel déjà
  en place pour « Articles 74 et 75… ».

Les trois premiers sont aussi exclus de la séquence ordinale
(`ordinal_from_raw_number`, src/api/main.py) — comme bis/ter/tiret déjà —
puisqu'ils partagent le numéro de base d'un article existant sans en être
des doublons.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.main import ordinal_from_raw_number  # noqa: E402
from src.extractor.parser import ARTICLE_PATTERN, LegalDocumentParser, _article_match_groups  # noqa: E402


def _articles(hierarchy):
    return [n for n in hierarchy if n["type"] == "ARTICLE"]


# --- CEMAC : numérotation décimale à 3 niveaux -------------------------------

def test_article_pattern_capture_numero_decimal_complet():
    """Cas réel : Règlement n° 07/12-UEAC-066-CM-23, « Article 1.1.2 »."""
    m = ARTICLE_PATTERN.match("Article 1.1.2 Directives et situation d'urgence")
    assert m is not None
    num, content = _article_match_groups(m)
    assert num == "1.1.2"
    assert content == "Directives et situation d'urgence"


def test_article_pattern_numero_decimal_simple_toujours_capture():
    """Non-régression : la forme à 2 niveaux (« 1.2 ») déjà rencontrée reste correcte."""
    m = ARTICLE_PATTERN.match("Article 1.2 Directives et situation d'urgence")
    num, _ = _article_match_groups(m)
    assert num == "1.2"


def test_ordinal_exclut_le_numero_decimal_complet():
    assert ordinal_from_raw_number("1.1.2") is None
    assert ordinal_from_raw_number("1.1.11") is None


def test_parse_hierarchy_ne_fusionne_plus_les_articles_decimaux_cemac():
    """Cas réel simplifié (Règlement CEMAC aviation civile) : plusieurs
    articles décimaux consécutifs restent des articles DISTINCTS, avec leur
    numéro complet — avant ce correctif, tous auraient reçu le numéro « 1 »."""
    texte = (
        "Article 1.1.1 Attributions\n"
        "Sous réserve de l'Article 1.1.4, le ministre chargé de l'aviation civile.\n"
        "Article 1.1.2 Directives et situation d'urgence\n"
        "Sous réserve du deuxième alinéa, le ministre chargé donne des directives.\n"
        "Article 1.1.3 Dérogation\n"
        "Le Ministre chargé de l'aviation civile peut déroger.\n"
    )
    hierarchy = LegalDocumentParser(text_content=texte).parse_hierarchy()
    articles = _articles(hierarchy)
    numeros = [a["number"] for a in articles]
    assert numeros == ["1.1.1", "1.1.2", "1.1.3"]


# --- Code Pénal : sous-paragraphes lettrés -----------------------------------

def test_article_pattern_capture_lettre_de_sous_paragraphe():
    """Cas réel exact (Code Pénal congolais, prod) : « Article 14 a) : »."""
    m = ARTICLE_PATTERN.match(
        "Article 14 a) : Lorsque l'infraction est punissable d'une peine de servitude pénale."
    )
    assert m is not None
    num, content = _article_match_groups(m)
    assert num == "14 a)"
    assert content == "Lorsque l'infraction est punissable d'une peine de servitude pénale."


def test_article_pattern_plusieurs_lettres_de_suite():
    for lettre in ("a", "b", "c", "d"):
        m = ARTICLE_PATTERN.match(f"Article 14 {lettre}) : Contenu de l'alinéa {lettre}.")
        assert m is not None, f"lettre {lettre} non reconnue"
        num, _ = _article_match_groups(m)
        assert num == f"14 {lettre})"


def test_ordinal_exclut_larticle_lettre():
    assert ordinal_from_raw_number("14 a)") is None
    assert ordinal_from_raw_number("150 k)") is None


def test_ordinal_numero_de_base_toujours_suivi():
    """Le numéro de base (« 14 » seul, sans lettre) reste dans la séquence —
    seules les insertions lettrées en sont exclues."""
    assert ordinal_from_raw_number("14") == 14


def test_parse_hierarchy_scinde_les_sous_paragraphes_lettres_du_code_penal():
    """Cas réel exact (Code Pénal congolais, prod, lignes 130-162 du markdown
    source) : avant ce correctif, « 14 a) », « 14 b) », « 14 c) », « 14 d) »
    étaient tous capturés comme numéro nu « 14 » — un doublon de chaîne à
    chaque occurrence. Désormais, quatre articles distincts avec leur numéro
    complet, aucun doublon."""
    texte = (
        "Article 14 :\n"
        "La confiscation spéciale s'applique uniquement.\n"
        "Article 14 a) :\n"
        "Lorsque l'infraction est punissable d'une peine de servitude pénale principale.\n"
        "Article 14 b) :\n"
        "Outre la peine de servitude pénale, les mêmes peines peuvent être prononcées.\n"
        "Article 14 c) :\n"
        "Les peines prévues par la présente section prennent cours.\n"
        "Article 15 :\n"
        "Disposition suivante.\n"
    )
    hierarchy = LegalDocumentParser(text_content=texte).parse_hierarchy()
    articles = _articles(hierarchy)
    numeros = [a["number"] for a in articles]
    assert numeros == ["14", "14 a)", "14 b)", "14 c)", "15"]


def test_article_pattern_capture_lettre_sans_parenthese_fermante():
    """Cas réel exact (Code Pénal congolais, prod) : « Article 150 a : »,
    sans parenthèse fermante — contrairement à « 14 a) » plus haut. Avant ce
    correctif, la parenthèse était obligatoire dans le suffixe lettré, donc
    ce format capturait un numéro nu « 150 » : 10 occurrences fusionnées."""
    m = ARTICLE_PATTERN.match(
        "Article 150 a : Toute personne au service d'un tiers qui aura sollicité."
    )
    assert m is not None
    num, content = _article_match_groups(m)
    assert num == "150 a"
    assert content == "Toute personne au service d'un tiers qui aura sollicité."


def test_ordinal_exclut_larticle_lettre_sans_parenthese():
    assert ordinal_from_raw_number("150 a") is None


def test_article_pattern_ne_devore_pas_linitiale_du_titre_suivant():
    """Non-régression découverte en corrigeant la lettre sans parenthèse
    ci-dessus : la parenthèse devenue optionnelle, `re.IGNORECASE` fait
    matcher `[a-z]` sur une majuscule aussi, donc « Article 1.1.3
    Dérogation » se faisait amputer son « D » (absorbé comme un faux
    suffixe lettré), laissant « érogation » en tête de contenu. Couvre aussi
    le cas accentué (« D » suivi de « é », hors plage ASCII)."""
    for titre, numero, contenu_attendu in (
        ("Article 1.1.1 Attributions", "1.1.1", "Attributions"),
        ("Article 1.1.3 Dérogation", "1.1.3", "Dérogation"),
    ):
        m = ARTICLE_PATTERN.match(titre)
        assert m is not None
        num, content = _article_match_groups(m)
        assert num == numero
        assert content == contenu_attendu


def test_parse_hierarchy_scinde_les_sous_paragraphes_lettres_sans_parenthese():
    """Cas réel simplifié (Code Pénal, article 150 a-c) : la lettre sans
    parenthèse doit produire des articles distincts, pas un doublon de
    « 150 »."""
    texte = (
        "Article 150 :\n"
        "Ceux qui auront contraint par violences ou menaces.\n"
        "Article 150 a :\n"
        "Toute personne au service d'un tiers qui aura sollicité.\n"
        "Article 150 b :\n"
        "Si une personne au service d'un tiers a directement sollicité.\n"
        "Article 151 :\n"
        "Disposition suivante.\n"
    )
    hierarchy = LegalDocumentParser(text_content=texte).parse_hierarchy()
    articles = _articles(hierarchy)
    numeros = [a["number"] for a in articles]
    assert numeros == ["150", "150 a", "150 b", "151"]


# --- Suffixes latins étendus (sexies/septies) --------------------------------

def test_article_pattern_capture_sexies_et_septies():
    """Cas réel (Code Pénal, article 138) : « Article 138 sexies : » n'était
    pas reconnu (seuls bis/ter/quater/quinquies l'étaient), donc capturé comme
    numéro nu « 138 » — doublon avec l'article 138 de base."""
    for suffixe in ("sexies", "septies"):
        m = ARTICLE_PATTERN.match(f"Article 138 {suffixe} : Sera puni d'une servitude pénale.")
        assert m is not None, f"suffixe {suffixe} non reconnu"
        num, _ = _article_match_groups(m)
        assert num == f"138 {suffixe}"


def test_ordinal_exclut_sexies_et_septies():
    assert ordinal_from_raw_number("138 sexies") is None
    assert ordinal_from_raw_number("138 septies") is None


# --- Citation singulière par virgule ------------------------------------------

def test_article_pattern_rejette_citation_singuliere_par_virgule():
    """Cas réel exact (Code Pénal, prod) : « article 182, 183 et 184. » en
    tête de ligne (repli de mise en page OCR) n'est pas un nouvel article
    mais une citation à la chaîne — pendant singulier du garde-fou pluriel
    (« Articles 74 et 75… ») déjà en place. Avant ce correctif, capturé comme
    numéro nu « 182 » de contenu « , 183 et 184. », doublon avec l'article
    182 de base."""
    assert ARTICLE_PATTERN.match("article 182, 183 et 184.") is None


def test_article_pattern_virgule_ne_fait_pas_reculer_sur_un_numero_plus_court():
    """Non-régression : le rejet de la citation par virgule ne doit pas
    laisser le moteur reculer sur un numéro plus court (« 182 » -> « 18 »),
    ce qui créerait un faux article « 18 » au lieu de ne rien matcher."""
    assert ARTICLE_PATTERN.match("article 182, 183 et 184.") is None
    # Un vrai article "18" reste bien sûr reconnu par ailleurs.
    m = ARTICLE_PATTERN.match("Article 18 : Disposition normale.")
    assert m is not None
    num, _ = _article_match_groups(m)
    assert num == "18"


def test_parse_hierarchy_ne_cree_pas_de_faux_article_depuis_une_citation():
    """Cas réel simplifié (Code Pénal, article 182) : la citation « article
    182, 183 et 184. » doit rester du contenu de l'article courant, pas
    devenir un article « 182 » fantôme en doublon."""
    texte = (
        "Article 181 :\n"
        "Sera coupable de trahison et puni de mort tout Congolais.\n"
        "Article 182 :\n"
        "Sera coupable d'espionnage tout Congolais qui, pour une puissance étrangère,\n"
        "aura livré des renseignements visés aux\n"
        "article 182, 183 et 184.\n"
        "Article 183 :\n"
        "Disposition suivante.\n"
    )
    hierarchy = LegalDocumentParser(text_content=texte).parse_hierarchy()
    articles = _articles(hierarchy)
    numeros = [a["number"] for a in articles]
    assert numeros == ["181", "182", "183"]


# --- Tiret séparateur des Actes uniformes OHADA -----------------------------

def test_article_pattern_ne_met_pas_le_tiret_separateur_dans_le_numero():
    """Cas réel exact (AUDCG 2010) : le tiret sépare le numéro du corps."""
    m = ARTICLE_PATTERN.match("ARTICLE 1- Tout commerçant est soumis au présent Acte uniforme.")
    assert m is not None
    num, content = _article_match_groups(m)
    assert num == "1"
    assert content == "Tout commerçant est soumis au présent Acte uniforme."


def test_article_pattern_ne_devore_pas_linitiale_apres_un_tiret_separateur():
    """Cas réel exact (AUDCG 2010) : « L » appartient à « L'acte », pas au numéro."""
    m = ARTICLE_PATTERN.match("ARTICLE 3- L'acte de commerce par nature est celui par lequel...")
    assert m is not None
    num, content = _article_match_groups(m)
    assert num == "3"
    assert content == "L'acte de commerce par nature est celui par lequel..."


def test_article_pattern_garde_le_numero_compose_apres_un_tiret():
    """Le correctif ne transforme pas un vrai article 853-1 en article 853."""
    m = ARTICLE_PATTERN.match("ARTICLE 853-1- Les statuts peuvent prévoir une société par actions simplifiée.")
    assert m is not None
    num, content = _article_match_groups(m)
    assert num == "853-1"
    assert content == "Les statuts peuvent prévoir une société par actions simplifiée."


def test_article_pattern_garde_un_suffixe_lettre_sans_tiret():
    m = ARTICLE_PATTERN.match("ARTICLE 89A : Première disposition annexe.")
    assert m is not None
    num, content = _article_match_groups(m)
    assert num == "89A"
    assert content == "Première disposition annexe."


def test_article_pattern_restitue_le_mot_initial_meme_sans_espace_apres_le_tiret():
    for heading, numero, contenu in (
        ("ARTICLE 117-A défaut d'accord écrit entre les parties.", "117", "A défaut d'accord écrit entre les parties."),
        ("ARTICLE 133-Le preneur respecte les clauses du bail.", "133", "Le preneur respecte les clauses du bail."),
    ):
        m = ARTICLE_PATTERN.match(heading)
        assert m is not None
        num, content = _article_match_groups(m)
        assert num == numero
        assert content == contenu


def test_article_pattern_rejette_une_citation_plurielle_en_plage_tiretee():
    """Cas réel exact (AUPC 2015) : la plage 5-11 ne devient pas un article 5."""
    assert ARTICLE_PATTERN.match("articles 5-11, 11-1 ou 33-1 ci-dessus") is None


def test_parse_hierarchy_accepte_un_marqueur_article_precede_dun_point_ocr():
    """Cas réel exact (AUDCG 2010) : l'article 115 ne doit plus disparaître."""
    texte = (
        "ARTICLE 114- Le preneur est tenu aux réparations d'entretien.\n"
        ".ARTiCLE 115- A l'expiration du bail, le preneur verse une indemnité.\n"
        "ARTICLE 116- Les parties fixent librement le montant du loyer.\n"
    )
    hierarchy = LegalDocumentParser(text_content=texte).parse_hierarchy()
    articles = _articles(hierarchy)
    assert [a["number"] for a in articles] == ["114", "115", "116"]


def test_parse_hierarchy_ignore_les_marqueurs_internes_de_fusion():
    texte = (
        "<!-- chunk chunk_1_a_32.md -->\n"
        "Préambule officiel.\n"
        "ARTICLE 1- Première disposition.\n"
        "<!-- chunk chunk_33_a_64.md -->\n"
        "Suite de la première disposition.\n"
        "ARTICLE 2- Deuxième disposition.\n"
    )
    hierarchy = LegalDocumentParser(text_content=texte).parse_hierarchy()
    articles = _articles(hierarchy)
    assert "<!-- chunk" not in hierarchy[0]["content"]
    assert "<!-- chunk" not in articles[0]["content"]


def test_parse_hierarchy_ignore_le_sommaire_place_avant_lacte():
    texte = (
        "# ACTE UNIFORME PORTANT SUR LE DROIT COMMERCIAL GÉNÉRAL\n"
        "## SOMMAIRE\n"
        "LIVRE I : STATUT DU COMMERÇANT 6\n"
        "CHAPITRE I : DÉFINITION DU COMMERÇANT 6\n"
        "Section 1 - Immatriculation des personnes\n"
        "physiques et morales 21\n"
        "Le Conseil des Ministres de l'OHADA ;\n"
        "Vu le Traité relatif à l'harmonisation du droit des affaires ;\n"
        "ARTICLE 1- Première disposition.\n"
        "ARTICLE 2- Deuxième disposition.\n"
    )
    hierarchy = LegalDocumentParser(text_content=texte).parse_hierarchy()
    articles = _articles(hierarchy)
    types = [node["type"] for node in hierarchy]

    assert [article["number"] for article in articles] == ["1", "2"]
    assert types == ["PREAMBULE", "ARTICLE", "ARTICLE"]
    assert "Le Conseil des Ministres" in hierarchy[0]["content"]
    assert "physiques et morales 21" not in hierarchy[0]["content"]


def test_parse_hierarchy_repare_un_titre_romain_colle_par_ocr():
    texte = (
        "ARTICLE 72- Disposition précédente.\n"
        "## LIVRE III FICHIER NATIONAL\n"
        "## CHAPITRE IDISPOSITIONS GENERALES\n"
        "ARTICLE 73- Chaque État Partie organise un Fichier National.\n"
    )
    hierarchy = LegalDocumentParser(text_content=texte).parse_hierarchy()

    assert hierarchy[0]["type"] == "ARTICLE"
    assert hierarchy[0]["number"] == "72"
    livre = next(node for node in hierarchy if node["type"] == "LIVRE")
    chapitre = next(node for node in livre["children"] if node["type"] == "CHAPITRE")
    assert chapitre["number"] == "I"
    assert chapitre["title"] == "DISPOSITIONS GENERALES"
    assert chapitre["children"][0]["number"] == "73"
