import asyncio
import datetime
import hashlib
import os
import re
import uuid
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.api.routers import documents as documents_router
from src.api.schemas import GlobalStatsOut, HealthOut
from src.db.database import SessionLocal, get_db, init_db
from src.db.models import ExtractionRun, LegalDocument, MediaFile, StructureNode, Article, ArticleVersion
from src.services.mineru_service import mineru_service
from src.services.minio_service import minio_service
from src.extractor.parser import LegalDocumentParser
from psycopg2.extras import DateRange
from sqlalchemy_utils import Ltree

event_queues = []
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STORAGE_TMP_DIR = os.path.join(PROJECT_ROOT, "storage", "tmp")
MEDIA_CATEGORY_BY_FORMAT = {
    "md": "EXTRACTION_MARKDOWN",
    "json": "EXTRACTION_JSON",
}

app = FastAPI(
    title="Mibeko Python API",
    description="Interface d'ingestion et d'extraction de documents juridiques",
    version="1.0.0",
    docs_url="/api/v1/docs",
    redoc_url="/api/v1/redoc",
    openapi_url="/api/v1/openapi.json"
)

# ---------------------------------------------------------------------------
# CORS — autorise le frontend React (dev :5173 et prod)
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routeurs
# ---------------------------------------------------------------------------
app.include_router(documents_router.router)

templates_dir = os.path.join(os.path.dirname(__file__), "templates")
static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")
templates = Jinja2Templates(directory=templates_dir)


def sanitize_path_component(value: str) -> str:
    """Nettoie une valeur pour produire un segment de chemin stable pour Domino."""

    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower())
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    return normalized or "document"


def compute_sha256(payload: bytes) -> str:
    """Calcule l'empreinte SHA-256 d'un contenu binaire."""

    return hashlib.sha256(payload).hexdigest()


def build_document_key(document_role: str, stock_code: Optional[str], title: str) -> str:
    """Construit une cle metier stable pour eviter les doublons documentaires."""

    if document_role == "STOCK" and stock_code:
        return f"stock:{sanitize_path_component(stock_code)}"

    return f"flux:{sanitize_path_component(title)}"


def build_domino_root(document_role: str, stock_code: Optional[str], document_id: uuid.UUID) -> str:
    """Construit la racine Domino/MinIO d'un document et de ses artefacts."""

    document_scope = sanitize_path_component(stock_code) if stock_code else str(document_id)
    role_scope = "stock" if document_role == "STOCK" else "flux"
    return f"domino/legal-documents/{role_scope}/{document_scope}"


def build_object_key(
    document_role: str,
    stock_code: Optional[str],
    document_id: uuid.UUID,
    area: str,
    filename: str,
    run_id: Optional[uuid.UUID] = None,
) -> str:
    """Construit une cle objet deterministe pour le stockage MinIO/Domino."""

    safe_name = sanitize_path_component(os.path.splitext(filename)[0])
    extension = os.path.splitext(filename)[1].lower()
    base = build_domino_root(document_role, stock_code, document_id)

    if run_id:
        return f"{base}/{area}/{run_id}/{safe_name}{extension}"

    return f"{base}/{area}/{safe_name}{extension}"


def build_media_record(
    document_id: uuid.UUID,
    object_key: str,
    file_path: str,
    original_filename: str,
    mime_type: str,
    file_category: str,
    payload_size: int,
    checksum_sha256: str,
    description: Optional[str] = None,
) -> MediaFile:
    """Construit un enregistrement media_files coherent avec le stockage MinIO."""

    return MediaFile(
        document_id=document_id,
        file_path=file_path,
        storage_provider="MINIO",
        bucket_name=minio_service.bucket_name,
        object_key=object_key,
        original_filename=original_filename,
        mime_type=mime_type,
        file_category=file_category,
        file_size=payload_size,
        checksum_sha256=checksum_sha256,
        description=description,
    )


async def notify_clients(event_name: str = "update", payload: str = "") -> None:
    """Diffuse un signal SSE a tous les clients connectes."""

    for queue in list(event_queues):
        await queue.put((event_name, payload))


def set_document_status(document: LegalDocument, has_markdown: bool, has_json: bool) -> None:
    """Calcule le statut d'extraction du document selon les artefacts disponibles."""

    if has_markdown and has_json:
        document.extraction_status = "completed"
    elif has_markdown or has_json:
        document.extraction_status = "partial"
    else:
        document.extraction_status = "pending"


def merge_metadata(document: LegalDocument, extra: dict) -> None:
    """Fusionne des metadonnees sans ecraser tout le bloc JSON existant."""

    current = document.metadata_ or {}
    document.metadata_ = {**current, **extra}


@app.on_event("startup")
def on_startup() -> None:
    """Initialise la couche base de donnees au demarrage de l'API."""

    init_db()

@app.on_event("shutdown")
async def on_shutdown() -> None:
    """Ferme proprement les connexions SSE pour éviter que le serveur ne reste bloqué lors de l'arrêt."""
    for queue in list(event_queues):
        await queue.put((None, None))


@app.get("/api/v1/health", response_model=HealthOut, tags=["health"])
def health_check(db: Session = Depends(get_db)):
    """Health check de l'API et de la connexion base de données."""
    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "error"

    return HealthOut(
        status="ok",
        service="mibeko-python",
        version="1.0.0",
        db=db_status,
        timestamp=datetime.datetime.utcnow(),
    )


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """Affiche le tableau de bord principal de depot documentaire."""

    return templates.TemplateResponse("index.html", {"request": request})


async def process_mineru_extraction(
    doc_id: uuid.UUID,
    pdf_media_id: uuid.UUID,
    pdf_path: str,
    document_role: str,
    stock_code: Optional[str],
) -> None:
    """Lance MinerU, stocke les artefacts Domino et trace le run d'extraction."""

    db = SessionLocal()
    run = ExtractionRun(
        id=uuid.uuid4(),
        document_id=doc_id,
        source="MINERU",
        status="running",
        started_at=datetime.datetime.utcnow(),
        source_media_file_id=pdf_media_id,
        meta={"processor": "MinerU"},
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    await notify_clients("update", "{}")

    try:
        task_id = await mineru_service.submit_pdf(pdf_path)
        result = await mineru_service.get_results(task_id)

        document = db.query(LegalDocument).filter(LegalDocument.id == doc_id).first()
        if document is None:
            raise ValueError("Document introuvable pour finaliser le run MinerU.")

        has_markdown = False
        has_json = False

        if result["status"] == "success":
            if result.get("md_url"):
                md_bytes = await mineru_service.download_result(result["md_url"])
                md_object_key = build_object_key(
                    document_role,
                    stock_code,
                    doc_id,
                    "extractions/markdown",
                    "source.md",
                    run.id,
                )
                md_path = minio_service.upload_bytes(md_object_key, md_bytes, "text/markdown")
                if not md_path:
                    raise ValueError("Echec de stockage MinIO pour le markdown.")

                media_md = build_media_record(
                    document_id=doc_id,
                    object_key=md_object_key,
                    file_path=md_path,
                    original_filename="source.md",
                    mime_type="text/markdown",
                    file_category="EXTRACTION_MARKDOWN",
                    payload_size=len(md_bytes),
                    checksum_sha256=compute_sha256(md_bytes),
                    description="Extraction MinerU au format Markdown",
                )
                db.add(media_md)
                db.flush()
                run.markdown_media_file_id = media_md.id
                has_markdown = True

            if result.get("json_url"):
                json_bytes = await mineru_service.download_result(result["json_url"])
                json_object_key = build_object_key(
                    document_role,
                    stock_code,
                    doc_id,
                    "extractions/json",
                    "source.json",
                    run.id,
                )
                json_path = minio_service.upload_bytes(json_object_key, json_bytes, "application/json")
                if not json_path:
                    raise ValueError("Echec de stockage MinIO pour le JSON.")

                media_json = build_media_record(
                    document_id=doc_id,
                    object_key=json_object_key,
                    file_path=json_path,
                    original_filename="source.json",
                    mime_type="application/json",
                    file_category="EXTRACTION_JSON",
                    payload_size=len(json_bytes),
                    checksum_sha256=compute_sha256(json_bytes),
                    description="Extraction MinerU au format JSON",
                )
                db.add(media_json)
                db.flush()
                run.json_media_file_id = media_json.id
                has_json = True

            run.status = "succeeded" if has_markdown and has_json else "partial"
            run.finished_at = datetime.datetime.utcnow()
            run.meta = {**(run.meta or {}), "mineru_task_id": task_id}
            set_document_status(document, has_markdown, has_json)
            merge_metadata(document, {"latest_extraction_run_id": str(run.id)})
        else:
            run.status = "failed"
            run.finished_at = datetime.datetime.utcnow()
            run.meta = {**(run.meta or {}), "mineru_task_id": task_id}
        set_document_status(document, has_markdown, has_json)
        merge_metadata(document, {"latest_extraction_run_id": str(run.id)})

        db.commit()

        import json
        await notify_clients("notification", json.dumps({"message": f"MinerU a terminé le traitement du document.", "type": "success"}))
    except Exception as exc:
        db.rollback()
        persisted_run = db.query(ExtractionRun).filter(ExtractionRun.id == run.id).first()
        persisted_document = db.query(LegalDocument).filter(LegalDocument.id == doc_id).first()

        if persisted_run:
            persisted_run.status = "failed"
            persisted_run.finished_at = datetime.datetime.utcnow()
            persisted_run.meta = {**(persisted_run.meta or {}), "error": str(exc)}

        if persisted_document:
            persisted_document.extraction_status = "failed"

        db.commit()

        import json
        await notify_clients("notification", json.dumps({"message": f"Échec de l'extraction MinerU: {str(exc)}", "type": "error"}))
    finally:
        if os.path.exists(pdf_path):
            os.remove(pdf_path)

        db.close()
        await notify_clients("update", "{}")


@app.post("/api/v1/documents/upload", tags=["documents"])
async def upload_document(
    background_tasks: BackgroundTasks,
    titre_officiel: str = Form(...),
    document_role: str = Form("STOCK"),
    stock_code: Optional[str] = Form(None),
    document_key: Optional[str] = Form(None),
    pdf_file: UploadFile = File(...),
    md_file: Optional[UploadFile] = File(None),
    json_file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
):
    """Depose un PDF et ses extractions optionnelles dans MinIO puis en base."""

    normalized_role = document_role.upper()
    normalized_stock_code = sanitize_path_component(stock_code) if stock_code else None

    if normalized_role not in {"STOCK", "FLUX"}:
        return JSONResponse(status_code=422, content={"message": "Le role doit etre STOCK ou FLUX."})

    if normalized_role == "STOCK" and not normalized_stock_code:
        return JSONResponse(status_code=422, content={"message": "Le champ code du stock est obligatoire pour un document de type STOCK."})

    resolved_document_key = document_key or build_document_key(normalized_role, normalized_stock_code, titre_officiel)
    existing_document = db.query(LegalDocument).filter(LegalDocument.document_key == resolved_document_key).first()
    if existing_document:
        return JSONResponse(
            status_code=409,
            content={"message": "Un document avec cette cle existe deja.", "document_id": str(existing_document.id)},
        )

    pdf_bytes = await pdf_file.read()
    if not pdf_bytes:
        return JSONResponse(status_code=422, content={"message": "Le fichier PDF est vide."})

    doc_id = uuid.uuid4()
    pdf_checksum = compute_sha256(pdf_bytes)
    pdf_object_key = build_object_key(
        normalized_role,
        normalized_stock_code,
        doc_id,
        "source/pdf",
        pdf_file.filename or "document.pdf",
    )
    pdf_s3_path = minio_service.upload_bytes(pdf_object_key, pdf_bytes, "application/pdf")
    if not pdf_s3_path:
        return JSONResponse(status_code=500, content={"message": "Echec de stockage du PDF dans MinIO."})

    candidate_type_code = "CODE" if normalized_role == "STOCK" else "LOI"
    valid_type_codes = {row[0] for row in db.execute(text("SELECT code FROM document_types")).fetchall()}
    type_code = candidate_type_code if candidate_type_code in valid_type_codes else None

    os.makedirs(STORAGE_TMP_DIR, exist_ok=True)
    temp_pdf_path = os.path.join(STORAGE_TMP_DIR, f"{uuid.uuid4()}_{pdf_file.filename or 'document.pdf'}")
    with open(temp_pdf_path, "wb") as buffer:
        buffer.write(pdf_bytes)

    new_doc = LegalDocument(
        id=doc_id,
        type_code=type_code,
        document_key=resolved_document_key,
        stock_code=normalized_stock_code,
        titre_officiel=titre_officiel,
        document_role=normalized_role,
        consolidation_as_of=datetime.datetime.utcnow().date() if normalized_role == "STOCK" else None,
        statut="vigueur",
        extraction_status="pending",
    )
    merge_metadata(new_doc, {"ingestion_mode": "web_upload"})

    pdf_media = build_media_record(
        document_id=doc_id,
        object_key=pdf_object_key,
        file_path=pdf_s3_path,
        original_filename=pdf_file.filename or "document.pdf",
        mime_type="application/pdf",
        file_category="SOURCE_PDF",
        payload_size=len(pdf_bytes),
        checksum_sha256=pdf_checksum,
        description="PDF source depose depuis l'interface web",
    )

    db.add(new_doc)
    db.add(pdf_media)
    db.flush()

    if md_file or json_file:
        provided_formats = []
        run = ExtractionRun(
            document_id=doc_id,
            source="MANUAL_UPLOAD",
            status="running",
            started_at=datetime.datetime.utcnow(),
            source_media_file_id=pdf_media.id,
            meta={"provided_formats": provided_formats},
        )
        db.add(run)
        db.flush()

        has_markdown = False
        has_json = False

        if md_file:
            md_bytes = await md_file.read()
            if md_bytes:
                md_object_key = build_object_key(
                    normalized_role,
                    normalized_stock_code,
                    doc_id,
                    "extractions/markdown",
                    md_file.filename or "source.md",
                    run.id,
                )
                md_s3_path = minio_service.upload_bytes(md_object_key, md_bytes, "text/markdown")
                if not md_s3_path:
                    db.rollback()
                    return JSONResponse(status_code=500, content={"message": "Echec de stockage du markdown dans MinIO."})

                media_md = build_media_record(
                    document_id=doc_id,
                    object_key=md_object_key,
                    file_path=md_s3_path,
                    original_filename=md_file.filename or "source.md",
                    mime_type="text/markdown",
                    file_category="EXTRACTION_MARKDOWN",
                    payload_size=len(md_bytes),
                    checksum_sha256=compute_sha256(md_bytes),
                    description="Markdown fourni manuellement a l'upload",
                )
                db.add(media_md)
                db.flush()
                run.markdown_media_file_id = media_md.id
                provided_formats.append("md")
                has_markdown = True

        if json_file:
            json_bytes = await json_file.read()
            if json_bytes:
                json_object_key = build_object_key(
                    normalized_role,
                    normalized_stock_code,
                    doc_id,
                    "extractions/json",
                    json_file.filename or "source.json",
                    run.id,
                )
                json_s3_path = minio_service.upload_bytes(json_object_key, json_bytes, "application/json")
                if not json_s3_path:
                    db.rollback()
                    return JSONResponse(status_code=500, content={"message": "Echec de stockage du JSON dans MinIO."})

                media_json = build_media_record(
                    document_id=doc_id,
                    object_key=json_object_key,
                    file_path=json_s3_path,
                    original_filename=json_file.filename or "source.json",
                    mime_type="application/json",
                    file_category="EXTRACTION_JSON",
                    payload_size=len(json_bytes),
                    checksum_sha256=compute_sha256(json_bytes),
                    description="JSON fourni manuellement a l'upload",
                )
                db.add(media_json)
                db.flush()
                run.json_media_file_id = media_json.id
                provided_formats.append("json")
                has_json = True

        run.status = "succeeded" if has_markdown and has_json else "partial"
        run.finished_at = datetime.datetime.utcnow()
        run.meta = {**(run.meta or {}), "provided_formats": provided_formats}
        set_document_status(new_doc, has_markdown, has_json)
        merge_metadata(new_doc, {"latest_extraction_run_id": str(run.id)})
        db.commit()

        if os.path.exists(temp_pdf_path):
            os.remove(temp_pdf_path)

        asyncio.create_task(notify_clients())
    else:
        db.commit()
        background_tasks.add_task(
            process_mineru_extraction,
            doc_id,
            pdf_media.id,
            temp_pdf_path,
            normalized_role,
            normalized_stock_code,
        )

    return JSONResponse(content={"message": "Document depose avec succes", "document_id": str(doc_id)})


@app.get("/api/v1/documents", tags=["documents"])
def list_documents(db: Session = Depends(get_db)):
    """Retourne la liste des documents avec disponibilite des artefacts et dernier run."""

    documents = db.query(LegalDocument).order_by(LegalDocument.created_at.desc()).limit(20).all()
    result = []

    for document in documents:
        has_md = any(file.file_category == "EXTRACTION_MARKDOWN" for file in document.files)
        has_json = any(file.file_category == "EXTRACTION_JSON" for file in document.files)
        latest_run = max(document.extraction_runs, key=lambda item: item.started_at or datetime.datetime.min, default=None)

        result.append(
            {
                "id": str(document.id),
                "titre_officiel": document.titre_officiel,
                "stock_code": document.stock_code,
                "document_role": document.document_role,
                "type_code": document.type_code,
                "extraction_status": document.extraction_status,
                "curation_status": document.curation_status,
                "has_md": has_md,
                "has_json": has_json,
                "latest_run_source": latest_run.source if latest_run else None,
                "latest_run_status": latest_run.status if latest_run else None,
                "created_at": document.created_at.isoformat() if document.created_at else None,
            }
        )

    return result


@app.get("/api/v1/stream", tags=["stream"])
async def stream_events():
    """Expose un flux SSE pour recharger le tableau en temps reel."""

    queue = asyncio.Queue()
    event_queues.append(queue)

    async def event_generator():
        try:
            while True:
                event_name, payload = await queue.get()
                if event_name is None:
                    break
                if event_name:
                    yield f"event: {event_name}\n"
                yield f"data: {payload}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            if queue in event_queues:
                event_queues.remove(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


async def execute_parsing_task(run_id: uuid.UUID, doc_id: uuid.UUID, media_id: uuid.UUID, format_type: str):
    """Tâche asynchrone effectuant le parsing effectif du MD/JSON."""
    db = SessionLocal()
    run = db.query(ExtractionRun).filter(ExtractionRun.id == run_id).first()
    document = db.query(LegalDocument).filter(LegalDocument.id == doc_id).first()
    media = db.query(MediaFile).filter(MediaFile.id == media_id).first()

    if not run or not document or not media:
        db.close()
        return

    try:
        run.status = "running"
        db.commit()

        import json
        await notify_clients("notification", json.dumps({"message": f"Démarrage du parsing structurel...", "type": "info"}))
        await notify_clients("update", "{}")

        # Téléchargement depuis MinIO
        file_bytes = minio_service.get_file_bytes(media.object_key)
        if not file_bytes:
            raise ValueError(f"Impossible de télécharger {media.object_key} depuis MinIO")

        text_content = ""
        if format_type == "md":
            text_content = file_bytes.decode('utf-8', errors='ignore')
        elif format_type == "json":
            import json
            data = json.loads(file_bytes.decode('utf-8', errors='ignore'))
            lines = []
            for page in data.get("pdf_info", []):
                for block in page.get("preproc_blocks", []):
                    for line in block.get("lines", []):
                        line_text = " ".join([span.get("content", "") for span in line.get("spans", [])])
                        lines.append(line_text)
            text_content = "\n".join(lines)

        # Parse le contenu textuel
        parser = LegalDocumentParser(text_content=text_content)
        hierarchy = parser.parse_hierarchy()

        # Nettoyer l'ancienne structure du document (cascade DELETE s'en charge via SQLAlchemy si bien configuré,
        # mais par précaution on peut faire un delete explicite ou compter sur le fait que c'est le 1er parsing)
        db.query(ArticleVersion).filter(ArticleVersion.article_id.in_(
            db.query(Article.id).filter(Article.document_id == document.id)
        )).delete(synchronize_session=False)
        db.query(Article).filter(Article.document_id == document.id).delete(synchronize_session=False)
        db.query(StructureNode).filter(StructureNode.document_id == document.id).delete(synchronize_session=False)
        db.flush()

        seen_article_numbers = {}

        def insert_nodes(nodes_list, parent_tree_path=None, parent_node_id=None, start_order=0):
            current_order = start_order
            for node_data in nodes_list:
                node_id = uuid.uuid4()
                node_ltree_id = str(node_id).replace("-", "_")
                current_tree_path = f"{parent_tree_path}.{node_ltree_id}" if parent_tree_path else node_ltree_id
                ltree_obj = Ltree(current_tree_path)

                if node_data["type"] == "ARTICLE":
                    num = str(node_data.get("number", "")).strip()
                    if not num:
                        num = "SANS_NUM_" + str(uuid.uuid4())[:8]

                    # Prévention du crash pour violation de contrainte unique "uq_articles_document_numero"
                    if num in seen_article_numbers:
                        seen_article_numbers[num] += 1
                        num = f"{num}_doublon_{seen_article_numbers[num]}"
                    else:
                        seen_article_numbers[num] = 0

                    article = Article(
                        id=node_id,
                        document_id=document.id,
                        parent_node_id=parent_node_id,
                        numero_article=num,
                        ordre_affichage=current_order,
                        validation_status="pending"
                    )
                    db.add(article)

                    version = ArticleVersion(
                        article_id=article.id,
                        contenu_texte=node_data.get("content", ""),
                        validity_period=DateRange(datetime.datetime.utcnow().date(), None),
                        source_run_id=run.id,
                        source_media_file_id=media.id,
                        validation_status="pending"
                    )
                    db.add(version)
                else:
                    node = StructureNode(
                        id=node_id,
                        document_id=document.id,
                        type_unite=node_data["type"],
                        numero=node_data.get("number", ""),
                        titre=node_data.get("title", ""),
                        tree_path=ltree_obj,
                        sort_order=current_order,
                        validation_status="pending"
                    )
                    db.add(node)
                    db.flush()

                    if node_data.get("children"):
                        insert_nodes(node_data["children"], parent_tree_path=current_tree_path, parent_node_id=node.id, start_order=0)

                current_order += 1

        if hierarchy:
            insert_nodes(hierarchy)

        run.status = "succeeded"
        run.finished_at = datetime.datetime.utcnow()
        document.curation_status = "parsed"
        db.commit()

        import json
        await notify_clients("notification", json.dumps({"message": f"Parsing terminé avec succès.", "type": "success"}))

    except Exception as exc:
        db.rollback()
        run.status = "failed"
        run.finished_at = datetime.datetime.utcnow()
        run.meta = {**(run.meta or {}), "error": str(exc)}
        db.commit()

        import json
        await notify_clients("notification", json.dumps({"message": f"Erreur lors du parsing: {str(exc)}", "type": "error"}))
    finally:
        db.close()
        await notify_clients("update", "{}")

@app.post("/api/v1/documents/{doc_id}/parse", tags=["documents"])
def parse_document(doc_id: str, background_tasks: BackgroundTasks, source_format: str = Form(...), db: Session = Depends(get_db)):
    """Enregistre une demande de parsing structurel a partir d'un artefact disponible."""

    document = db.query(LegalDocument).filter(LegalDocument.id == doc_id).first()
    if not document:
        return JSONResponse(status_code=404, content={"message": "Document non trouve"})

    normalized_format = source_format.lower()
    expected_category = MEDIA_CATEGORY_BY_FORMAT.get(normalized_format)
    if not expected_category:
        return JSONResponse(status_code=422, content={"message": "Le format doit etre md ou json."})

    media = next((file for file in document.files if file.file_category == expected_category), None)
    if not media:
        return JSONResponse(status_code=400, content={"message": f"Fichier .{normalized_format} introuvable pour ce document."})

    run = ExtractionRun(
        document_id=document.id,
        source="PARSING",
        status="queued",
        started_at=datetime.datetime.utcnow(),
        source_media_file_id=media.id,
        meta={
            "requested_format": normalized_format,
            "note": "Parsing structurel vers articles/article_versions.",
        },
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    # Lancement de la tâche en arrière-plan
    background_tasks.add_task(
        execute_parsing_task,
        run_id=run.id,
        doc_id=document.id,
        media_id=media.id,
        format_type=normalized_format
    )

    return {
        "message": f"Demande de parsing enregistree depuis le fichier .{normalized_format}. Le processus est en cours d'execution.",
        "run_id": str(run.id),
    }
