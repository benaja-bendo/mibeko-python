"""Tests du détecteur de drift schéma DB↔modèles (P2.3).

On teste la comparaison PURE (`compare_schema`) sur des dicts fabriqués, plus
l'extraction des colonnes attendues depuis les modèles SQLAlchemy réels
(`expected_columns_from_models`). Aucun accès DB : la comparaison est isolée.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db.schema_check import compare_schema, expected_columns_from_models  # noqa: E402


# ---------------------------------------------------------------------------
# compare_schema (pure)
# ---------------------------------------------------------------------------

def test_aucun_ecart_quand_base_et_modeles_coincident():
    expected = {"docs": {"id": False, "titre": False, "note": True}}
    actual = {"docs": {"id": False, "titre": False, "note": True}}
    assert compare_schema(expected, actual) == []


def test_colonne_manquante_en_base_est_signalee():
    expected = {"docs": {"id": False, "schema_ok": True}}
    actual = {"docs": {"id": False}}  # schema_ok absente en base
    issues = compare_schema(expected, actual)
    assert len(issues) == 1
    assert "docs.schema_ok" in issues[0]
    assert "absente en base" in issues[0]


def test_table_entierement_absente_est_signalee():
    expected = {"docs": {"id": False}, "orphan": {"id": False}}
    actual = {"docs": {"id": False}}  # table orphan absente
    issues = compare_schema(expected, actual)
    assert any("orphan : table absente en base" in issue for issue in issues)


def test_nullabilite_divergente_est_signalee():
    expected = {"docs": {"id": False, "note": True}}   # modèle : note nullable
    actual = {"docs": {"id": False, "note": False}}    # base : note NOT NULL
    issues = compare_schema(expected, actual)
    assert len(issues) == 1
    assert "docs.note" in issues[0]
    assert "nullabilité divergente" in issues[0]


def test_colonne_en_trop_en_base_est_ignoree():
    # Le modèle n'en dépend pas : une colonne supplémentaire côté Laravel n'est
    # pas un problème pour Python.
    expected = {"docs": {"id": False}}
    actual = {"docs": {"id": False, "extra_laravel": True}}
    assert compare_schema(expected, actual) == []


def test_les_colonnes_manquantes_priment_sur_la_nullabilite():
    # Deux familles d'écarts : le danger (colonne absente) doit apparaître AVANT
    # la simple divergence de nullabilité.
    expected = {"docs": {"note": True, "absente": False}}
    actual = {"docs": {"note": False}}  # note NOT NULL en base, absente manquante
    issues = compare_schema(expected, actual)
    assert len(issues) == 2
    assert "absente en base" in issues[0]        # danger d'abord
    assert "nullabilité divergente" in issues[1]  # puis nullabilité


# ---------------------------------------------------------------------------
# expected_columns_from_models (modèles réels)
# ---------------------------------------------------------------------------

def test_extraction_couvre_les_tables_du_domaine():
    expected = expected_columns_from_models()
    for table in ("legal_documents", "official_journals", "articles", "extraction_runs"):
        assert table in expected, f"table {table} manquante dans l'extraction"


def test_extraction_utilise_le_nom_de_colonne_db_pas_l_attribut():
    # L'attribut Python `metadata_` est mappé sur la colonne DB `metadata`.
    cols = expected_columns_from_models()["legal_documents"]
    assert "metadata" in cols
    assert "metadata_" not in cols


def test_extraction_marque_la_pk_non_nullable():
    cols = expected_columns_from_models()["legal_documents"]
    assert cols["id"] is False
    # Une colonne réellement facultative reste nullable.
    assert cols["reference_nor"] is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
