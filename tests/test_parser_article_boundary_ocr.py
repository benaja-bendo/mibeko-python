"""Remédiation 2026-08-02 phase 5 : le marqueur d'article n'est pas toujours
« Article N » propre — l'OCR le dégrade parfois en « Article : » (chiffre
perdu, rendu vide) ou en pluriel « Articles N : ». Avant ce correctif,
`ARTICLE_PATTERN` (src/extractor/parser.py) ne matchait ni l'un ni l'autre :
le texte de l'article suivant restait collé en fin de l'article précédent
(contenu jamais perdu, juste caché), ET créait une fausse alerte
`article_manquant` (le numéro « disparu » de la séquence).

Confirmé sur 9+ documents réels en prod (Arrêté n° 3277, 1817, 1834, 3831,
4303, arrêté conjoint affaires foncières, « décision Cour constitutionnelle »)
par un audit en lecture seule — cf. mémoire de session du 2026-08-02.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.extractor.parser import ARTICLE_PATTERN, LegalDocumentParser, _is_page_banner_noise  # noqa: E402


def _articles(hierarchy):
    return [n for n in hierarchy if n["type"] == "ARTICLE"]


# --- ARTICLE_PATTERN, cas positifs (déjà couverts avant le correctif) --------

def test_article_avec_numero_simple():
    m = ARTICLE_PATTERN.match("Article 42 : Le titulaire doit...")
    assert m
    assert m.group("num") == "42"
    assert m.group("content") == "Le titulaire doit..."


def test_article_premier():
    m = ARTICLE_PATTERN.match("Article premier : Est autorisée...")
    assert m
    assert m.group("num").upper() == "PREMIER"


# --- Cas réels corrigés par la remédiation phase 5 --------------------------

def test_article_numero_perdu_a_locr():
    """Cas réel : Arrêté n° 3277, article 43 caché dans l'article 42."""
    m = ARTICLE_PATTERN.match("Article : Obligations de neutralité et de confidentialité")
    assert m is not None
    assert m.group("num") is None
    assert m.group("content") == "Obligations de neutralité et de confidentialité"


def test_article_pluriel_avec_numero():
    """Cas réel : « décision pour saisir la Cour constitutionnelle », article 194."""
    m = ARTICLE_PATTERN.match(
        "Articles 194 : Les co-auteurs et les complices des personnes visées..."
    )
    assert m is not None
    assert m.group("num") == "194"
    assert m.group("content") == "Les co-auteurs et les complices des personnes visées..."


def test_article_format_deux_points_double():
    """Cas réel : Arrêté n° 1817, « Article: 89 : ... » (ponctuation inhabituelle)."""
    m = ARTICLE_PATTERN.match("Article: 89 : Le service de l'administration comprend...")
    assert m is not None
    assert m.group("num") == "89"


# --- Garde-fou : ne pas sur-matcher une prose qui commence par « Article » ---

def test_article_sans_separateur_ni_numero_non_matche():
    """Sans numéro ET sans séparateur explicite, ce n'est pas un en-tête
    d'article — juste une phrase qui commence par le mot « Article » (rare
    mais possible). Le séparateur est OBLIGATOIRE quand le numéro est absent,
    précisément pour éviter ce faux positif."""
    assert ARTICLE_PATTERN.match("Article scientifique publié dans la revue...") is None


def test_art_abrege_sans_numero_ni_separateur_non_matche():
    assert ARTICLE_PATTERN.match("Artisanat local et développement rural") is None


# --- Bout en bout : parse_hierarchy scinde bien 42/43 au lieu de les fusionner --

def test_parse_hierarchy_scinde_article_a_numero_perdu():
    texte = (
        "Article 42 : Le titulaire doit garantir "
        "l'installation de certaines infrastructures.\n"
        "Article : Obligations de neutralité et de confidentialité\n"
        "Le titulaire doit garantir la neutralité de son service.\n"
        "Article 44 : Le titulaire s'engage à respecter la réglementation.\n"
    )
    hierarchy = LegalDocumentParser(text_content=texte).parse_hierarchy()
    articles = _articles(hierarchy)
    numeros = [a["number"] for a in articles]

    assert len(articles) == 3
    assert numeros[0] == "42"
    assert numeros[2] == "44"
    # Le 2e article n'a pas de numéro lisible : ingest_hierarchy lui donnera un
    # identifiant SANS_NUM_xxxxx (cf. main.py) — ici on vérifie juste qu'il
    # existe comme article À PART, avec le bon contenu, pas fusionné au 42.
    assert numeros[1] == ""
    assert "Obligations de neutralité" in articles[1]["content"]
    assert "Obligations de neutralité" not in articles[0]["content"]


# --- Bandeau de page fantôme (Code civil, prod, 144 cas confirmés) ----------

def test_is_page_banner_noise_page_seule():
    assert _is_page_banner_noise("p.11") is True


def test_is_page_banner_noise_page_plus_titre():
    """Cas réel exact : Code civil, article « 1 »."""
    assert _is_page_banner_noise("p.9\nTitre Ier : Des droits civils") is True


def test_is_page_banner_noise_vide():
    assert _is_page_banner_noise("") is True


def test_is_page_banner_noise_faux_pour_vrai_texte():
    assert _is_page_banner_noise("Les lois entrent en vigueur...") is False


def test_is_page_banner_noise_faux_si_page_suivie_de_vrai_texte():
    """Un vrai article NE DOIT PAS être pris pour du bruit simplement parce
    qu'il commence par une ligne « p.N » — seul un contenu ENTIÈREMENT
    composé de bruit compte."""
    assert _is_page_banner_noise("p.11\nAucune information ne peut être...") is False


def test_parse_hierarchy_fusionne_bandeau_de_page_fantome():
    """Cas réel (Code civil Congo-Brazzaville, prod) : un « article » fantôme
    ne contenant qu'un bandeau de page précède le vrai article de même
    numéro — avant ce correctif, `unique_article_number` le renommait en
    « 16-8_doublon_1 » et créait un flag `article_doublon` sur un article qui
    n'existe pas vraiment."""
    texte = (
        "Article 16-7 : Contenu de l'article précédent.\n"
        "Article 16-8\n"
        "p.11\n"
        "Article 16-8\n"
        "Aucune information permettant d'identifier le donneur ne peut être divulguée.\n"
        "Article 16-9 : Contenu de l'article suivant.\n"
    )
    hierarchy = LegalDocumentParser(text_content=texte).parse_hierarchy()
    articles = _articles(hierarchy)
    numeros = [a["number"] for a in articles]

    assert numeros == ["16-7", "16-8", "16-9"]  # le fantôme a disparu, pas de doublon
    assert "Aucune information" in articles[1]["content"]
    assert "p.11" not in articles[1]["content"]


def test_parse_hierarchy_ecarte_bandeau_meme_si_numero_suivant_differe():
    """Un « article » qui n'est QUE du bruit de bandeau de page est écarté
    inconditionnellement — même si le numéro suivant diffère. Un vrai article
    n'est jamais réduit à un simple numéro de page ; il n'y a donc rien à
    perdre à toujours l'écarter, quel que soit ce qui suit."""
    texte = (
        "Article 16-8\n"
        "p.11\n"
        "Article 16-9 : Contenu réel de l'article suivant.\n"
    )
    hierarchy = LegalDocumentParser(text_content=texte).parse_hierarchy()
    articles = _articles(hierarchy)
    numeros = [a["number"] for a in articles]

    assert numeros == ["16-9"]
    assert "Contenu réel" in articles[0]["content"]


def test_parse_hierarchy_fusionne_bandeau_avec_rappel_de_chapitre_entre_les_deux():
    """Cas réel EXACT (Code civil Congo-Brazzaville, prod, ligne 721-732 du
    markdown source) : entre le bandeau fantôme et le vrai article s'intercale
    un rappel de Titre/Chapitre courant DUPLIQUÉ (lui-même un artefact de
    bandeau de page) — `open_structure` referme l'article via `close_article`
    AVANT que `open_article` ne revoie le même numéro. La détection de bruit
    doit donc vivre dans `close_article` lui-même, pas dans `open_article`
    (149/152 des cas réels suivaient exactement ce schéma, pas le cas simple
    testé ci-dessus)."""
    texte = (
        "Article 16-7 : Contenu de l'article précédent.\n"
        "Chapitre IV : De l'utilisation des techniques d'imagerie cérébrale\n"
        "Article 16-8\n"
        "p.11\n"
        "Chapitre IV : De l'utilisation des techniques d'imagerie cérébrale\n"
        "Article 16-8\n"
        "Aucune information permettant d'identifier le donneur ne peut être divulguée.\n"
        "Article 16-9 : Les dispositions du présent chapitre sont d'ordre public.\n"
    )
    hierarchy = LegalDocumentParser(text_content=texte).parse_hierarchy()

    def flatten(nodes):
        out = []
        for n in nodes:
            if n["type"] == "ARTICLE":
                out.append(n)
            out.extend(flatten(n.get("children", [])))
        return out

    articles = flatten(hierarchy)
    numeros = [a["number"] for a in articles]

    assert numeros == ["16-7", "16-8", "16-9"]  # un seul « 16-8 », pas de doublon
    seize_huit = articles[numeros.index("16-8")]
    assert "Aucune information" in seize_huit["content"]
    assert "p.11" not in seize_huit["content"]
