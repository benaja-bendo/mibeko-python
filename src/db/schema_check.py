"""Détection de drift schéma DB (Laravel) vs modèles SQLAlchemy (P2.3).

Le schéma réel est piloté par les migrations Laravel ; ce service Python s'y
connecte en lecture/écriture avec ses propres modèles. Un décalage (colonne
renommée/supprimée côté Laravel, nullabilité changée) est déjà à l'origine
d'incidents en production, silencieusement, car ``init_db()`` est un no-op.

Ce module compare, au démarrage, les colonnes ATTENDUES par chaque modèle
SQLAlchemy (nom + nullabilité) à celles réellement présentes dans la base
(``information_schema.columns``). Il n'altère JAMAIS le schéma et ne bloque
JAMAIS le démarrage : il produit un rapport (``schema_ok`` + liste courte
d'écarts) consommé par ``/api/v1/health``.

La comparaison pure (``compare_schema``) est isolée de tout accès DB pour être
testable unitairement.
"""

import logging
from typing import Dict, List, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.db.database import Base

logger = logging.getLogger("mibeko.db")

# On borne la liste d'écarts remontée dans /health : au-delà, le signal utile
# (« le schéma diverge ») est déjà donné, inutile de déverser des centaines de
# lignes dans une réponse publique mise en cache.
MAX_REPORTED_ISSUES = 20


def expected_columns_from_models() -> Dict[str, Dict[str, bool]]:
    """Colonnes attendues par table : {table: {nom_colonne: nullable}}.

    On lit ``__table__.columns`` : ``column.name`` est le nom DB réel (ex.
    ``metadata`` pour l'attribut ``metadata_``), ``column.nullable`` la
    nullabilité déclarée. Une clé primaire est non-nullable même si ``nullable``
    n'est pas explicitement False.
    """

    # S'assure que les modèles sont enregistrés dans le registre (l'import peut
    # ne pas avoir eu lieu si on appelle cette fonction avant tout usage ORM).
    import src.db.models  # noqa: F401

    expected: Dict[str, Dict[str, bool]] = {}
    for mapper in Base.registry.mappers:
        table = mapper.local_table
        if table is None:
            continue
        columns: Dict[str, bool] = {}
        for column in table.columns:
            nullable = bool(column.nullable) and not column.primary_key
            columns[column.name] = nullable
        expected[table.name] = columns
    return expected


def fetch_actual_columns(db: Session, table_names: List[str]) -> Dict[str, Dict[str, bool]]:
    """Colonnes réelles en base pour les tables demandées : {table: {col: nullable}}.

    Interroge ``information_schema.columns`` du schéma courant. Une table absente
    en base ne figure tout simplement pas dans le résultat (dict vide côté actual).
    """

    if not table_names:
        return {}

    rows = db.execute(
        text(
            """
            SELECT table_name, column_name, is_nullable
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = ANY(:tables)
            """
        ),
        {"tables": list(table_names)},
    ).fetchall()

    actual: Dict[str, Dict[str, bool]] = {}
    for table_name, column_name, is_nullable in rows:
        actual.setdefault(table_name, {})[column_name] = (is_nullable == "YES")
    return actual


def compare_schema(
    expected: Dict[str, Dict[str, bool]],
    actual: Dict[str, Dict[str, bool]],
) -> List[str]:
    """Compare colonnes attendues vs réelles → liste d'écarts (pure, testable).

    Priorise les écarts DANGEREUX (une colonne attendue par le modèle mais absente
    en base : toute lecture/écriture la référençant plantera) devant les écarts de
    nullabilité (le modèle croit une colonne facultative alors qu'elle est NOT NULL
    en base, ou l'inverse). Les colonnes présentes en base mais inconnues du modèle
    ne sont PAS signalées : le modèle n'en dépend pas.
    """

    missing: List[str] = []
    nullability: List[str] = []

    for table_name in sorted(expected):
        expected_cols = expected[table_name]
        actual_cols = actual.get(table_name)

        if actual_cols is None:
            missing.append(f"{table_name} : table absente en base")
            continue

        for column_name in sorted(expected_cols):
            if column_name not in actual_cols:
                missing.append(f"{table_name}.{column_name} : colonne absente en base")
                continue
            expected_nullable = expected_cols[column_name]
            actual_nullable = actual_cols[column_name]
            if expected_nullable != actual_nullable:
                nullability.append(
                    f"{table_name}.{column_name} : nullabilité divergente "
                    f"(modèle={'NULL' if expected_nullable else 'NOT NULL'}, "
                    f"base={'NULL' if actual_nullable else 'NOT NULL'})"
                )

    # Danger d'abord (colonnes/tables manquantes), puis divergences de nullabilité.
    return missing + nullability


def check_schema(db: Session) -> Tuple[bool, List[str]]:
    """Exécute la comparaison complète contre la base → (schema_ok, écarts courts).

    Ne lève jamais : un souci d'accès à ``information_schema`` renvoie
    ``(True, [])`` (on ne bloque rien) mais logge l'échec.
    """

    try:
        expected = expected_columns_from_models()
        actual = fetch_actual_columns(db, list(expected.keys()))
        issues = compare_schema(expected, actual)
        return (len(issues) == 0), issues[:MAX_REPORTED_ISSUES]
    except Exception:
        logger.exception("Schema check : impossible de comparer le schéma DB aux modèles.")
        return True, []
