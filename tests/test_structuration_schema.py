"""Tests du schéma pydantic strict de la sortie LLM (étage 3).

Aucune insertion partielle en base : `validate_texte_juridique` doit lever
`ValidationError` sur toute sortie Mistral non conforme, avant que
`structure_document` ne touche la DB.
"""

import pytest
from pydantic import ValidationError

from src.structuration.schema import ArticleExtrait, TexteJuridique, validate_texte_juridique

CAS_VALIDE_COMPLET = {
    "nature": "Loi",
    "numero": "12-2026",
    "date_signature": "2026-06-01",
    "date_publication": "2026-06-15",
    "autorite": "Président de la République",
    "visas": ["Vu la Constitution", "Vu la loi n°1-2003"],
    "preambule": "Le Parlement a délibéré et adopté,",
    "articles": [
        {"numero": "1", "contenu": "La présente loi régit les relations de travail.", "page": 1},
        {"numero": "2", "contenu": "Elle s'applique à tous les travailleurs.", "page": 1},
    ],
    "annexes": [],
    "tableaux": [],
    "signature": "Fait à Brazzaville, le 1er juin 2026.",
}


def test_cas_valide_complet_est_accepte():
    texte = validate_texte_juridique(CAS_VALIDE_COMPLET)
    assert isinstance(texte, TexteJuridique)
    assert texte.nature == "Loi"
    assert len(texte.articles) == 2
    assert isinstance(texte.articles[0], ArticleExtrait)


def test_cas_minimal_avec_seuls_les_champs_obligatoires_est_accepte():
    minimal = {"nature": "Décret", "articles": []}
    texte = validate_texte_juridique(minimal)
    assert texte.nature == "Décret"
    assert texte.articles == []
    assert texte.visas == []
    assert texte.numero is None


def test_nature_manquante_leve_validation_error():
    invalide = {k: v for k, v in CAS_VALIDE_COMPLET.items() if k != "nature"}
    with pytest.raises(ValidationError) as exc_info:
        validate_texte_juridique(invalide)
    assert "nature" in str(exc_info.value)


def test_articles_manquant_leve_validation_error():
    invalide = {k: v for k, v in CAS_VALIDE_COMPLET.items() if k != "articles"}
    with pytest.raises(ValidationError) as exc_info:
        validate_texte_juridique(invalide)
    assert "articles" in str(exc_info.value)


def test_article_sans_numero_leve_validation_error():
    invalide = {
        **CAS_VALIDE_COMPLET,
        "articles": [{"contenu": "Contenu sans numéro d'article."}],
    }
    with pytest.raises(ValidationError):
        validate_texte_juridique(invalide)


def test_article_sans_contenu_leve_validation_error():
    invalide = {
        **CAS_VALIDE_COMPLET,
        "articles": [{"numero": "1"}],
    }
    with pytest.raises(ValidationError):
        validate_texte_juridique(invalide)


def test_nature_de_mauvais_type_leve_validation_error():
    invalide = {**CAS_VALIDE_COMPLET, "nature": 42}
    with pytest.raises(ValidationError):
        validate_texte_juridique(invalide)


def test_visas_absent_retombe_sur_liste_vide_par_defaut():
    sans_visas = {k: v for k, v in CAS_VALIDE_COMPLET.items() if k != "visas"}
    texte = validate_texte_juridique(sans_visas)
    assert texte.visas == []
