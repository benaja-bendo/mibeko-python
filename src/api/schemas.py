"""
Pydantic schemas pour l'API FastAPI de Mibeko Python.
Ces schémas définissent les contrats JSON entre le backend Python et le frontend React/TS.
"""

from __future__ import annotations

import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class OrmBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# MediaFile
# ---------------------------------------------------------------------------

class MediaFileOut(OrmBase):
    id: UUID
    document_id: UUID
    file_path: str
    storage_provider: str
    bucket_name: Optional[str] = None
    object_key: Optional[str] = None
    original_filename: Optional[str] = None
    mime_type: Optional[str] = None
    file_category: Optional[str] = None
    file_size: Optional[int] = None
    checksum_sha256: Optional[str] = None
    description: Optional[str] = None
    created_at: Optional[datetime.datetime] = None


# ---------------------------------------------------------------------------
# ExtractionRun
# ---------------------------------------------------------------------------

class ExtractionRunOut(OrmBase):
    id: UUID
    document_id: UUID
    source: Optional[str] = None
    status: str
    started_at: Optional[datetime.datetime] = None
    finished_at: Optional[datetime.datetime] = None
    source_media_file_id: Optional[UUID] = None
    markdown_media_file_id: Optional[UUID] = None
    json_media_file_id: Optional[UUID] = None
    meta: Optional[dict] = None


# ---------------------------------------------------------------------------
# LegalDocument
# ---------------------------------------------------------------------------

class LegalDocumentSummary(OrmBase):
    """Résumé léger d'un document pour les listes."""
    id: UUID
    titre_officiel: str
    # Objet derive du corps de l'acte — jamais un titre officiel. Sur un acte
    # en abrege, c'est la seule information lisible de la ligne de liste.
    libelle_descriptif: Optional[str] = None
    stock_code: Optional[str] = None
    document_role: Optional[str] = None
    type_code: Optional[str] = None
    legal_scope: Optional[str] = None
    official_journal_id: Optional[UUID] = None
    extraction_status: Optional[str] = None
    curation_status: Optional[str] = None
    has_md: bool = False
    has_json: bool = False
    latest_run_source: Optional[str] = None
    latest_run_status: Optional[str] = None
    nb_articles: int = 0
    created_at: Optional[datetime.datetime] = None


class LegalDocumentDetail(OrmBase):
    """Détail complet d'un document avec ses fichiers et runs."""
    id: UUID
    titre_officiel: str
    # Cf. LegalDocumentSummary : derive du corps, affiche a cote du titre
    # officiel et jamais a sa place. `libelle_descriptif_source` dit si un
    # humain l'a ecrit (`manuel`) ou s'il vient du premier article (`article`).
    libelle_descriptif: Optional[str] = None
    libelle_descriptif_source: Optional[str] = None
    stock_code: Optional[str] = None
    document_key: Optional[str] = None
    document_role: Optional[str] = None
    type_code: Optional[str] = None
    statut: Optional[str] = None
    legal_scope: Optional[str] = None
    extraction_status: Optional[str] = None
    curation_status: Optional[str] = None
    date_publication: Optional[datetime.date] = None
    date_signature: Optional[datetime.date] = None
    consolidation_as_of: Optional[datetime.date] = None
    metadata_: Optional[dict] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None
    files: list[MediaFileOut] = []
    extraction_runs: list[ExtractionRunOut] = []

    # Champs calculés
    has_md: bool = False
    has_json: bool = False
    nb_articles: int = 0
    nb_nodes: int = 0


# ---------------------------------------------------------------------------
# ArticleVersion (pour la lecture des articles extraits)
# ---------------------------------------------------------------------------

class ArticleVersionOut(OrmBase):
    id: UUID
    article_id: UUID
    contenu_texte: Optional[str] = None
    validation_status: Optional[str] = None
    is_verified: bool = False
    created_at: Optional[datetime.datetime] = None


class ArticleOut(OrmBase):
    id: UUID
    document_id: UUID
    parent_node_id: Optional[UUID] = None
    numero_article: Optional[str] = None
    ordre_affichage: int = 0
    validation_status: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    versions: list[ArticleVersionOut] = []


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

class DocumentStatsOut(BaseModel):
    document_id: str
    nb_articles: int
    nb_nodes: int
    nb_extraction_runs: int
    nb_media_files: int
    extraction_status: Optional[str] = None
    curation_status: Optional[str] = None


class GlobalStatsOut(BaseModel):
    """KPIs globaux pour le dashboard frontend."""
    total_documents: int
    documents_completed: int
    documents_processing: int
    documents_pending: int
    documents_failed: int
    documents_draft: int = 0
    documents_review: int = 0
    documents_published: int = 0
    total_articles: int
    total_runs: int
    runs_running: int


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class HealthOut(BaseModel):
    status: str = "ok"
    service: str = "mibeko-python"
    version: str = "1.0.0"
    db: str = "ok"
    timestamp: datetime.datetime
    # Cohérence du schéma DB (piloté par Laravel) vs les modèles SQLAlchemy.
    # None = check non encore effectué ; True = aucun écart ; False = écart(s).
    schema_ok: Optional[bool] = None
    # Liste courte et lisible des écarts détectés (colonnes manquantes /
    # divergence de nullabilité), tronquée pour rester exploitable.
    schema_issues: list[str] = []


# ---------------------------------------------------------------------------
# Upload response
# ---------------------------------------------------------------------------

class UploadResponse(BaseModel):
    message: str
    document_id: str


class ParseResponse(BaseModel):
    message: str
    run_id: str


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

class PaginatedArticles(BaseModel):
    items: list[ArticleOut]
    total: int
    page: int
    per_page: int
    pages: int
