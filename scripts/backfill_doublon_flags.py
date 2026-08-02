#!/usr/bin/env python3
"""Rattrapage phase 2 (docs/audit-ingestion-2026-08-02.md) : pose un
`CurationFlag` `article_doublon` individuel sur chaque article déjà renommé
`_doublon_N` par une ingestion ANTÉRIEURE au correctif de
`unique_article_number` (qui ne renommait alors jamais qu'en silence).

Aucun re-parsing, aucune re-ingestion : le numéro d'origine est retrouvé en
inversant le suffixe (`derive_numero_origine`, src/api/main.py), et le flag
est posé directement sur l'article déjà en base. Idempotent — un article déjà
flagué (`article_doublon`, non résolu) n'est pas re-flagué.

Usage :
    python scripts/backfill_doublon_flags.py              # dry-run (défaut)
    python scripts/backfill_doublon_flags.py --execute     # écrit réellement

Garde-fou : refuse d'écrire sur autre chose que 127.0.0.1:5433 (dev).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.api.main import derive_numero_origine  # noqa: E402
from src.db.models import Article, CurationFlag  # noqa: E402

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "5433")


def _guard_dev_only() -> None:
    if (DB_HOST, str(DB_PORT)) != ("127.0.0.1", "5433"):
        raise SystemExit(
            f"Refus : ce script n'écrit QUE sur la base de dev locale (127.0.0.1:5433), "
            f"pas sur {DB_HOST}:{DB_PORT}. Mission « corrections post-audit », règle absolue n°1."
        )


def _session():
    db_user = os.getenv("DB_USERNAME", "root")
    db_pass = os.getenv("DB_PASSWORD", "root")
    db_name = os.getenv("DB_DATABASE", "mibeko-db")
    url = f"postgresql://{db_user}:{db_pass}@{DB_HOST}:{DB_PORT}/{db_name}"
    engine = create_engine(url)
    return sessionmaker(bind=engine)()


def find_unflagged_renamed_articles(db) -> list:
    """Articles vivants suffixés `_doublon_N` sans flag `article_doublon` non
    résolu déjà posé pour eux."""
    already_flagged_ids = {
        row[0]
        for row in db.query(CurationFlag.article_id)
        .filter(CurationFlag.type_probleme == "article_doublon", CurationFlag.article_id.isnot(None))
        .all()
    }
    candidates = (
        db.query(Article)
        .filter(Article.deleted_at.is_(None), Article.numero_article.op("~")("_doublon_\\d+$"))
        .all()
    )
    return [a for a in candidates if a.id not in already_flagged_ids]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true", help="Écrit réellement (par défaut : dry-run, aucune écriture).")
    args = parser.parse_args()

    _guard_dev_only()
    db = _session()

    articles = find_unflagged_renamed_articles(db)
    print(f"Articles renommés sans flag : {len(articles)}")

    par_document = Counter(a.document_id for a in articles)
    print(f"Documents concernés : {len(par_document)}")

    if not args.execute:
        print("\nDRY-RUN : aucune écriture. Relancer avec --execute pour appliquer.")
        for article in articles[:20]:
            origine = derive_numero_origine(article.numero_article)
            print(f"  - {article.id} document={article.document_id} « {origine} » -> « {article.numero_article} »")
        if len(articles) > 20:
            print(f"  … et {len(articles) - 20} de plus.")
        db.rollback()
        return

    for article in articles:
        origine = derive_numero_origine(article.numero_article)
        db.add(CurationFlag(
            document_id=article.document_id,
            article_id=article.id,
            source="heuristic",
            type_probleme="article_doublon",
            severity="blocking",
            description=(
                f"Numéro d'article « {origine} » en collision avec un autre article du même "
                f"document — renommé en « {article.numero_article} » pour respecter la contrainte "
                "d'unicité. Nécessite une revue humaine (fusion, re-numérotation, ou confirmation "
                "qu'il s'agit bien de deux articles distincts). [Rattrapage phase 2 — ingéré avant "
                "le correctif de unique_article_number.]"
            ),
        ))

    db.commit()
    print(f"\n--execute : {len(articles)} flags créés sur {len(par_document)} documents (COMMIT).")


if __name__ == "__main__":
    main()
