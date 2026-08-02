#!/usr/bin/env python3
"""Rattrapage phase 1 (docs/audit-ingestion-2026-08-02.md) : ré-ingère les
Journaux officiels déjà en base sous forme de document plat unique, en les
scindant en actes distincts via `split_and_persist_journal_acts` — même cœur
que le correctif live de `structuration/structurer.py` (phase 0 : routage
manquant vers le découpeur en actes).

Ré-ingestion à partir des artefacts EXTRACTION_MARKDOWN déjà stockés dans
MinIO (`media_files`) — AUCUN re-parsing de PDF, AUCUN re-OCR, AUCUN nouvel
appel LLM : la date de publication et le rattachement `official_journal_id`
du document plat existant (déjà résolus, ou déjà NULL) sont directement
réutilisés pour chaque acte issu de son découpage.

Périmètre : document_role=FLUX, type_code=JO, curation_status != published
(critère objectif de l'audit phase 1 ; le croisement avec STOCK et published
est vérifié et rapporté explicitement — aucun des deux n'est concerné en
base de dev au 2026-08-02).

Usage :
    python scripts/reingest_flat_journals.py              # dry-run (défaut)
    python scripts/reingest_flat_journals.py --execute     # écrit réellement

Garde-fou : refuse d'écrire sur autre chose que 127.0.0.1:5433 (dev), même
principe que `tests/conftest.py` — mission phase 1, règle absolue n°1.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.api.main import split_official_journal_markdown  # noqa: E402
from src.db.models import Article, CurationFlag, LegalDocument, MediaFile  # noqa: E402
from src.services.minio_service import minio_service  # noqa: E402
from src.structuration.journals import split_and_persist_journal_acts  # noqa: E402

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


def find_perimeter(db) -> list:
    """Critère objectif (audit phase 1) : FLUX + type_code=JO + non publié."""
    return (
        db.query(LegalDocument)
        .filter(
            LegalDocument.deleted_at.is_(None),
            LegalDocument.document_role == "FLUX",
            LegalDocument.type_code == "JO",
            LegalDocument.curation_status != "published",
        )
        .order_by(LegalDocument.created_at)
        .all()
    )


def _media(db, document_id, category: str) -> Optional[MediaFile]:
    return (
        db.query(MediaFile)
        .filter(MediaFile.document_id == document_id, MediaFile.file_category == category)
        .first()
    )


def _media_ref(media: MediaFile) -> Dict[str, Any]:
    return {
        "object_key": media.object_key,
        "file_path": media.file_path,
        "original_filename": media.original_filename or "document",
        "size_bytes": media.file_size or 0,
        "checksum_sha256": media.checksum_sha256 or "",
    }


def reingest_one(db, document: LegalDocument, execute: bool) -> Dict[str, Any]:
    base = {"document_id": str(document.id), "titre": document.titre_officiel}

    md_media = _media(db, document.id, "EXTRACTION_MARKDOWN")
    if md_media is None:
        return {**base, "statut": "sans_markdown"}

    markdown_bytes = minio_service.get_file_bytes(md_media.object_key)
    if markdown_bytes is None:
        return {**base, "statut": "markdown_illisible_minio"}
    markdown_text = markdown_bytes.decode("utf-8", errors="ignore")

    pdf_media = _media(db, document.id, "SOURCE_PDF")
    if pdf_media is None:
        return {**base, "statut": "sans_pdf"}
    json_media = _media(db, document.id, "EXTRACTION_JSON")

    extracted_texts = split_official_journal_markdown(markdown_text)
    if len(extracted_texts) <= 1:
        return {**base, "statut": "un_seul_acte", "nb_actes": len(extracted_texts)}

    if not execute:
        return {**base, "statut": "scindable_dry_run", "nb_actes": len(extracted_texts)}

    basename = (md_media.original_filename or document.titre_officiel).rsplit(".", 1)[0]
    provenance = dict(document.metadata_ or {})
    provenance["routage_jo_corrige"] = True
    provenance["document_source_id"] = str(document.id)

    created = split_and_persist_journal_acts(
        db,
        markdown_text=markdown_text,
        basename=basename,
        official_journal_id=document.official_journal_id,
        date_publication=document.date_publication,
        curation_status="draft",
        pdf_media=_media_ref(pdf_media),
        md_media=_media_ref(md_media),
        json_media=_media_ref(json_media) if json_media else None,
        provenance=provenance,
    )

    if not created:
        # split_official_journal_markdown a trouvé >1 acte mais aucun n'a été
        # persisté (cas normalement impossible ici) : ne rien retirer.
        return {**base, "statut": "erreur_scission_sans_creation"}

    now = datetime.datetime.utcnow()
    document.deleted_at = now
    db.query(Article).filter(
        Article.document_id == document.id, Article.deleted_at.is_(None)
    ).update({Article.deleted_at: now}, synchronize_session=False)
    # `CurationFlag.resolved_at`/`resolved_by` existent en base (migrations
    # Laravel) mais ne sont pas mappés côté SQLAlchemy (drift documenté,
    # mibeko-python/CLAUDE.md) : seule la colonne `resolved`, réellement
    # mappée, est mise à jour ici.
    db.query(CurationFlag).filter(
        CurationFlag.document_id == document.id, CurationFlag.resolved.is_(False)
    ).update({CurationFlag.resolved: True}, synchronize_session=False)

    return {
        **base,
        "statut": "scinde",
        "nb_actes_crees": len(created),
        "nouveaux_ids": [str(d.id) for d in created],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true", help="Écrit réellement (par défaut : dry-run, aucune écriture).")
    args = parser.parse_args()

    _guard_dev_only()
    db = _session()

    perimetre = find_perimeter(db)
    print(f"Périmètre : {len(perimetre)} documents (FLUX, type_code=JO, curation_status != published).")

    publies_dans_le_perimetre = [d for d in perimetre if d.curation_status == "published"]
    stock_dans_le_perimetre = [d for d in perimetre if d.document_role == "STOCK"]
    if publies_dans_le_perimetre or stock_dans_le_perimetre:
        print("ARRÊT — intersection avec published/STOCK détectée, en dehors du périmètre autorisé sans validation humaine :")
        for d in publies_dans_le_perimetre + stock_dans_le_perimetre:
            print(f"  - {d.id} {d.titre_officiel!r} (curation_status={d.curation_status}, document_role={d.document_role})")
        sys.exit(1)

    resultats = [reingest_one(db, document, execute=args.execute) for document in perimetre]

    if args.execute:
        db.commit()
        print("\n--execute : modifications validées (COMMIT).")
    else:
        db.rollback()
        print("\nDRY-RUN : aucune écriture (ROLLBACK). Relancer avec --execute pour appliquer.")

    for r in resultats:
        print(r)

    print("\nRésumé :", dict(Counter(r["statut"] for r in resultats)))
    if args.execute:
        total_actes = sum(r.get("nb_actes_crees", 0) for r in resultats)
        print(f"Total actes créés : {total_actes}")


if __name__ == "__main__":
    main()
