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
est vérifié et rapporté explicitement à chaque exécution).

Usage (dev — défaut) :
    python scripts/reingest_flat_journals.py                    # dry-run
    python scripts/reingest_flat_journals.py --execute           # écrit sur le dev local

Usage (prod — mission « état des lieux prod », 2026-08-02, phase 4) :
    python scripts/reingest_flat_journals.py --target prod                  # dry-run, profil PROD_RO_*
    python scripts/reingest_flat_journals.py --target prod --execute        # écrit en PROD, profil PROD_RW_*

Garde-fous :
- `--target dev` (défaut) : refuse d'écrire sur autre chose que 127.0.0.1:5433
  (dev), même principe que `tests/conftest.py`.
- `--target prod` dry-run : passe par `src.db.prod_readonly` (PROD_RO_DB_*),
  qui PROUVE la lecture seule avant toute requête (SQLSTATE 25006/42501).
  Lecture des artefacts markdown via MinIO PROD en lecture seule
  (PROD_RO_MINIO_*) — le script n'écrit JAMAIS dans MinIO (les nouveaux actes
  référencent les objets déjà stockés par l'ancien document plat), donc un
  profil lecture seule y suffit même en --execute.
- `--target prod --execute` : exige PROD_RW_DB_* exportées dans le shell
  (jamais dans un fichier — cf. `src/promotion/push_corpus.py`), vérifie que
  la cible d'écriture répond comme la cible de lecture seule (même base),
  puis exige de taper PRODUCTION pour confirmer. Aucune exception.

Procédure d'annulation : `pg_dump` complet horodaté pris automatiquement
AVANT toute écriture n'est PAS fait par ce script (le faire à la main avant
de lancer --execute, cf. runbook) — restaurer ce dump annule intégralement
l'opération. Ce script ne fait par ailleurs QUE des soft-deletes
(`deleted_at`) et des `resolved=true` sur les anciens documents/flags : sans
même restaurer, l'opération est réversible en remettant `deleted_at=NULL`
sur les documents/articles concernés et en supprimant les nouveaux documents
créés (identifiés par `metadata->>'routage_jo_corrige' = 'true'`).
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

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.api.main import split_official_journal_markdown  # noqa: E402
from src.db.models import Article, CurationFlag, LegalDocument, MediaFile  # noqa: E402
from src.structuration.journals import split_and_persist_journal_acts  # noqa: E402

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "5433")


def _guard_dev_only() -> None:
    if (DB_HOST, str(DB_PORT)) != ("127.0.0.1", "5433"):
        raise SystemExit(
            f"Refus : cible dev demandée mais l'environnement pointe {DB_HOST}:{DB_PORT}, "
            "pas 127.0.0.1:5433. Utiliser --target prod pour la production."
        )


def _session_dev():
    db_user = os.getenv("DB_USERNAME", "root")
    db_pass = os.getenv("DB_PASSWORD", "root")
    db_name = os.getenv("DB_DATABASE", "mibeko-db")
    url = f"postgresql://{db_user}:{db_pass}@{DB_HOST}:{DB_PORT}/{db_name}"
    engine = create_engine(url)
    return sessionmaker(bind=engine)(), None


def _session_prod_readonly():
    """Session PROD en lecture seule, lecture seule PROUVÉE avant tout usage."""
    from src.db.prod_readonly import SQLSTATE_LECTURE_SEULE, assert_read_only, charger_cible, creer_engine

    cible = charger_cible()
    engine = creer_engine(cible)
    sqlstate = assert_read_only(engine)
    if sqlstate != SQLSTATE_LECTURE_SEULE:
        raise SystemExit(f"Refus : lecture seule non prouvée par SQLSTATE {SQLSTATE_LECTURE_SEULE} (obtenu : {sqlstate}).")
    print(f"Préflight PROD : lecture seule prouvée ({cible.resume()}).")
    return sessionmaker(bind=engine)(), engine


def _session_prod_ecriture(engine_ro):
    """Session PROD en écriture — n'est appelée QUE juste avant un --execute
    confirmé. Vérifie que la cible RW répond comme la cible RO (même base),
    même garde que `push_corpus.py`."""
    from src.promotion.push_corpus import CibleProdAmbigue, ConfigurationProdManquante, charger_cible_ecriture

    try:
        engine_rw = charger_cible_ecriture()
    except (ConfigurationProdManquante, CibleProdAmbigue) as exc:
        raise SystemExit(f"Refus : {exc}")

    with engine_rw.connect() as cnx_rw, engine_ro.connect() as cnx_ro:
        compte_sql = "select count(*) from legal_documents"
        if cnx_rw.execute(text(compte_sql)).scalar() != cnx_ro.execute(text(compte_sql)).scalar():
            raise SystemExit(
                "Refus : la cible RW (PROD_RW_DB_*) ne répond pas comme la cible RO "
                "(PROD_RO_DB_*) — les deux profils ne visent pas la même base."
            )
    return sessionmaker(bind=engine_rw)()


def _minio_client(target: str):
    """MinIO en LECTURE SEULE dans les deux cibles : ce script n'écrit jamais
    d'objet, les nouveaux actes référencent les objets déjà stockés par
    l'ancien document plat (mêmes object_key/file_path)."""
    from minio import Minio

    if target == "dev":
        host = os.getenv("MINIO_HOST", "127.0.0.1").strip()
        port = os.getenv("MINIO_PORT", "9000").strip()
        access_key = os.getenv("MINIO_ACCESS_KEY", "")
        secret_key = os.getenv("MINIO_SECRET_KEY", "")
        secure = os.getenv("MINIO_SECURE", "false").lower() == "true"
    else:
        endpoint = os.getenv("PROD_RO_MINIO_ENDPOINT", "").strip()
        if not endpoint or ":" not in endpoint:
            raise SystemExit("Refus : PROD_RO_MINIO_ENDPOINT absente ou invalide (attendu host:port).")
        host, port = endpoint.split(":", 1)
        access_key = os.getenv("PROD_RO_MINIO_ACCESS_KEY", "")
        secret_key = os.getenv("PROD_RO_MINIO_SECRET_KEY", "")
        secure = os.getenv("PROD_RO_MINIO_SECURE", "false").lower() == "true"
        if port == os.getenv("MINIO_PORT", "9000").strip():
            raise SystemExit(f"Refus : PROD_RO_MINIO_ENDPOINT ({endpoint}) pointe le port du MinIO dev.")

    return Minio(f"{host}:{port}", access_key=access_key, secret_key=secret_key, secure=secure)


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


def reingest_one(db, minio_client, document: LegalDocument, execute: bool) -> Dict[str, Any]:
    base = {"document_id": str(document.id), "titre": document.titre_officiel}

    md_media = _media(db, document.id, "EXTRACTION_MARKDOWN")
    if md_media is None:
        return {**base, "statut": "sans_markdown"}

    try:
        response = minio_client.get_object(
            os.getenv("PROD_RO_MINIO_BUCKET") or os.getenv("MINIO_BUCKET", "mibeko-documents"),
            md_media.object_key,
        )
        markdown_bytes = response.read()
        response.close()
        response.release_conn()
    except Exception as exc:
        return {**base, "statut": "markdown_illisible_minio", "erreur": str(exc)[:200]}
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
    parser.add_argument("--target", choices=["dev", "prod"], default="dev", help="Cible (défaut : dev).")
    args = parser.parse_args()

    engine_ro = None
    if args.target == "dev":
        _guard_dev_only()
        db, _ = _session_dev()
    else:
        db, engine_ro = _session_prod_readonly()

    minio_client = _minio_client(args.target)

    perimetre = find_perimeter(db)
    print(f"Cible : {args.target}. Périmètre : {len(perimetre)} documents (FLUX, type_code=JO, curation_status != published).")

    publies_dans_le_perimetre = [d for d in perimetre if d.curation_status == "published"]
    stock_dans_le_perimetre = [d for d in perimetre if d.document_role == "STOCK"]
    if publies_dans_le_perimetre or stock_dans_le_perimetre:
        print("ARRÊT — intersection avec published/STOCK détectée, en dehors du périmètre autorisé sans validation humaine :")
        for d in publies_dans_le_perimetre + stock_dans_le_perimetre:
            print(f"  - {d.id} {d.titre_officiel!r} (curation_status={d.curation_status}, document_role={d.document_role})")
        sys.exit(1)

    if args.execute and args.target == "prod":
        print(f"\n  {len(perimetre)} documents PROD vont être scindés (soft-delete + nouveaux actes).")
        saisie = input("Taper PRODUCTION pour confirmer : ").strip()
        if saisie != "PRODUCTION":
            print("Annulé.")
            sys.exit(1)
        db = _session_prod_ecriture(engine_ro)
        # `find_perimeter` doit être rejoué sur la session d'écriture — la
        # session RO précédente ne partage pas sa transaction avec elle.
        perimetre = find_perimeter(db)

    resultats = [reingest_one(db, minio_client, document, execute=args.execute) for document in perimetre]

    if args.execute:
        db.commit()
        print(f"\n--execute ({args.target}) : modifications validées (COMMIT).")
    else:
        db.rollback()
        print(f"\nDRY-RUN ({args.target}) : aucune écriture (ROLLBACK). Relancer avec --execute pour appliquer.")

    for r in resultats:
        print(r)

    print("\nRésumé :", dict(Counter(r["statut"] for r in resultats)))
    if args.execute:
        total_actes = sum(r.get("nb_actes_crees", 0) for r in resultats)
        print(f"Total actes créés : {total_actes}")


if __name__ == "__main__":
    main()
