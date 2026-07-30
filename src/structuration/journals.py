"""Rattachement des JO ingérés au référentiel `official_journals`.

Constat du rechargement complet du 21/07/2026 : le flux d'upload manuel crée
toujours la fiche `official_journals` (page /editor/journals), mais le pipeline
insérait les JO comme simples documents FLUX avec `official_journal_id = NULL`
— la page Journaux officiels restait donc vide après un chargement pipeline.

Deux entrées :
- `ensure_official_journal` : find-or-create + rattachement, appelé par la
  structuration pour chaque document `journal_officiel` ;
- `backfill_journals` : rattrapage idempotent des JO déjà en base (commande
  CLI `link-journals`), apparie manifeste ↔ document via le SHA-256.

Règle mission « n'invente jamais » : sans date de publication réelle (extraite
du texte) ou sans numéro au manifeste, AUCUNE fiche n'est créée — la date est
NOT NULL en base et une date inventée serait une fausse provenance. Ces cas
restent à compléter à la main via /editor/journals.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from src.acquisition.manifest import Manifest, ManifestEntry
from src.db.models import LegalDocument, MediaFile, OfficialJournal


def titre_jo_depuis_manifeste(entry: ManifestEntry, basename: str) -> str:
    """Titre officiel d'un document JO depuis le MANIFESTE (vérité terrain).

    Le LLM lit le sommaire et titre souvent le journal d'après son premier
    acte (« loi de finances n° 38-2023 » pour un JO entier) — constat : 51 JO
    sur 65 mal titrés au rechargement du 21/07/2026. Le nom de fichier suit la
    grammaire sgg `congo-jo-{annee}-{numero}(-sp)?(-volume-…)?(-2)?` : la
    variante (volume, numéro spécial, doublon) est conservée dans le titre pour
    garantir l'unicité du document_key entre volumes d'un même numéro.
    """
    base = f"Journal officiel n° {entry.jo_numero}-{entry.jo_annee}"
    for num in (str(entry.jo_numero), str(entry.jo_numero).zfill(2)):
        prefix = f"congo-jo-{entry.jo_annee}-{num}"
        if basename == prefix:
            return base
        if basename.startswith(prefix + "-"):
            variante = basename[len(prefix) + 1:].replace("-", " ")
            if variante == "sp":
                variante = "spécial"
            return f"{base} — {variante}"
    # Nom hors grammaire connue : basename complet, l'unicité prime.
    return f"{base} — {basename}"


def ensure_official_journal(
    db: Session,
    entry: ManifestEntry,
    document: LegalDocument,
    pdf_s3_path: str,
) -> Optional[OfficialJournal]:
    """Crée (ou retrouve) la fiche JO et y rattache le document. Idempotent.

    Le titre vient du MANIFESTE (`Journal officiel n° <numero>-<annee>`), pas
    du LLM : sur un JO, l'extraction d'en-tête décrit souvent le premier acte
    du sommaire, pas le journal lui-même (constat : 51 JO sur 65 mal titrés).
    """
    if not (entry.jo_numero and entry.jo_annee and document.date_publication):
        return None

    number = str(entry.jo_numero)
    journal = (
        db.query(OfficialJournal)
        .filter(
            OfficialJournal.publication_date == document.date_publication,
            OfficialJournal.number == number,
            OfficialJournal.deleted_at.is_(None),
        )
        .first()
    )
    if journal is None:
        journal = OfficialJournal(
            id=uuid.uuid4(),
            title=f"Journal officiel n° {entry.jo_numero}-{entry.jo_annee}",
            publication_date=document.date_publication,
            # Le PDF du journal EST le PDF source du document : on pointe le
            # même objet MinIO, pas de double stockage.
            file_path=pdf_s3_path,
            transcription_status="completed",
            is_published=True,
            number=number,
        )
        db.add(journal)
        db.flush()

    document.official_journal_id = journal.id
    return journal


def backfill_journals(db: Session, data_dir: Path, dry_run: bool = False) -> Dict[str, Any]:
    """Rattrape les JO déjà ingérés sans fiche. Apparie par SHA-256 (metadata),
    jamais par titre (le LLM titre souvent le JO d'après son premier acte)."""
    summary: Dict[str, Any] = {
        "rattaches": 0,
        "deja_rattaches": 0,
        "retitres": 0,
        "sans_date_ou_numero": [],
        "sans_pdf_minio": [],
        "introuvables_en_base": 0,
    }
    manifests_dir = data_dir / "manifests"
    for manifest_path in sorted(manifests_dir.glob("*.jsonl")):
        manifest = Manifest(manifest_path)
        for entry in manifest.iter_entries():
            if entry.type_source != "journal_officiel" or entry.statut != "structure":
                continue
            document = (
                db.query(LegalDocument)
                .filter(
                    LegalDocument.metadata_["sha256"].astext == entry.sha256,
                    LegalDocument.deleted_at.is_(None),
                )
                .first()
            )
            if document is None:
                # Normal pour les doublons absorbés en « deja_existant » : le
                # document en base porte le sha de l'entrée gagnante.
                summary["introuvables_en_base"] += 1
                continue
            # Retitrage depuis le manifeste : le LLM titre souvent le JO
            # d'après son premier acte. Le document_key stocké ne change pas
            # (colonne, pas de re-dérivation) — sans risque sur des brouillons.
            if entry.jo_numero and entry.jo_annee:
                titre_attendu = titre_jo_depuis_manifeste(entry, entry.id.split("/")[-1])
                if document.titre_officiel != titre_attendu:
                    if not dry_run:
                        document.titre_officiel = titre_attendu
                    summary["retitres"] += 1
            if document.official_journal_id is not None:
                summary["deja_rattaches"] += 1
                continue
            if not (entry.jo_numero and entry.jo_annee and document.date_publication):
                summary["sans_date_ou_numero"].append(entry.id)
                continue
            pdf_media = (
                db.query(MediaFile)
                .filter(
                    MediaFile.document_id == document.id,
                    MediaFile.file_category == "SOURCE_PDF",
                )
                .first()
            )
            if pdf_media is None or not pdf_media.file_path:
                summary["sans_pdf_minio"].append(entry.id)
                continue
            if not dry_run:
                ensure_official_journal(db, entry, document, pdf_media.file_path)
            summary["rattaches"] += 1
    if not dry_run:
        db.commit()
    return summary
