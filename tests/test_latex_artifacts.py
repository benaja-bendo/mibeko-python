"""Tests du retrait des échappements LaTeX laissés par MinerU.

Jumeau de `tests/Unit/LatexArtifactCleanerTest.php` (mibeko-tableau-de-bord) :
les deux suites couvrent les MÊMES formes, relevées réellement dans le corpus
de développement le 07/08/2026 (`article_versions.contenu_texte` et
`legal_documents.titre_officiel`) — aucune n'est inventée. Si l'une des deux
implémentations change, l'autre doit changer avec.

Le contrat tenu ici : on déséchappe, on ne réinterprète pas. Ce dont on n'est
pas sûr (formule, devise, bruit OCR) reste INTACT plutôt que d'être dégradé.

Exécutable sans base :  python3 tests/test_latex_artifacts.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.extractor.latex_artifacts import analyser, strip_latex_artifacts  # noqa: E402
from src.extractor.parser import LegalDocumentParser  # noqa: E402
from src.extractor.text_quality import sanitize_legal_text  # noqa: E402

# (entrée, sortie attendue) — formes typographiques sûres.
FORMES_CONVERTIES = [
    ("le  $1^{\\text{er}}$  juin 1927", "le 1er juin 1927"),
    ("le  $1^{er}$  janvier", "le 1er janvier"),
    ("la  $1^{\\text{re}}$  chambre", "la 1re chambre"),
    ("le  $4^{\\mathrm{e}}$  échelon", "le 4e échelon"),
    ("—  $1^{\\circ}$  La déclaration", "— 1° La déclaration"),
    ("commis  $4^{\\circ}$  échelon", "commis 4° échelon"),
    ("Avis  $\\mathbf{n}^{\\circ}$  338", "Avis n° 338"),
    ("Avis  $\\mathfrak{n}^{\\circ}$  342", "Avis n° 342"),
    ("Avis  $\\pmb{\\mathrm{n}}^{\\circ}$  338", "Avis n° 338"),
    ("Décret  $\\mathbf{N}^{\\circ}$  56.674", "Décret N° 56.674"),
    ("arrêté  $\\mathfrak{n}^{\\circ}86 - 877$  du", "arrêté n°86 - 877 du"),
    ("le  $\\mathfrak{n}^{\\mathrm{o}}$  37", "le n° 37"),
    ("les  $\\mathbf{n}^{\\text{os}}$  1 et 2", "les nos 1 et 2"),
    ("plafond de  $10\\%$  du total", "plafond de 10% du total"),
    ("plafond de  $7 \\%$  du total", "plafond de 7 % du total"),
    ("gisement à  $276^{\\circ}43'$  du nord", "gisement à 276°43' du nord"),
    ("vitesse de  $40\\mathrm{km / h}$  max", "vitesse de 40km / h max"),
    ("complément de  $4 / 10^{\\circ}$  aux cadres", "complément de 4 / 10° aux cadres"),
]

# Formes qui doivent rester STRICTEMENT intactes.
FORMES_REFUSEES = [
    "un montant de 60 $ (cours le plus haut) à 45 $",
    "un total de $ 45.007.002 dont $ 12",
    "ligne A $100.000.000\nligne B $125.000.000\n$",
    "la formule  $\\frac{a}{b}$  donne",
    "soit  $\\mathcal{L}(\\mathbb{R})$  l’espace",
    "la mesure  $1^{\\prime \\prime}$  exacte",
    "le  $1^{*}$  janvier",
    "établissement  $\\mathbf{e}^{\\pm}$  au fonctionnement",
    "la puissance  $x^{n}$  vaut",
    "seuil  $b \\in \\mathbb{R}^{n}$  fixé",
    "flèche  $\\rightarrow$  vers",
    "le terme  $u_{n}$  converge",
]


def test_deseschappe_les_artefacts_typographiques():
    for avant, apres in FORMES_CONVERTIES:
        assert strip_latex_artifacts(avant) == apres, f"{avant!r} → {strip_latex_artifacts(avant)!r}"


def test_laisse_intact_ce_qui_n_est_pas_surement_convertible():
    for texte in FORMES_REFUSEES:
        assert strip_latex_artifacts(texte) == texte, f"{texte!r} a été modifié"


def test_signale_ce_qu_il_refuse_de_convertir():
    analyse = analyser("la formule  $\\frac{a}{b}$  et le  $1^{\\text{er}}$  juin")

    assert analyse.texte == "la formule  $\\frac{a}{b}$  et le 1er juin"
    assert analyse.convertis == [("$1^{\\text{er}}$", "1er")]
    assert "$\\frac{a}{b}$" in analyse.refuses


def test_n_apparie_jamais_deux_dollars_separes_par_un_saut_de_ligne():
    # Les tableaux de montants d'une convention minière alignent un « $ » par
    # ligne : les apparier effacerait des devises et fusionnerait des lignes.
    texte = "PRIX\n$1.582.400.000\n$918.000.000\n"

    assert strip_latex_artifacts(texte) == texte


def test_est_idempotent():
    une = strip_latex_artifacts("le  $1^{\\text{er}}$  juin, article  $2^{\\circ}$")

    assert strip_latex_artifacts(une) == une


def test_preserve_l_espacement_sans_en_inventer():
    assert strip_latex_artifacts("du$1^{\\text{er}}$juin") == "du1erjuin"
    assert strip_latex_artifacts("du $1^{\\text{er}}$ juin") == "du 1er juin"


def test_laisse_le_texte_sans_latex_rigoureusement_inchange():
    texte = "Article 1er : la présente loi entre en vigueur le 1° janvier.\n\nFait à Brazzaville."

    assert strip_latex_artifacts(texte) == texte


def test_le_marqueur_de_page_mineru_survit_intact():
    # `[[MIBEKO_PAGE:N]]` porte la citabilité par page : le nettoyage LaTeX ne
    # doit jamais l'altérer, sans quoi le tamponnage de page tombe en panne.
    texte = "[[MIBEKO_PAGE:14]]\nle  $1^{\\text{er}}$  juin\n[[MIBEKO_PAGE:15]]"

    assert strip_latex_artifacts(texte) == "[[MIBEKO_PAGE:14]]\nle 1er juin\n[[MIBEKO_PAGE:15]]"


def test_sanitize_legal_text_applique_le_deseschappement_avant_les_motifs_ocr():
    # Le passage LaTeX vient EN PREMIER : « N° o » ne se répare qu'une fois le
    # mode mathématique retiré (motif `\bN°\s*o\b` de OCR_ARTIFACT_REPLACEMENTS).
    assert sanitize_legal_text("Décret $\\mathbf{N}^{\\circ}$ o 59-243") == "Décret N° 59-243"


def test_le_parseur_deseschappe_le_texte_qu_il_recoit():
    # Point d'entrée unique des contenus d'articles, quel que soit l'appelant.
    parser = LegalDocumentParser(text_content="Article  $1^{\\text{er}}$ .- La loi entre en vigueur.")

    assert parser.extract_text() == "Article 1er .- La loi entre en vigueur."


# mibeko-dashboard#24 — régression trouvée le 09/08/2026 en vérifiant, avant
# exécution, le mapping produit par `mibeko:proposer-nettoyage-latex` sur la
# production : quand MinerU laisse le chiffre EN DEHORS du `$` et n'ouvre le
# mode mathématique que pour l'exposant seul, le nettoyeur reproduisait le
# blanc de séparation au lieu de coller le chiffre à l'exposant. Les 5 formes
# ci-dessous sont parmi les 7 occurrences réelles trouvées sur les 128
# candidats du lot de correction (`article_versions.contenu_texte`, versions
# publiées), pas des cas inventés. Jumelles des cas PHP de
# `tests/Unit/LatexArtifactCleanerTest.php`.
FORMES_BASE_COLLEE_A_EXPOSANT_NU = [
    ("paragraphe 1  $^{er}$  de la présente loi.", "paragraphe 1er de la présente loi."),
    ("les alinéas 1  $^{er}$  et 2 sont applicables", "les alinéas 1er et 2 sont applicables"),
    ("du livre 1  $^{er}$  .", "du livre 1er ."),
    ("au-delà du 8  $^{ème}$  degré", "au-delà du 8ème degré"),
    ("né avant le 180  $^{ème}$  jour", "né avant le 180ème jour"),
]


def test_colle_le_chiffre_externe_au_dollar_a_l_exposant_nu_qu_il_precede():
    for avant, apres in FORMES_BASE_COLLEE_A_EXPOSANT_NU:
        assert strip_latex_artifacts(avant) == apres, f"{avant!r} → {strip_latex_artifacts(avant)!r}"


def test_ne_colle_pas_quand_la_base_est_deja_a_l_interieur_du_dollar():
    # Cas déjà correct avant le correctif : la base ET l'exposant sont tous
    # deux dans le `$` — rien ne doit changer, seul le cas où la base est HORS
    # du `$` (ci-dessus) était fautif.
    assert strip_latex_artifacts("le  $1^{\\text{er}}$  juin 1927") == "le 1er juin 1927"


def test_ne_colle_pas_un_chiffre_externe_a_autre_chose_qu_un_exposant_nu():
    # Le chiffre "5" précède ici une portion qui n'est PAS un exposant nu
    # (elle contient \mathfrak{n}) : la règle de collage ne doit pas
    # s'appliquer, seul le comportement espacement habituel doit jouer.
    assert strip_latex_artifacts("acte 5  $\\mathfrak{n}^{\\circ}$  du registre") == "acte 5 n° du registre"


def test_le_parseur_detecte_un_article_dont_le_numero_etait_echappe():
    # Bénéfice collatéral du nettoyage en amont : la regex d'article voit
    # « Article 1er » là où elle lisait « Article $1^{\text{er}}$ ».
    hierarchy = LegalDocumentParser(
        text_content="Article  $1^{\\text{er}}$ .- La présente loi entre en vigueur.\n"
        "Article 2.- Elle sera publiée au Journal officiel."
    ).parse_hierarchy()

    articles = [node for node in hierarchy if node["type"] == "ARTICLE"]
    assert [node["number"] for node in articles] == ["1er", "2"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"  ✓ {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"  ✗ {test.__name__} : {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests passés")
    sys.exit(1 if failures else 0)
