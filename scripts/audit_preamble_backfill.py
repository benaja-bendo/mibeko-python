"""
Audit (LECTURE SEULE) du périmètre de backfill « préambule ».

Quantifie les documents qui n'ont PAS encore de feuille PREAMBULE, ventilés par :
  - origine : acte de Journal Officiel (sans artefact propre) vs document standard ;
  - re-parsabilité : présence d'un EXTRACTION_MARKDOWN/JSON propre (re-parse sans
    ré-OCR possible) ou non ;
  - curation_status : pour mesurer le RISQUE d'écraser un travail de curation
    manuelle (la ré-ingestion supprime puis recrée structure + articles).

N'écrit RIEN en base (aucun commit, aucune mutation). À lancer manuellement :

    python3 scripts/audit_preamble_backfill.py
"""

import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db.database import SessionLocal  # noqa: E402
from src.db.models import Article, LegalDocument, MediaFile  # noqa: E402

ARTIFACT_CATEGORIES = ("EXTRACTION_MARKDOWN", "EXTRACTION_JSON")


def main() -> None:
    db = SessionLocal()
    try:
        # Documents vivants : (id, est_acte_JO, curation_status).
        documents = (
            db.query(
                LegalDocument.id,
                LegalDocument.official_journal_id,
                LegalDocument.curation_status,
            )
            .filter(LegalDocument.deleted_at.is_(None))
            .all()
        )

        # Documents possédant DÉJÀ une feuille préambule (rien à faire).
        preamble_doc_ids = {
            row[0]
            for row in db.query(Article.document_id)
            .filter(Article.numero_article == "PREAMBULE")
            .distinct()
            .all()
        }

        # Documents avec un artefact d'extraction PROPRE (re-parse sans ré-OCR).
        artifact_doc_ids = {
            row[0]
            for row in db.query(MediaFile.document_id)
            .filter(MediaFile.file_category.in_(ARTIFACT_CATEGORIES))
            .distinct()
            .all()
        }

        total = len(documents)
        already = 0
        jo_no_artifact: Counter = Counter()        # actes JO sans artefact → ré-OCR/re-upload requis
        standard_reparsable: Counter = Counter()   # standard avec artefact → /parse possible
        standard_no_artifact: Counter = Counter()  # standard sans artefact → ré-OCR requis

        for doc_id, journal_id, curation in documents:
            if doc_id in preamble_doc_ids:
                already += 1
                continue
            curation = curation or "?"
            if journal_id is not None:
                jo_no_artifact[curation] += 1
            elif doc_id in artifact_doc_ids:
                standard_reparsable[curation] += 1
            else:
                standard_no_artifact[curation] += 1

        def dump(title: str, counter: Counter) -> None:
            subtotal = sum(counter.values())
            print(f"\n{title} : {subtotal}")
            for status, n in sorted(counter.items(), key=lambda kv: -kv[1]):
                risk = "  ⚠ curation à risque" if status in ("validated", "published") else ""
                print(f"    - {status:<12} {n}{risk}")

        print("=" * 64)
        print("AUDIT BACKFILL PRÉAMBULE (lecture seule)")
        print("=" * 64)
        print(f"Documents vivants total           : {total}")
        print(f"  déjà dotés d'un préambule        : {already}")
        print(f"  à traiter                        : {total - already}")

        dump("Actes de JO SANS artefact (ré-OCR ou re-upload md/json requis)", jo_no_artifact)
        dump("Standards re-parsables (/parse, sans ré-OCR)", standard_reparsable)
        dump("Standards SANS artefact (ré-OCR requis)", standard_no_artifact)

        print(
            "\nNote : ré-ingérer un document SUPPRIME puis recrée sa structure et "
            "ses articles.\nLes lignes « validated »/« published » signalent un risque "
            "d'écraser de la curation manuelle."
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
