"""Orchestrateur de structuration d'un document (étage 3 de l'usine à textes).

Combine le parseur heuristique (source de vérité pour le contenu structurel :
articles, préambule, signature, tableaux) et le LLM Mistral (source de vérité
UNIQUEMENT pour les métadonnées d'en-tête : nature/numero/dates/autorité).
Sortie validée par `schema.validate_texte_juridique` avant toute écriture DB
(règle mission : jamais d'insertion partielle, transaction unique en fin de
fonction, rollback complet sur échec).

`ingest_hierarchy` est importée depuis `src.api.main` : ce module ne connecte
pas la DB ni ne démarre de serveur à l'import (`init_db` est un no-op, cf.
src/db/database.py), et `scripts/backfill_preamble.py` l'importe déjà de la
même façon en usage réel — précédent suivi ici plutôt que dupliquer la
logique d'insertion structurelle.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from src.acquisition.manifest import ManifestEntry, sha256_file
from src.api.main import build_document_key, ingest_hierarchy, merge_metadata, resolve_legal_scope, sanitize_path_component
from src.db.models import CurationFlag, ExtractionRun, LegalDocument, MediaFile
from src.extractor.parser import LegalDocumentParser
from src.services.mistral_service import mistral_service as default_mistral_client
from src.structuration.schema import validate_texte_juridique

logger = logging.getLogger("mibeko.structuration")

STOCK_TYPE_SOURCES = {"code"}

# Mistral a l'interdiction d'inventer : si l'en-tête ne permet pas de déduire
# la nature, elle est reprise du manifeste (provenance connue et tracée, pas
# une invention). Les type_source sans équivalent (ex. lot_prive) restent sans
# nature → échec de validation → revue humaine.
NATURE_PAR_TYPE_SOURCE = {
    "journal_officiel": "Journal officiel",
    "code": "Code",
    "loi": "Loi",
    "decret": "Décret",
    "arrete": "Arrêté",
    "ordonnance": "Ordonnance",
    "traite_international": "Traité international",
}

MISTRAL_METADATA_INSTRUCTIONS = """Tu extrais les métadonnées d'en-tête d'un texte juridique congolais (Congo-Brazzaville).
Réponds UNIQUEMENT en JSON strict, avec EXACTEMENT ces clés : nature, numero, date_signature, date_publication, autorite.
Mets null pour toute valeur inconnue. N'invente JAMAIS une information absente du texte fourni :
appuie-toi UNIQUEMENT sur le texte donné, rien d'autre."""


def _resolve_artefact(data_dir: Path, kind: str, entry_id: str) -> Optional[Path]:
    """Localise un artefact pipeline (`md` ou `json`) avec repli legacy.

    L'usine range les artefacts sous `pipeline/<kind>/<source>/<nom>.<kind>` ;
    les documents traités avant l'usine sont à la racine `pipeline/<kind>/`
    (sans sous-dossier source) et sont réutilisés tels quels — zéro re-OCR
    pour eux (cf. docs/pipeline/01-plan.md §9, note d'intégration).
    """
    primary = data_dir / "pipeline" / kind / f"{entry_id}.{kind}"
    if primary.is_file():
        return primary
    legacy = data_dir / "pipeline" / kind / f"{entry_id.split('/')[-1]}.{kind}"
    if legacy.is_file():
        return legacy
    return None


def _extract_preamble_text(hierarchy: list) -> str:
    for node in hierarchy:
        if node["type"] == "PREAMBULE":
            return node.get("content", "")
    first_article_index = next(
        (i for i, node in enumerate(hierarchy) if node["type"] == "ARTICLE"), None
    )
    if first_article_index is None:
        return ""
    return "\n".join(node.get("content", "") or node.get("title", "") for node in hierarchy[:first_article_index])


def _collect_leaf_texts(hierarchy: list, node_type: str) -> list:
    return [node.get("content", "") for node in hierarchy if node["type"] == node_type]


def _flatten_articles(hierarchy: list) -> list:
    articles = []
    for node in hierarchy:
        if node["type"] == "ARTICLE":
            articles.append({
                "numero": node.get("number", ""),
                "contenu": node.get("content", ""),
                "page": node.get("page"),
            })
        if node.get("children"):
            articles.extend(_flatten_articles(node["children"]))
    return articles


async def _call_mistral_metadata(mistral_client, texte_entete: str) -> dict:
    return await mistral_client.extract_metadata(texte_entete, MISTRAL_METADATA_INSTRUCTIONS)


def structure_document(
    db: Session,
    data_dir: Path,
    entry: ManifestEntry,
    mistral_client=None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Structure un document (md+json déjà produits en Phase 2) et l'insère en base.

    Étapes 1-5 (parsing + appel LLM + validation) s'exécutent toujours ; l'étape
    6 (écriture DB) est sautée si `dry_run=True`.
    """
    client = mistral_client or default_mistral_client

    md_path = _resolve_artefact(data_dir, "md", entry.id)
    if md_path is None:
        primary = data_dir / "pipeline" / "md" / f"{entry.id}.md"
        legacy = data_dir / "pipeline" / "md" / f"{entry.id.split('/')[-1]}.md"
        return {"statut": "erreur", "document_id": None, "motif": f"markdown introuvable : {primary} (repli legacy tenté : {legacy})"}

    try:
        markdown_text = md_path.read_text(encoding="utf-8")
        hierarchy = LegalDocumentParser(text_content=markdown_text).parse_hierarchy()
    except Exception as exc:
        # Un document illisible/corrompu ne doit PAS faire planter tout le lot :
        # on renvoie une erreur traçable (comme pour le markdown introuvable) afin
        # que run_batch marque l'entrée `erreur` et poursuive (lot reprenable).
        return {"statut": "erreur", "document_id": None, "motif": f"lecture/parsing du markdown en échec : {exc}"}
    texte_entete = _extract_preamble_text(hierarchy)
    # Fallback d'en-tête : si le préambule extrait est vide ou trop pauvre
    # (< 200 caractères), on envoie au LLM le début du markdown brut, sans les
    # lignes de marqueurs [[MIBEKO_PAGE:N]] pour ne pas polluer le prompt.
    texte_entete_llm = texte_entete
    if len(texte_entete) < 200:
        sans_marqueurs = "\n".join(
            line for line in markdown_text.splitlines() if not line.strip().startswith("[[MIBEKO_PAGE:")
        )
        texte_entete_llm = sans_marqueurs[:3000]

    llm_metadata: Optional[dict] = None
    last_error: Optional[str] = None
    for attempt in range(2):
        try:
            llm_metadata = asyncio.run(_call_mistral_metadata(client, texte_entete_llm))
        except Exception as exc:
            last_error = f"appel Mistral en échec : {exc}"
            logger.warning("structuration %s : %s (tentative %d/2)", entry.id, last_error, attempt + 1)
            continue

        try:
            nature = llm_metadata.get("nature") or NATURE_PAR_TYPE_SOURCE.get(entry.type_source)
            nature_depuis_manifeste = llm_metadata.get("nature") is None and nature is not None
            candidate = {
                **llm_metadata,
                "nature": nature,
                "visas": [],
                "preambule": texte_entete or None,
                "articles": _flatten_articles(hierarchy),
                "annexes": [],
                "tableaux": _collect_leaf_texts(hierarchy, "TABLEAU"),
                "signature": "\n".join(_collect_leaf_texts(hierarchy, "SIGNATURE")) or None,
            }
            validate_texte_juridique(candidate)
            last_error = None
            break
        except Exception as exc:
            last_error = f"validation du schéma en échec : {exc}"
            logger.warning("structuration %s : %s (tentative %d/2)", entry.id, last_error, attempt + 1)

    if last_error:
        if not dry_run:
            # Règle mission : jamais d'insertion partielle, mais l'échec doit
            # rester traçable pour un humain (pas seulement dans le manifeste).
            db.add(CurationFlag(
                document_id=None,
                source="llm",
                type_probleme="structuration_echec_validation",
                severity="blocking",
                description=f"Structuration en échec pour « {entry.id} » : {last_error}",
            ))
            db.commit()
        return {"statut": "erreur", "document_id": None, "motif": last_error}

    if dry_run:
        return {"statut": "structure", "document_id": None, "motif": None}

    basename = entry.id.split("/")[-1]
    document_role = "STOCK" if entry.type_source in STOCK_TYPE_SOURCES else "FLUX"
    stock_code = sanitize_path_component(basename)[:100] if document_role == "STOCK" else None
    numero = llm_metadata.get("numero")
    if numero:
        titre_officiel = f"{nature} n° {numero}"
    else:
        # Le basename garantit l'unicité du document_key : deux JO intitulés
        # « Journal officiel » sans numéro d'acte entreraient sinon en collision.
        titre_officiel = f"{nature} — {basename}"
    document_key = build_document_key(document_role, stock_code, titre_officiel)

    existing_document = db.query(LegalDocument).filter(LegalDocument.document_key == document_key).first()
    if existing_document:
        return {"statut": "deja_existant", "document_id": existing_document.id, "motif": None}

    json_path = _resolve_artefact(data_dir, "json", entry.id)

    try:
        document = LegalDocument(
            titre_officiel=titre_officiel,
            document_role=document_role,
            document_key=document_key,
            stock_code=stock_code,
            reference_nor=llm_metadata.get("numero"),
            date_signature=_parse_iso_date(llm_metadata.get("date_signature")),
            date_publication=_parse_iso_date(llm_metadata.get("date_publication")),
            legal_scope=resolve_legal_scope(titre_officiel),
            curation_status="draft",
            extraction_status="completed",
        )
        provenance = {
            "source_url": entry.source_url,
            "fetched_at": entry.fetched_at,
            "sha256": entry.sha256,
            "autorite": llm_metadata.get("autorite"),
        }
        if nature_depuis_manifeste:
            provenance["nature_depuis_manifeste"] = True
        merge_metadata(document, provenance)
        db.add(document)
        db.flush()

        markdown_media = MediaFile(
            document_id=document.id,
            file_path=str(md_path),
            object_key=str(md_path),
            file_category="EXTRACTION_MARKDOWN",
            checksum_sha256=sha256_file(md_path),
            file_size=md_path.stat().st_size,
        )
        db.add(markdown_media)

        json_media = None
        if json_path is not None:
            json_media = MediaFile(
                document_id=document.id,
                file_path=str(json_path),
                object_key=str(json_path),
                file_category="EXTRACTION_JSON",
                checksum_sha256=sha256_file(json_path),
                file_size=json_path.stat().st_size,
            )
            db.add(json_media)
        db.flush()

        run = ExtractionRun(
            document_id=document.id,
            source="STRUCTURATION_LLM",
            status="succeeded",
            started_at=datetime.datetime.utcnow(),
            finished_at=datetime.datetime.utcnow(),
            markdown_media_file_id=markdown_media.id,
            json_media_file_id=json_media.id if json_media else None,
        )
        db.add(run)
        db.flush()

        ingest_hierarchy(db, document, hierarchy, run_id=run.id, media_id=markdown_media.id, validation_status="pending")

        db.commit()
    except Exception as exc:
        db.rollback()
        return {"statut": "erreur", "document_id": None, "motif": f"échec d'insertion DB : {exc}"}

    return {"statut": "structure", "document_id": document.id, "motif": None}


def _parse_iso_date(raw_value: Optional[str]) -> Optional[datetime.date]:
    if not raw_value:
        return None
    try:
        return datetime.date.fromisoformat(raw_value.strip())
    except ValueError:
        return None
