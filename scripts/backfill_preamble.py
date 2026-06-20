"""
Backfill « préambule » pour les documents déjà ingérés (à lancer DANS
l'environnement déployé : DB Postgres + MinIO joignables).

⚠️ DESTRUCTIF À L'EXÉCUTION : ré-ingérer un document passe par
`clear_document_structure` qui SUPPRIME puis recrée structure + articles +
versions → toute curation manuelle est écrasée. Par sécurité :
  - DRY-RUN par défaut (aucune écriture) ; il faut `--apply` pour modifier ;
  - les documents `validated`/`published` sont ignorés sauf `--force`.

Deux modes (un seul à la fois) :

  # 1) Documents STANDARD (hors JO) ayant leur propre artefact md/json :
  #    re-parse SANS ré-OCR. Idéal et sûr pour le stock non curé.
  python3 scripts/backfill_preamble.py --standard [--apply] [--force] [--limit N]

  # 2) Un Journal Officiel, depuis le md/json d'origine que TU fournis
  #    (les actes de JO n'ont pas d'artefact propre → impossible sans la source) :
  python3 scripts/backfill_preamble.py --journal <UUID> --source chemin.md [--apply] [--force]

Lance d'abord `scripts/audit_preamble_backfill.py` (lecture seule) pour le périmètre.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db.database import SessionLocal  # noqa: E402
from src.db.models import (  # noqa: E402
    Article,
    LegalDocument,
    MediaFile,
    OfficialJournal,
)
from src.api.main import (  # noqa: E402
    create_or_update_jo_documents_from_markdown,
    extract_text_from_mineru_json,
    ingest_hierarchy,
    minio_service,
)
from src.extractor.parser import LegalDocumentParser  # noqa: E402

CURATED_STATUSES = {"validated", "published"}


def has_preamble(db, document_id) -> bool:
    return (
        db.query(Article)
        .filter(Article.document_id == document_id, Article.numero_article == "PREAMBULE")
        .first()
        is not None
    )


def pick_artifact(document: LegalDocument):
    """Retourne (media, format) en préférant le markdown au JSON, sinon (None, None)."""
    md = next((f for f in document.files if f.file_category == "EXTRACTION_MARKDOWN"), None)
    if md:
        return md, "md"
    js = next((f for f in document.files if f.file_category == "EXTRACTION_JSON"), None)
    if js:
        return js, "json"
    return None, None


def load_text_from_media(media: MediaFile, fmt: str) -> str:
    raw = minio_service.get_file_bytes(media.object_key)
    if not raw:
        return ""
    if fmt == "md":
        return raw.decode("utf-8", errors="ignore")
    data = json.loads(raw.decode("utf-8", errors="ignore"))
    return extract_text_from_mineru_json(data, with_page_markers=True)


def backfill_standard(db, apply: bool, force: bool, limit: int | None) -> None:
    documents = (
        db.query(LegalDocument)
        .filter(
            LegalDocument.deleted_at.is_(None),
            LegalDocument.official_journal_id.is_(None),
        )
        .order_by(LegalDocument.created_at.desc())
        .all()
    )

    touched = 0
    for document in documents:
        if limit is not None and touched >= limit:
            break
        if has_preamble(db, document.id):
            continue

        media, fmt = pick_artifact(document)
        if not media:
            print(f"  · SKIP  {document.id} — aucun artefact md/json")
            continue
        if document.curation_status in CURATED_STATUSES and not force:
            print(f"  · SKIP  {document.id} — {document.curation_status} (curation, --force pour forcer)")
            continue

        text = load_text_from_media(media, fmt)
        if not text:
            print(f"  · SKIP  {document.id} — artefact illisible/vide ({media.object_key})")
            continue

        hierarchy = LegalDocumentParser(text_content=text).parse_hierarchy()
        if not any(node["type"] == "PREAMBULE" for node in hierarchy):
            # Aucun préambule à gagner → on n'ouvre pas une ré-ingestion destructive.
            print(f"  · SKIP  {document.id} — aucun préambule détecté au re-parse")
            continue

        if apply:
            ingest_hierarchy(db, document, hierarchy, media_id=media.id, validation_status="pending")
            db.commit()
            print(f"  ✓ DONE  {document.id} — ré-ingéré (+préambule) [{document.titre_officiel[:60]}]")
        else:
            print(f"  → WOULD {document.id} — ré-ingérerait (+préambule) [{document.titre_officiel[:60]}]")
        touched += 1

    verb = "ré-ingérés" if apply else "à ré-ingérer (dry-run)"
    print(f"\nStandard : {touched} document(s) {verb}.")


def backfill_journal(db, journal_id: str, source_path: str, apply: bool, force: bool) -> None:
    journal = db.query(OfficialJournal).filter(OfficialJournal.id == journal_id).first()
    if not journal:
        print(f"Journal {journal_id} introuvable.")
        return
    if not os.path.exists(source_path):
        print(f"Fichier source introuvable : {source_path}")
        return

    raw = open(source_path, "rb").read()
    if source_path.lower().endswith(".json"):
        markdown_text = extract_text_from_mineru_json(json.loads(raw.decode("utf-8", errors="ignore")))
    else:
        markdown_text = raw.decode("utf-8", errors="ignore")
    if not markdown_text.strip():
        print("Source vide après lecture.")
        return

    acts = (
        db.query(LegalDocument)
        .filter(LegalDocument.official_journal_id == journal.id, LegalDocument.deleted_at.is_(None))
        .all()
    )
    curated = [a for a in acts if a.curation_status in CURATED_STATUSES]
    print(f"Journal {journal.number or journal.id} : {len(acts)} acte(s) existant(s), dont {len(curated)} curé(s).")
    if curated and not force:
        # La ré-ingestion reconstruit la structure → écrase d'éventuelles retouches
        # MANUELLES du texte des articles curés (le statut, lui, est préservé ci-dessous).
        print("  ⚠ Des actes sont validated/published : ré-ingérer reconstruit leurs articles")
        print("    et pourrait écraser des retouches manuelles. Relance avec --force pour les inclure.")
        return

    if apply:
        # Préserve le curation_status d'origine des actes pré-existants : la fonction
        # repasse tout à 'review', on restaure avant commit pour ne pas déclasser les publiés.
        previous_status = {a.id: a.curation_status for a in acts}
        created = create_or_update_jo_documents_from_markdown(db, journal, markdown_text, curation_status="review")
        for document in created:
            if document.id in previous_status:
                document.curation_status = previous_status[document.id]
        db.commit()
        with_preamble = sum(1 for d in created if has_preamble(db, d.id))
        print(f"  ✓ DONE  {len(created)} acte(s) ré-ingéré(s), {with_preamble} avec préambule "
              f"(statut de curation préservé pour les {len(previous_status)} actes existants).")
    else:
        print(f"  → WOULD ré-ingérer ~{len(acts)} acte(s) depuis {os.path.basename(source_path)} (dry-run).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill préambule (dry-run par défaut).")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--standard", action="store_true", help="Documents standards avec artefact propre")
    mode.add_argument("--journal", metavar="UUID", help="Un Journal Officiel (avec --source)")
    parser.add_argument("--source", help="Chemin du md/json d'origine du JO (mode --journal)")
    parser.add_argument("--apply", action="store_true", help="Écrit réellement (sinon dry-run)")
    parser.add_argument("--force", action="store_true", help="Inclut les documents validated/published")
    parser.add_argument("--limit", type=int, default=None, help="Limite le nombre de documents (mode --standard)")
    args = parser.parse_args()

    if args.journal and not args.source:
        parser.error("--journal requiert --source (chemin du md/json d'origine).")

    banner = "APPLY (écriture)" if args.apply else "DRY-RUN (aucune écriture)"
    print("=" * 64)
    print(f"BACKFILL PRÉAMBULE — {banner}")
    print("=" * 64)

    db = SessionLocal()
    try:
        if args.standard:
            backfill_standard(db, apply=args.apply, force=args.force, limit=args.limit)
        else:
            backfill_journal(db, args.journal, args.source, apply=args.apply, force=args.force)
    finally:
        db.close()


if __name__ == "__main__":
    main()
