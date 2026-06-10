"""
Routeur documents — CRUD + stats sur les LegalDocuments du backend Python.
Expose les fonctionnalités d'ingestion qui étaient uniquement disponibles via l'interface Jinja2.
"""

import datetime
import uuid
from math import ceil
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from src.api.auth import AuthenticatedUser, require_editor
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.api.schemas import (
    ArticleOut,
    ArticleVersionOut,
    DocumentStatsOut,
    GlobalStatsOut,
    LegalDocumentDetail,
    LegalDocumentSummary,
    MediaFileOut,
    ExtractionRunOut,
    PaginatedArticles,
)
from src.db.database import get_db
from src.db.models import (
    Article,
    ArticleVersion,
    ExtractionRun,
    LegalDocument,
    MediaFile,
    StructureNode,
)

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


def _build_summary(document: LegalDocument, latest_run: Optional[ExtractionRun] = None, nb_articles: int = 0) -> dict:
    has_md = any(f.file_category == "EXTRACTION_MARKDOWN" for f in document.files)
    has_json = any(f.file_category == "EXTRACTION_JSON" for f in document.files)
    if latest_run is None and document.extraction_runs:
        latest_run = max(
            document.extraction_runs,
            key=lambda r: r.started_at or datetime.datetime.min,
            default=None,
        )
    return {
        "id": document.id,
        "titre_officiel": document.titre_officiel,
        "stock_code": document.stock_code,
        "document_role": document.document_role,
        "type_code": document.type_code,
        "legal_scope": document.legal_scope,
        "official_journal_id": document.official_journal_id,
        "extraction_status": document.extraction_status,
        "curation_status": document.curation_status,
        "has_md": has_md,
        "has_json": has_json,
        "latest_run_source": latest_run.source if latest_run else None,
        "latest_run_status": latest_run.status if latest_run else None,
        "nb_articles": nb_articles,
        "created_at": document.created_at,
    }


# ---------------------------------------------------------------------------
# GET /api/documents  — liste paginée
# ---------------------------------------------------------------------------

@router.get("", response_model=list[LegalDocumentSummary])
def list_documents(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: Optional[str] = Query(default=None, description="Filtrer par extraction_status"),
    role: Optional[str] = Query(default=None, description="Filtrer par document_role (STOCK ou FLUX)"),
    curation: Optional[str] = Query(
        default=None,
        description="Filtrer par curation_status exact, ou 'queue' pour la file de validation (tout sauf published)",
    ),
    q: Optional[str] = Query(default=None, description="Recherche sur le titre ou le code stock"),
    db: Session = Depends(get_db),
):
    """Retourne la liste des documents avec disponibilité des artefacts et dernier run."""
    query = (
        db.query(LegalDocument)
        .filter(LegalDocument.deleted_at.is_(None))
        .order_by(LegalDocument.created_at.desc())
    )
    if status:
        query = query.filter(LegalDocument.extraction_status == status)
    if role:
        query = query.filter(LegalDocument.document_role == role.upper())
    if curation == "queue":
        query = query.filter(LegalDocument.curation_status != "published")
    elif curation:
        query = query.filter(LegalDocument.curation_status == curation)
    if q:
        needle = f"%{q.strip()}%"
        query = query.filter(
            LegalDocument.titre_officiel.ilike(needle) | LegalDocument.stock_code.ilike(needle)
        )
    documents = query.offset(offset).limit(limit).all()

    article_counts: dict = {}
    document_ids = [doc.id for doc in documents]
    if document_ids:
        article_counts = dict(
            db.query(Article.document_id, func.count(Article.id))
            .filter(Article.document_id.in_(document_ids))
            .group_by(Article.document_id)
            .all()
        )

    return [_build_summary(doc, nb_articles=article_counts.get(doc.id, 0)) for doc in documents]


# ---------------------------------------------------------------------------
# GET /api/documents/stats  — KPIs globaux
# ---------------------------------------------------------------------------

@router.get("/stats", response_model=GlobalStatsOut)
def global_stats(db: Session = Depends(get_db)):
    """Retourne les KPIs globaux pour le dashboard frontend."""
    def count_docs(*criteria) -> int:
        return (
            db.query(func.count(LegalDocument.id))
            .filter(LegalDocument.deleted_at.is_(None), *criteria)
            .scalar()
            or 0
        )

    total_docs = count_docs()
    completed = count_docs(LegalDocument.extraction_status == "completed")
    processing = count_docs(LegalDocument.extraction_status == "processing")
    pending = count_docs(LegalDocument.extraction_status == "pending")
    failed = count_docs(LegalDocument.extraction_status == "failed")
    draft = count_docs(LegalDocument.curation_status == "draft")
    review = count_docs(LegalDocument.curation_status == "review")
    published = count_docs(LegalDocument.curation_status == "published")
    total_articles = db.query(func.count(Article.id)).scalar() or 0
    total_runs = db.query(func.count(ExtractionRun.id)).scalar() or 0
    runs_running = db.query(func.count(ExtractionRun.id)).filter(ExtractionRun.status == "running").scalar() or 0

    return GlobalStatsOut(
        total_documents=total_docs,
        documents_completed=completed,
        documents_processing=processing,
        documents_pending=pending,
        documents_failed=failed,
        documents_draft=draft,
        documents_review=review,
        documents_published=published,
        total_articles=total_articles,
        total_runs=total_runs,
        runs_running=runs_running,
    )


# ---------------------------------------------------------------------------
# GET /api/documents/{doc_id}  — détail complet
# ---------------------------------------------------------------------------

@router.get("/{doc_id}", response_model=LegalDocumentDetail)
def get_document(doc_id: str, db: Session = Depends(get_db)):
    """Retourne le détail complet d'un document avec ses fichiers et runs."""
    document = db.query(LegalDocument).filter(LegalDocument.id == doc_id, LegalDocument.deleted_at.is_(None)).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document non trouvé")

    has_md = any(f.file_category == "EXTRACTION_MARKDOWN" for f in document.files)
    has_json = any(f.file_category == "EXTRACTION_JSON" for f in document.files)
    nb_articles = db.query(func.count(Article.id)).filter(Article.document_id == document.id).scalar() or 0
    nb_nodes = db.query(func.count(StructureNode.id)).filter(StructureNode.document_id == document.id).scalar() or 0

    detail = LegalDocumentDetail(
        id=document.id,
        titre_officiel=document.titre_officiel,
        stock_code=document.stock_code,
        document_key=document.document_key,
        document_role=document.document_role,
        type_code=document.type_code,
        statut=document.statut,
        legal_scope=document.legal_scope,
        extraction_status=document.extraction_status,
        curation_status=document.curation_status,
        date_publication=document.date_publication,
        date_signature=document.date_signature,
        consolidation_as_of=document.consolidation_as_of,
        metadata_=document.metadata_,
        created_at=document.created_at,
        updated_at=document.updated_at,
        files=[MediaFileOut.model_validate(f) for f in document.files],
        extraction_runs=[ExtractionRunOut.model_validate(r) for r in document.extraction_runs],
        has_md=has_md,
        has_json=has_json,
        nb_articles=nb_articles,
        nb_nodes=nb_nodes,
    )
    return detail


# ---------------------------------------------------------------------------
# GET /api/documents/{doc_id}/runs  — liste des ExtractionRun
# ---------------------------------------------------------------------------

@router.get("/{doc_id}/runs", response_model=list[ExtractionRunOut])
def get_document_runs(doc_id: str, db: Session = Depends(get_db)):
    """Retourne tous les runs d'extraction pour un document, du plus récent au plus ancien."""
    document = db.query(LegalDocument).filter(LegalDocument.id == doc_id, LegalDocument.deleted_at.is_(None)).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document non trouvé")

    runs = (
        db.query(ExtractionRun)
        .filter(ExtractionRun.document_id == document.id)
        .order_by(ExtractionRun.started_at.desc())
        .all()
    )
    return [ExtractionRunOut.model_validate(r) for r in runs]


# ---------------------------------------------------------------------------
# GET /api/documents/{doc_id}/files  — liste des MediaFile
# ---------------------------------------------------------------------------

@router.get("/{doc_id}/files", response_model=list[MediaFileOut])
def get_document_files(doc_id: str, db: Session = Depends(get_db)):
    """Retourne tous les fichiers média associés à un document."""
    document = db.query(LegalDocument).filter(LegalDocument.id == doc_id, LegalDocument.deleted_at.is_(None)).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document non trouvé")

    files = db.query(MediaFile).filter(MediaFile.document_id == document.id).all()
    return [MediaFileOut.model_validate(f) for f in files]


# ---------------------------------------------------------------------------
# GET /api/documents/{doc_id}/stats  — stats du document
# ---------------------------------------------------------------------------

@router.get("/{doc_id}/stats", response_model=DocumentStatsOut)
def get_document_stats(doc_id: str, db: Session = Depends(get_db)):
    """Retourne les statistiques d'extraction d'un document."""
    document = db.query(LegalDocument).filter(LegalDocument.id == doc_id, LegalDocument.deleted_at.is_(None)).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document non trouvé")

    nb_articles = db.query(func.count(Article.id)).filter(Article.document_id == document.id).scalar() or 0
    nb_nodes = db.query(func.count(StructureNode.id)).filter(StructureNode.document_id == document.id).scalar() or 0
    nb_runs = db.query(func.count(ExtractionRun.id)).filter(ExtractionRun.document_id == document.id).scalar() or 0
    nb_files = db.query(func.count(MediaFile.id)).filter(MediaFile.document_id == document.id).scalar() or 0

    return DocumentStatsOut(
        document_id=doc_id,
        nb_articles=nb_articles,
        nb_nodes=nb_nodes,
        nb_extraction_runs=nb_runs,
        nb_media_files=nb_files,
        extraction_status=document.extraction_status,
        curation_status=document.curation_status,
    )


# ---------------------------------------------------------------------------
# GET /api/documents/{doc_id}/articles  — articles extraits paginés
# ---------------------------------------------------------------------------

@router.get("/{doc_id}/articles", response_model=PaginatedArticles)
def get_document_articles(
    doc_id: str,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Retourne les articles extraits d'un document, avec leurs versions."""
    document = db.query(LegalDocument).filter(LegalDocument.id == doc_id, LegalDocument.deleted_at.is_(None)).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document non trouvé")

    total = db.query(func.count(Article.id)).filter(Article.document_id == document.id).scalar() or 0
    articles = (
        db.query(Article)
        .filter(Article.document_id == document.id)
        .order_by(Article.ordre_affichage)
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    items = []
    for art in articles:
        versions = db.query(ArticleVersion).filter(ArticleVersion.article_id == art.id).all()
        items.append(
            ArticleOut(
                id=art.id,
                document_id=art.document_id,
                parent_node_id=art.parent_node_id,
                numero_article=art.numero_article,
                ordre_affichage=art.ordre_affichage,
                validation_status=art.validation_status,
                created_at=art.created_at,
                versions=[ArticleVersionOut.model_validate(v) for v in versions],
            )
        )

    return PaginatedArticles(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        pages=ceil(total / per_page) if total > 0 else 0,
    )


# ---------------------------------------------------------------------------
# DELETE /api/documents/{doc_id}  — suppression
# ---------------------------------------------------------------------------

@router.delete("/{doc_id}", status_code=204)
def delete_document(doc_id: str, db: Session = Depends(get_db), _user: AuthenticatedUser = Depends(require_editor)):
    """Supprime un document et toutes ses données associées (cascade)."""
    document = db.query(LegalDocument).filter(LegalDocument.id == doc_id, LegalDocument.deleted_at.is_(None)).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document non trouvé")

    # Suppression en cascade manuelle pour les tables sans FK CASCADE
    article_ids = [a.id for a in db.query(Article).filter(Article.document_id == document.id).all()]
    if article_ids:
        db.query(ArticleVersion).filter(ArticleVersion.article_id.in_(article_ids)).delete(synchronize_session=False)
    db.query(Article).filter(Article.document_id == document.id).delete(synchronize_session=False)
    db.query(StructureNode).filter(StructureNode.document_id == document.id).delete(synchronize_session=False)
    db.query(ExtractionRun).filter(ExtractionRun.document_id == document.id).delete(synchronize_session=False)
    db.query(MediaFile).filter(MediaFile.document_id == document.id).delete(synchronize_session=False)
    db.delete(document)
    db.commit()
