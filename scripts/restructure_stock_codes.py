#!/usr/bin/env python3
"""Remédiation 2026-08-02 phase 5 : rejoue la structuration des 9 documents
STOCK affectés par les trois défauts corrigés ce soir dans le parseur/la
détection d'anomalies (marqueur d'article dégradé par l'OCR, bandeau de page
fantôme, sous-numérotation bis/ter/tiret/décimale prise pour un doublon) —
285 des 652 flags `article_doublon` initiaux venaient de ces 9 documents.

Aucun re-parsing de PDF, AUCUN nouvel appel LLM, AUCUNE écriture MinIO : le
markdown déjà stocké (`EXTRACTION_MARKDOWN`) est relu tel quel et reparsé
avec `LegalDocumentParser` corrigé, puis `ingest_hierarchy` remplace la
structure existante (structure_nodes/articles/article_versions) — comme le
ferait un `structure-batch --force` s'il en avait le pouvoir (il ne l'a pas :
idempotent par document_key, cf. CLAUDE.md).

Vérifié en amont (lecture seule, 2026-08-02) : aucun de ces 9 documents ne
porte de revue humaine (0 article `is_verified`, 0 `reviewed_by`, 0 flag
`source='human'`, 0 référence dans un `dossier_articles`) — rejeu sûr, rien à
perdre. `document_role`/`titre_officiel`/`type_code`/dates ne sont PAS
touchés (seule la structure interne change) ; `curation_status` reste
`draft` (rien n'est publié parmi ces 9).

Usage (dev — défaut) :
    python scripts/restructure_stock_codes.py                    # dry-run
    python scripts/restructure_stock_codes.py --execute           # écrit sur le dev local

Usage (prod) :
    python scripts/restructure_stock_codes.py --target prod                  # dry-run, profil PROD_RO_*
    python scripts/restructure_stock_codes.py --target prod --execute        # écrit en PROD, profil PROD_RW_DB_* (lecture MinIO seule, PROD_RO_MINIO_* suffit)

Garde-fous : mêmes profils/vérifications que scripts/reingest_flat_journals.py
(dump frais requis avant tout --execute --target prod, saisie « PRODUCTION »
exigée, aucune exception).

Procédure d'annulation : un dump pris juste avant restaure l'état antérieur
intégralement. Sans dump, réversible manuellement — aucune donnée SOURCE
n'est perdue (le markdown reste en MinIO, inchangé), mais recréer la
structure PRÉCÉDENTE demanderait de restaurer le dump.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.api.main import ingest_hierarchy  # noqa: E402
from src.db.models import Article, CurationFlag, LegalDocument, MediaFile  # noqa: E402
from src.extractor.parser import LegalDocumentParser  # noqa: E402

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "5433")

# Périmètre figé (audit 2026-08-02) — volontairement une liste explicite, pas
# une requête dynamique : ce sont EXACTEMENT les 9 documents diagnostiqués,
# aucun autre document STOCK ne doit être touché par ce script.
PERIMETRE_IDS = [
    "a01036db-9512-46fa-9cfa-5f0f3c16e040",  # Code civil — Code civil Congo Brazzaville-2012-03-24
    "db2bd2a1-ddba-4c96-b0e0-b0c7228f9b2b",  # Code Pénal — Code_Penal_Congolais_01
    "5f1c4cbf-40a3-42a3-9f77-d5f59bc1c33f",  # Règlement n° 07/12-UEAC-066-CM-23
    "67925559-0250-4c72-929a-68e2530b4197",  # CODE PENAL — Penal-Code-1836
    "97ff3cd0-17bb-4b47-ad1c-ce8a8d2edb9b",  # LOI n° 9-2004
    "c6bd03ad-0a52-4b18-9e23-d4b7dc18c86e",  # Loi n° 28-2016
    "10ba37a8-30f4-4ded-be43-0035e2f4df7f",  # Code Communautaire — cemac-code-2001-route
    "52869fa4-8cb7-4404-a981-0e33a7143ea4",  # Loi n° 33-2020
    "9bb8fdf0-31b8-4b4d-97a6-aed6a83a7195",  # Loi n° 6-2019
]


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
    from src.db.prod_readonly import SQLSTATE_LECTURE_SEULE, assert_read_only, charger_cible, creer_engine

    cible = charger_cible()
    engine = creer_engine(cible)
    sqlstate = assert_read_only(engine)
    if sqlstate != SQLSTATE_LECTURE_SEULE:
        raise SystemExit(f"Refus : lecture seule non prouvée par SQLSTATE {SQLSTATE_LECTURE_SEULE} (obtenu : {sqlstate}).")
    print(f"Préflight PROD : lecture seule prouvée ({cible.resume()}).")
    return sessionmaker(bind=engine)(), engine


def _session_prod_ecriture(engine_ro):
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


def _minio_readonly(target: str):
    """MinIO en LECTURE SEULE dans les deux cibles : ce script ne fait AUCUN
    upload — il relit le markdown déjà stocké par l'ingestion d'origine."""
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

    return Minio(f"{host}:{port}", access_key=access_key, secret_key=secret_key, secure=secure)


def _markdown_bucket(target: str) -> str:
    return (os.getenv("PROD_RO_MINIO_BUCKET") if target == "prod" else os.getenv("MINIO_BUCKET", "mibeko-documents")) or "mibeko-documents"


def flatten_articles(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for node in nodes:
        if node["type"] == "ARTICLE":
            out.append(node)
        if node.get("children"):
            out.extend(flatten_articles(node["children"]))
    return out


def restructure_one(db, minio_client, bucket: str, document: LegalDocument, execute: bool) -> Dict[str, Any]:
    base = {"document_id": str(document.id), "titre": document.titre_officiel}

    md_media = (
        db.query(MediaFile)
        .filter(MediaFile.document_id == document.id, MediaFile.file_category == "EXTRACTION_MARKDOWN")
        .first()
    )
    if md_media is None:
        return {**base, "statut": "sans_markdown"}

    try:
        response = minio_client.get_object(bucket, md_media.object_key)
        markdown_text = response.read().decode("utf-8", errors="ignore")
        response.close()
        response.release_conn()
    except Exception as exc:
        return {**base, "statut": "markdown_illisible_minio", "erreur": str(exc)[:200]}

    articles_avant = (
        db.query(Article).filter(Article.document_id == document.id, Article.deleted_at.is_(None)).count()
    )
    flags_avant = (
        db.query(CurationFlag)
        .filter(CurationFlag.document_id == document.id, CurationFlag.resolved.is_(False))
        .count()
    )

    hierarchy = LegalDocumentParser(text_content=markdown_text).parse_hierarchy()
    articles_apres = len(flatten_articles(hierarchy))

    resultat = {
        **base,
        "statut": "a_rejouer" if not execute else "rejoue",
        "articles_avant": articles_avant,
        "articles_apres_reparse": articles_apres,
        "flags_avant": flags_avant,
    }
    if not execute:
        return resultat

    document.metadata_ = {**(document.metadata_ or {}), "restructuration_ocr_corrigee": True}
    ingest_hierarchy(db, document, hierarchy, run_id=None, media_id=md_media.id, validation_status="pending")

    resultat["flags_apres"] = (
        db.query(CurationFlag)
        .filter(CurationFlag.document_id == document.id, CurationFlag.resolved.is_(False))
        .count()
    )
    return resultat


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

    minio_client = _minio_readonly(args.target)
    bucket = _markdown_bucket(args.target)

    documents = (
        db.query(LegalDocument)
        .filter(LegalDocument.id.in_(PERIMETRE_IDS), LegalDocument.deleted_at.is_(None))
        .all()
    )
    print(f"Cible : {args.target}. Périmètre : {len(documents)}/{len(PERIMETRE_IDS)} documents trouvés.")
    if len(documents) != len(PERIMETRE_IDS):
        trouves = {str(d.id) for d in documents}
        manquants = [i for i in PERIMETRE_IDS if i not in trouves]
        print(f"ATTENTION : {len(manquants)} id(s) du périmètre introuvable(s) ou supprimé(s) : {manquants}")

    publies = [d for d in documents if d.curation_status == "published"]
    if publies:
        print("ARRÊT — un ou plusieurs documents du périmètre sont publiés, hors garde-fou sans validation humaine :")
        for d in publies:
            print(f"  - {d.id} {d.titre_officiel!r}")
        sys.exit(1)

    if args.execute and args.target == "prod":
        print(f"\n  {len(documents)} documents PROD vont voir leur structure (articles/structure_nodes) reconstruite.")
        saisie = input("Taper PRODUCTION pour confirmer : ").strip()
        if saisie != "PRODUCTION":
            print("Annulé.")
            sys.exit(1)
        db = _session_prod_ecriture(engine_ro)
        documents = (
            db.query(LegalDocument)
            .filter(LegalDocument.id.in_(PERIMETRE_IDS), LegalDocument.deleted_at.is_(None))
            .all()
        )

    resultats = [restructure_one(db, minio_client, bucket, document, execute=args.execute) for document in documents]

    if args.execute:
        db.commit()
        print(f"\n--execute ({args.target}) : modifications validées (COMMIT).")
    else:
        db.rollback()
        print(f"\nDRY-RUN ({args.target}) : aucune écriture (ROLLBACK). Relancer avec --execute pour appliquer.")

    for r in resultats:
        print(r)

    if args.execute:
        total_avant = sum(r.get("flags_avant", 0) for r in resultats if "flags_avant" in r)
        total_apres = sum(r.get("flags_apres", 0) for r in resultats if "flags_apres" in r)
        print(f"\nRésumé : {total_avant} flags article_doublon/manquant/bloc ouverts avant -> {total_apres} après.")


if __name__ == "__main__":
    main()
