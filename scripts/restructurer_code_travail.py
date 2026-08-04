#!/usr/bin/env python3
"""Re-structure le Code du travail (id 052fc57e-9fe0-47c4-a4cf-454f9bef7ee1),
dont 76 des 246 articles ont un contenu vide en base.

Cause racine (diagnostic 2026-08-04, git archéologie + reproduction) : ce
document a été ingéré le 27/05/2026, dans la fenêtre où `LegalDocumentParser`
rangeait le texte d'un article tenant sur une seule ligne (sans continuation)
dans `title` au lieu de `content` — bug remplacé par une réécriture complète
du parseur le 10/06/2026 (commit 5100bda), jamais rejoué sur ce document.
Vérifié le 04/08/2026, en lecture seule sur la vraie prod : reparser le
markdown déjà stocké avec le parseur ACTUEL produit 251 articles, 0 vide
(contre 246 articles, 76 vides aujourd'hui) — voir la sortie de ce script en
dry-run pour la preuve à jour.

C'est un document isolé : recherche exhaustive sur tout le corpus prod vivant
(04/08/2026) — un seul autre document a un article à contenu vide (« LOI No
001-90 », 1 seul article, défaut différent et hors périmètre de ce script).

Mécanisme identique à `restructure_stock_codes.py` (remédiation du
02/08/2026, déjà exécutée avec succès sur 9 documents) : AUCUN re-parsing de
PDF, AUCUN appel LLM, AUCUNE écriture MinIO — le markdown déjà stocké
(EXTRACTION_MARKDOWN) est relu tel quel et reparsé avec `LegalDocumentParser`
actuel, puis `ingest_hierarchy` remplace la structure existante
(structure_nodes/articles/article_versions). `titre_officiel`, `type_code`,
les dates et `curation_status` (reste `draft`) ne sont pas touchés.

Vérifié en amont (lecture seule, 04/08/2026) sur ce document précis : 0
`curation_flags` non résolu, 0 flag `source='human'`, 0 référence dans
`dossier_articles`, jamais publié — rejeu sûr, rien à perdre.

Usage (dev) :
    python scripts/restructurer_code_travail.py                    # dry-run
    python scripts/restructurer_code_travail.py --execute           # écrit sur le dev local

Usage (prod) :
    python scripts/restructurer_code_travail.py --target prod                  # dry-run, profil PROD_RO_*
    python scripts/restructurer_code_travail.py --target prod --execute        # écrit en PROD, profil PROD_RW_DB_*

Garde-fous : dump frais requis avant tout --execute --target prod (voir
docs/infra/production.md § 4), saisie « PRODUCTION » exigée, refus si le
document est publié (garde-fou générique — sans objet ici, il est en draft).
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

DOCUMENT_ID = "052fc57e-9fe0-47c4-a4cf-454f9bef7ee1"


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


def restructure(db, minio_client, bucket: str, document: LegalDocument, execute: bool) -> Dict[str, Any]:
    base = {"document_id": str(document.id), "titre": document.titre_officiel}

    md_media = (
        db.query(MediaFile)
        .filter(MediaFile.document_id == document.id, MediaFile.file_category == "EXTRACTION_MARKDOWN")
        .first()
    )
    if md_media is None:
        return {**base, "statut": "sans_markdown"}

    response = minio_client.get_object(bucket, md_media.object_key)
    markdown_text = response.read().decode("utf-8", errors="ignore")
    response.close()
    response.release_conn()

    articles_avant = db.query(Article).filter(Article.document_id == document.id, Article.deleted_at.is_(None)).count()
    vides_avant = db.execute(
        text(
            "select count(*) from articles a"
            " join article_versions av on av.article_id = a.id and upper_inf(av.validity_period)"
            " where a.document_id = :doc_id and a.deleted_at is null"
            " and coalesce(trim(av.contenu_texte), '') = ''"
        ),
        {"doc_id": str(document.id)},
    ).scalar()
    flags_avant = (
        db.query(CurationFlag)
        .filter(CurationFlag.document_id == document.id, CurationFlag.resolved.is_(False))
        .count()
    )

    hierarchy = LegalDocumentParser(text_content=markdown_text).parse_hierarchy()
    articles_nodes = flatten_articles(hierarchy)
    articles_apres = len(articles_nodes)
    vides_apres_reparse = sum(1 for n in articles_nodes if not (n.get("content") or "").strip())

    resultat = {
        **base,
        "statut": "a_rejouer" if not execute else "rejoue",
        "articles_avant": articles_avant,
        "vides_avant": vides_avant,
        "articles_apres_reparse": articles_apres,
        "vides_apres_reparse": vides_apres_reparse,
        "flags_avant": flags_avant,
    }
    if not execute:
        return resultat

    document.metadata_ = {**(document.metadata_ or {}), "restructuration_2026_08_04": True}
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

    document = (
        db.query(LegalDocument)
        .filter(LegalDocument.id == DOCUMENT_ID, LegalDocument.deleted_at.is_(None))
        .first()
    )
    if document is None:
        raise SystemExit(f"Document introuvable sur la cible {args.target} : {DOCUMENT_ID}")

    if document.curation_status == "published":
        raise SystemExit(
            "ARRÊT — ce document est publié, hors garde-fou sans validation humaine explicite : "
            f"{document.id} {document.titre_officiel!r}"
        )

    if args.execute and args.target == "prod":
        print(f"\n  Le document {document.id} ({document.titre_officiel!r}) va voir sa structure "
              "(articles/structure_nodes) reconstruite en PRODUCTION.")
        saisie = input("Taper PRODUCTION pour confirmer : ").strip()
        if saisie != "PRODUCTION":
            print("Annulé.")
            sys.exit(1)
        db = _session_prod_ecriture(engine_ro)
        document = db.query(LegalDocument).filter(LegalDocument.id == DOCUMENT_ID).first()

    resultat = restructure(db, minio_client, bucket, document, execute=args.execute)

    if args.execute:
        db.commit()
        print(f"\n--execute ({args.target}) : modifications validées (COMMIT).")
    else:
        db.rollback()
        print(f"\nDRY-RUN ({args.target}) : aucune écriture (ROLLBACK). Relancer avec --execute pour appliquer.")

    print(resultat)


if __name__ == "__main__":
    main()
