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

import datetime
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from src.acquisition.manifest import Manifest, ManifestEntry
from src.api.main import (
    build_document_key,
    ingest_hierarchy,
    map_detected_type_to_type_code,
    merge_metadata,
    resolve_institution_id,
    resolve_legal_scope,
    split_official_journal_markdown,
    extract_french_date,
    extract_reference_from_title,
)
from src.db.models import ExtractionRun, LegalDocument, MediaFile, OfficialJournal
from src.extractor.parser import LegalDocumentParser
from src.services.minio_service import minio_service


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
    document: Optional[LegalDocument],
    pdf_s3_path: str,
    publication_date: Optional[datetime.date] = None,
) -> Optional[OfficialJournal]:
    """Crée (ou retrouve) la fiche JO et y rattache le document. Idempotent.

    Le titre vient du MANIFESTE (`Journal officiel n° <numero>-<annee>`), pas
    du LLM : sur un JO, l'extraction d'en-tête décrit souvent le premier acte
    du sommaire, pas le journal lui-même (constat : 51 JO sur 65 mal titrés).

    `document` est optionnel (audit docs/audit-ingestion-2026-08-02.md phase 1 :
    le découpage en actes n'a plus besoin d'un document unique préexistant) —
    dans ce cas `publication_date` doit être fournie explicitement. Quand un
    `document` est fourni, `document.date_publication` reste le repli historique.
    """
    resolved_date = publication_date or (document.date_publication if document else None)
    if not (entry.jo_numero and entry.jo_annee and resolved_date):
        return None

    number = str(entry.jo_numero)
    journal = (
        db.query(OfficialJournal)
        .filter(
            OfficialJournal.publication_date == resolved_date,
            OfficialJournal.number == number,
            OfficialJournal.deleted_at.is_(None),
        )
        .first()
    )
    if journal is None:
        journal = OfficialJournal(
            id=uuid.uuid4(),
            title=f"Journal officiel n° {entry.jo_numero}-{entry.jo_annee}",
            publication_date=resolved_date,
            # Le PDF du journal EST le PDF source du document : on pointe le
            # même objet MinIO, pas de double stockage.
            file_path=pdf_s3_path,
            transcription_status="completed",
            is_published=True,
            number=number,
        )
        db.add(journal)
        db.flush()

    if document is not None:
        document.official_journal_id = journal.id
    return journal


def split_and_persist_journal_acts(
    db: Session,
    *,
    markdown_text: str,
    basename: str,
    official_journal_id: Optional[uuid.UUID],
    date_publication: Optional[datetime.date],
    curation_status: str,
    pdf_media: Dict[str, Any],
    md_media: Dict[str, Any],
    json_media: Optional[Dict[str, Any]] = None,
    provenance: Optional[Dict[str, Any]] = None,
    run_source: str = "STRUCTURATION_LLM",
    run_meta: Optional[Dict[str, Any]] = None,
) -> List[LegalDocument]:
    """Scinde un markdown de Journal officiel en actes et les persiste en base.

    Cœur PARTAGÉ entre le pipeline live (`structuration.structurer`) et le
    script de rattrapage des JO déjà ingérés à plat (`scripts/reingest_flat_journals.py`)
    — audit docs/audit-ingestion-2026-08-02.md, phases 0-1 : `structure-batch`
    ne routait jamais vers le découpeur en actes, déjà éprouvé côté upload
    manuel (`split_official_journal_markdown`). Aucune écriture si un seul
    acte est détecté (renvoie `[]` — l'appelant décide alors de ne rien faire,
    le document existant restant un document unique légitime).

    `pdf_media`/`md_media`/`json_media` décrivent un stockage MinIO DÉJÀ
    effectué (clé objet, chemin, checksum, nom, taille) — cette fonction ne
    téléverse rien elle-même : le même triplet d'artefacts est référencé par
    N `media_files`, un jeu par acte, sans dupliquer le stockage.
    """
    extracted_texts = split_official_journal_markdown(markdown_text)
    if len(extracted_texts) <= 1:
        return []

    journal_institution_id = resolve_institution_id(db, "JO")
    created: List[LegalDocument] = []
    # Deux actes du MÊME markdown peuvent produire un titre strictement
    # identique (doublon d'OCR, en-tête répétée, ou vrai homonyme) —
    # constaté à l'exécution réelle de la phase 1 (« Journal officiel n°
    # 1-1958 »). Sans désambiguïsation, le second acte retrouverait le
    # document du premier via `document_key` et `ingest_hierarchy` en
    # écraserait le contenu (`clear_document_structure` vide la hiérarchie
    # avant réinsertion) : perte de données. Même idiome que
    # `unique_article_number`/`_doublon_N` (api/main.py:1022-1034) pour le
    # même problème à l'échelle de l'article.
    seen_document_keys: Dict[str, int] = {}

    for extracted in extracted_texts:
        title = extracted["titre"].strip()
        detected_type = extracted["type"]
        # NE PAS écrire dans `legal_documents.reference_nor` (colonne UNIQUE,
        # `uq_legal_documents_reference_nor`) : constaté à l'exécution réelle
        # de la phase 1 — un même numéro d'arrêté/décret (« 3705 ») se
        # retrouve sur des actes distincts de JO différents (numérotation non
        # globale), ce qui viole la contrainte dès le second acte homonyme.
        # La référence détectée reste utile pour la citation : conservée en
        # metadata (JSONB, non contraint) plutôt que perdue.
        reference_nor_detectee = extract_reference_from_title(title)
        signature_date = extract_french_date(title)
        # basename en suffixe : deux actes de titre identique dans deux JO
        # différents ne doivent jamais collisionner (même convention que le
        # repli `f"{nature} — {basename}"` de structurer.py:256).
        base_document_key = build_document_key("FLUX", None, f"{title} — {basename}")
        if base_document_key in seen_document_keys:
            seen_document_keys[base_document_key] += 1
            document_key = f"{base_document_key}_acte_{seen_document_keys[base_document_key]}"
        else:
            seen_document_keys[base_document_key] = 0
            document_key = base_document_key

        document = db.query(LegalDocument).filter(LegalDocument.document_key == document_key).first()
        if document is None:
            document = LegalDocument(
                id=uuid.uuid4(),
                official_journal_id=official_journal_id,
                institution_id=journal_institution_id,
                type_code=map_detected_type_to_type_code(detected_type, db),
                document_key=document_key,
                document_role="FLUX",
                titre_officiel=title,
                reference_nor=None,
                date_signature=signature_date,
                date_publication=date_publication,
                statut="vigueur",
                legal_scope=resolve_legal_scope(title),
                curation_status=curation_status,
                # Statut définitif fixé plus bas, une fois la hiérarchie de CET
                # acte connue (pas celle du JO entier) — jamais "completed" avant
                # de savoir si l'acte a effectivement produit un article.
                extraction_status="processing",
            )
            db.add(document)
            db.flush()
        else:
            document.official_journal_id = document.official_journal_id or official_journal_id
            document.curation_status = curation_status

        if provenance:
            merge_metadata(document, provenance)
        if reference_nor_detectee:
            merge_metadata(document, {"reference_nor_detectee": reference_nor_detectee})

        pdf_row = MediaFile(
            document_id=document.id, storage_provider="MINIO", bucket_name=minio_service.bucket_name,
            object_key=pdf_media["object_key"], file_path=pdf_media["file_path"],
            original_filename=pdf_media["original_filename"], mime_type="application/pdf",
            file_category="SOURCE_PDF", file_size=pdf_media["size_bytes"],
            checksum_sha256=pdf_media["checksum_sha256"],
            description="PDF source acquis par l'usine à textes (JO, acte détaché du routage corrigé)",
        )
        db.add(pdf_row)
        md_row = MediaFile(
            document_id=document.id, storage_provider="MINIO", bucket_name=minio_service.bucket_name,
            object_key=md_media["object_key"], file_path=md_media["file_path"],
            original_filename=md_media["original_filename"], mime_type="text/markdown",
            file_category="EXTRACTION_MARKDOWN", file_size=md_media["size_bytes"],
            checksum_sha256=md_media["checksum_sha256"],
            description="Extraction markdown (usine à textes)",
        )
        db.add(md_row)
        json_row = None
        if json_media:
            json_row = MediaFile(
                document_id=document.id, storage_provider="MINIO", bucket_name=minio_service.bucket_name,
                object_key=json_media["object_key"], file_path=json_media["file_path"],
                original_filename=json_media["original_filename"], mime_type="application/json",
                file_category="EXTRACTION_JSON", file_size=json_media["size_bytes"],
                checksum_sha256=json_media["checksum_sha256"],
                description="Extraction JSON MinerU (usine à textes)",
            )
            db.add(json_row)
        db.flush()

        run = ExtractionRun(
            document_id=document.id,
            source=run_source,
            status="succeeded",
            started_at=datetime.datetime.utcnow(),
            finished_at=datetime.datetime.utcnow(),
            markdown_media_file_id=md_row.id,
            json_media_file_id=json_row.id if json_row else None,
            meta=run_meta or {},
        )
        db.add(run)
        db.flush()

        act_content = extracted["contenu"]
        hierarchy = LegalDocumentParser(text_content=act_content).parse_hierarchy()
        if not hierarchy and act_content.strip():
            first_marker = re.search(r"\[\[MIBEKO_PAGE:(\d+)\]\]", act_content)
            hierarchy = [{
                "type": "ARTICLE",
                "number": "Unique",
                "title": "Texte intégral",
                "content": act_content.strip(),
                "page": int(first_marker.group(1)) if first_marker else None,
                "children": [],
            }]
        if hierarchy:
            ingest_hierarchy(db, document, hierarchy, run_id=run.id, media_id=md_row.id, validation_status="pending")
            # `ingest_hierarchy` a déjà écrit les `article_versions` via son
            # propre appel interne (run_id/media_id passés directement) — pas
            # de backfill séparé nécessaire ici, contrairement au chemin
            # d'upload manuel historique qui n'avait jamais câblé cette
            # provenance (constat audit phase 1).
            document.extraction_status = "completed"
        else:
            # Deux titres d'actes détectés côte à côte (bruit OCR, sommaire mal
            # filtré) laissent un acte sans aucun contenu entre eux : constaté
            # sur 32 documents du 02/08/2026, tous marqués "completed" à tort
            # (0 structure_nodes, 0 article) avant ce correctif. "failed" les
            # fait apparaître dans le filtre « Échecs » existant de
            # /editor/ingestion (Ingestion.tsx) au lieu de les faire passer
            # pour traités.
            document.extraction_status = "failed"

        created.append(document)

    return created


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
