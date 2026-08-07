import asyncio
import datetime
import hashlib
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from src.api.routers import documents as documents_router
from src.api.auth import AuthenticatedUser, require_editor
from src.api.config import EXPOSE_API_DOCS, INGESTION_CONSOLE_ENABLED, IS_PRODUCTION, SERVICE_VERSION
from src.api.schemas import GlobalStatsOut, HealthOut
from src.api.upload_utils import BodySizeLimitMiddleware, read_upload_capped, sanitize_filename, stream_upload_to_tmp
from src.db.database import SessionLocal, get_db, init_db
from src.db.schema_check import check_schema
from src.db.models import Article, ArticleVersion, CurationFlag, ExtractionRun, Institution, LegalDocument, MediaFile, OfficialJournal, StructureNode
from src.services.mineru_service import mineru_service
from src.services.minio_service import minio_service
from src.extractor.parser import LegalDocumentParser
from src.extractor.chunk_merger import merge_json_chunks, merge_markdown_chunks
from psycopg2.extras import DateRange
from sqlalchemy_utils import Ltree

logger = logging.getLogger("mibeko.api")

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
    version=SERVICE_VERSION,
    # Documentation OpenAPI désactivée en production sauf EXPOSE_API_DOCS=true :
    # ce service interne décrit des opérations d'écriture qui n'ont pas à être publiques.
    docs_url="/api/v1/docs" if EXPOSE_API_DOCS else None,
    redoc_url="/api/v1/redoc" if EXPOSE_API_DOCS else None,
    openapi_url="/api/v1/openapi.json" if EXPOSE_API_DOCS else None,
)

# ---------------------------------------------------------------------------
# CORS — autorise le frontend React (dev :5173 et prod)
# Liste partagée avec le gestionnaire d'exception global : ce dernier doit
# ré-attacher l'en-tête CORS à la main (une exception non gérée court-circuite
# le middleware CORS, cf. unhandled_exception_handler plus bas).
# ---------------------------------------------------------------------------
ALLOWED_ORIGINS = [
    "https://mibeko.fr",
    "https://www.mibeko.fr",
    "https://app.mibeko.fr",
    "https://www.app.mibeko.fr",
]

# Les origines de développement (Vite, etc.) ne sont servies qu'en dehors de
# la production : inutile d'autoriser localhost sur python.mibeko.fr (S10).
if not IS_PRODUCTION:
    ALLOWED_ORIGINS += [
        "http://localhost:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Gestionnaire d'exception global — une exception non gérée est traitée par le
# middleware le plus externe (ServerErrorMiddleware), qui COURT-CIRCUITE le
# middleware CORS : la réponse 500 partait donc sans en-tête Access-Control-
# Allow-Origin, et le navigateur l'affichait comme une « erreur CORS » trompeuse
# masquant le vrai 500. On logge ici la traceback (diagnostic) et on ré-attache
# l'en-tête CORS. La réponse reste générique : les détails internes (str(exc),
# traceback) ne sortent jamais vers le client (audit P0.8/S3), ils vont
# uniquement dans les logs serveur.
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error("ERREUR 500 non gérée sur %s %s", request.method, request.url.path, exc_info=exc)
    response = JSONResponse(
        status_code=500,
        content={"message": "Erreur interne du serveur."},
    )
    origin = request.headers.get("origin")
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Vary"] = "Origin"
    return response


# ---------------------------------------------------------------------------
# En-têtes de sécurité — ce sous-domaine est une brique d'infrastructure,
# jamais un site : aucun moteur ne doit l'indexer et on applique les en-têtes
# de durcissement standards.
# ---------------------------------------------------------------------------
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    if forwarded_proto == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# ---------------------------------------------------------------------------
# Plafond global du corps de requête (FIX-1) — ajouté EN DERNIER pour s'exécuter
# EN PREMIER (Starlette empile les middlewares en ordre inverse d'ajout) : le
# plafond de taille doit intercepter avant tout, y compris avant que le multipart
# ne soit spoolé sur disque et avant l'authentification. Défense DoS disque : un
# anonyme ne peut plus remplir le disque avec un corps géant (Content-Length
# honnête → 413 immédiat ; chunked/menteur → coupé net au comptage des octets).
# ---------------------------------------------------------------------------
app.add_middleware(BodySizeLimitMiddleware)


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


def parse_optional_date(raw_value: Optional[str]) -> Optional[datetime.date]:
    """Convertit une chaine ISO simple en date Python, sinon retourne None.

    Une valeur mal formée renvoie un 422 explicite au lieu de laisser la
    ValueError remonter en 500 (audit S13).
    """

    if not raw_value:
        return None

    try:
        return datetime.date.fromisoformat(raw_value.strip())
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Date invalide « {raw_value} » : format attendu YYYY-MM-DD.",
        )


def resolve_document_type_code(
    db: Session,
    requested_type_code: Optional[str],
    document_role: str,
    title: Optional[str] = None,
) -> Optional[str]:
    """Valide un code type fourni, sinon l'auto-détecte depuis le titre, sinon fallback.

    Priorité : type explicite valide → détection par le titre (convention collective
    → CONV, acte uniforme → AU, etc.) → défaut (CODE pour un STOCK, LOI pour un FLUX).
    """

    valid_type_codes = {row[0] for row in db.execute(text("SELECT code FROM document_types")).fetchall()}

    if requested_type_code:
        normalized_type = requested_type_code.strip().upper()
        if normalized_type in valid_type_codes:
            return normalized_type

    # Auto-détection limitée aux types nouveaux et non ambigus (CONV, AU) pour ne
    # pas requalifier à tort un code dont le titre contient « Loi ».
    if title:
        detected = map_detected_type_to_type_code(detect_texte_type(title), db)
        if detected in {"CONV", "AU"}:
            return detected

    candidate_type_code = "CODE" if document_role == "STOCK" else "LOI"
    return candidate_type_code if candidate_type_code in valid_type_codes else None


def resolve_institution_id(db: Session, institution_sigle: Optional[str]) -> Optional[uuid.UUID]:
    """Résout une institution par son sigle pour enrichir les métadonnées documentaires."""

    if not institution_sigle:
        return None

    institution = db.query(Institution).filter(Institution.sigle == institution_sigle.strip().upper()).first()
    return institution.id if institution else None


def build_journal_object_key(journal: OfficialJournal, filename: str) -> str:
    """Construit une clé objet stable pour le PDF source d’un Journal Officiel."""

    publication_scope = journal.publication_date.isoformat()
    # Borne le nom : les titres d'actes très longs gonflaient le chemin S3
    # au-delà des limites de colonne (file_path). 120 caractères restent lisibles
    # et laissent une marge confortable même avec le préfixe bucket le plus long.
    safe_name = sanitize_path_component(os.path.splitext(filename)[0])[:120]
    extension = os.path.splitext(filename)[1].lower() or ".pdf"
    number_scope = sanitize_path_component(journal.number or str(journal.id))
    return f"domino/official-journals/{publication_scope}/{number_scope}/source/{safe_name}{extension}"


def _mineru_block_text(block: Dict[str, Any]) -> str:
    """Texte concaténé des lignes/spans d'un bloc MinerU (hors tables)."""

    out: List[str] = []
    for line in block.get("lines", []) or []:
        line_text = " ".join(
            span.get("content", "").strip()
            for span in line.get("spans", []) or []
            if span.get("content")
        ).strip()
        if line_text:
            out.append(line_text)
    return " ".join(out).strip()


def _mineru_table_html(block: Dict[str, Any]) -> str:
    """Récupère le HTML d'un bloc table MinerU (span['html']), sur une seule ligne.

    On aplatit les espaces (HTML insensible aux espaces entre balises) pour que le
    tableau reste sur UNE ligne : le parseur le route alors vers un nœud TABLEAU.
    """

    candidates = list(block.get("blocks", []) or []) + [block]
    for sub in candidates:
        for line in sub.get("lines", []) or []:
            for span in line.get("spans", []) or []:
                if span.get("html"):
                    return " ".join(span["html"].split())
    return ""


def extract_text_from_mineru_json(data: Dict[str, Any], with_page_markers: bool = False) -> str:
    """Reconstruit un markdown lisible depuis le JSON MinerU.

    Points clés :
    - n'exploite qu'UNE source de blocs par page (`para_blocks` de préférence,
      sinon `preproc_blocks`) : MinerU duplique souvent les deux, et les additionner
      doublait tout le texte ingéré ;
    - marque les titres avec « # » pour fiabiliser la détection de structure
      (le parseur retire ce préfixe via `_clean_for_matching`) ;
    - préserve les tableaux en HTML (`span['html']`) au lieu de les perdre ;
    - les en-têtes/pieds/numéros de page sont déjà écartés par MinerU
      (`discarded_blocks`), donc ignorés ici.

    Si `with_page_markers` est vrai, une ligne « [[MIBEKO_PAGE:N]] » (N = page
    1-based) est insérée à chaque page : le parseur s'en sert pour tamponner les
    nœuds avec leur page d'origine (citabilité), puis l'ignore comme contenu.
    """

    lines: List[str] = []

    for page in data.get("pdf_info", []):
        if not isinstance(page, dict):
            continue

        if with_page_markers and isinstance(page.get("page_idx"), int):
            lines.append(f"[[MIBEKO_PAGE:{page['page_idx'] + 1}]]")

        # para_blocks est la sortie finale ; preproc_blocks le pré-traitement.
        # Ils sont généralement identiques → on n'en prend qu'un seul.
        blocks = page.get("para_blocks") or page.get("preproc_blocks") or []

        for block in blocks:
            block_type = block.get("type")

            if block_type == "table":
                html = _mineru_table_html(block)
                if html:
                    lines.append(html)
                continue

            if block_type == "title":
                # Un titre peut tenir sur plusieurs lignes visuelles → on les
                # joint et on préfixe « # » pour la détection de structure.
                text = _mineru_block_text(block)
                if text:
                    lines.append(f"# {text}")
                continue

            # Corps (text / list / …) : on préserve les lignes visuelles pour
            # garder alinéas et puces que le parseur conserve dans le contenu.
            for line in block.get("lines", []) or []:
                line_text = " ".join(
                    span.get("content", "").strip()
                    for span in line.get("spans", []) or []
                    if span.get("content")
                ).strip()
                if line_text:
                    lines.append(line_text)

    return "\n".join(lines)


def _normalize_for_match(text: str) -> str:
    """Normalise une ligne pour l'alignement md↔json : minuscules, espaces compactés."""

    cleaned = re.sub(r"^[#>\s*_]+", "", text)
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def build_json_page_index(json_payload: Dict[str, Any]) -> List[Tuple[str, int]]:
    """Liste ordonnée (ligne normalisée, page 1-based) depuis le JSON MinerU.

    Sert à retrouver la page PDF d'origine d'une ligne de texte. On ignore les
    lignes trop courtes (bruit) pour fiabiliser l'alignement.
    """

    index: List[Tuple[str, int]] = []
    for page in json_payload.get("pdf_info", []) or []:
        if not isinstance(page, dict):
            continue
        page_no = page.get("page_idx")
        if not isinstance(page_no, int):
            continue
        blocks = page.get("para_blocks") or page.get("preproc_blocks") or []
        for block in blocks:
            for line in block.get("lines", []) or []:
                text = " ".join(
                    span.get("content", "").strip()
                    for span in line.get("spans", []) or []
                    if span.get("content")
                ).strip()
                norm = _normalize_for_match(text)
                if len(norm) >= 8:
                    index.append((norm, page_no + 1))
    return index


def annotate_markdown_with_pages(
    content: str,
    page_index: List[Tuple[str, int]],
    cursor: List[int],
    window: int = 250,
) -> str:
    """Injecte des marqueurs « [[MIBEKO_PAGE:N]] » dans `content` (texte d'un acte).

    Aligne chaque ligne sur `page_index` (issu du JSON) via un curseur partagé qui
    avance (les deux sources sont dans le même ordre documentaire) : ainsi les
    lignes répétées d'un acte à l'autre (« Vu la Constitution ») sont résolues à la
    BONNE page. Le parseur consomme ensuite ces marqueurs pour tamponner chaque
    nœud (préambule, articles, signature) avec sa page PDF d'origine. Tolérant aux
    différences de segmentation md/json (match par préfixe de 18 caractères).
    """

    out: List[str] = []
    last_emitted: Optional[int] = None
    for raw in content.split("\n"):
        norm = _normalize_for_match(raw)
        page: Optional[int] = None
        if len(norm) >= 8:
            key = norm[:18]
            start = cursor[0]
            end = min(len(page_index), start + window)
            j = start
            while j < end:
                cand, cand_page = page_index[j]
                if cand.startswith(key) or norm.startswith(cand[:18]):
                    page = cand_page
                    cursor[0] = j + 1
                    break
                j += 1
        if page is not None and page != last_emitted:
            out.append(f"[[MIBEKO_PAGE:{page}]]")
            last_emitted = page
        out.append(raw)
    return "\n".join(out)


# Heuristique de qualité/lisibilité : extraite dans src/extractor/text_quality.py
# (module pur, sans FastAPI/SQLAlchemy) pour être réutilisable par le triage de
# l'étage 2 de l'usine à textes sans importer toute l'API.
from src.extractor.text_quality import (  # noqa: E402
    OCR_ARTIFACT_REPLACEMENTS,
    OCR_QUALITY_WARN_THRESHOLD,
    compute_ocr_quality,
    sanitize_legal_text,
)


def flag_low_ocr_quality(
    db: Session,
    document_id: uuid.UUID,
    quality: Dict[str, Any],
    threshold: float = OCR_QUALITY_WARN_THRESHOLD,
    run_id: Optional[uuid.UUID] = None,
) -> bool:
    """Émet un flag de curation NON bloquant si la qualité OCR passe sous le seuil.

    Réutilise le mécanisme ``curation_flags`` existant (comme
    flag_article_sequence_anomalies) mais en sévérité ``warning`` : l'éditeur voit
    l'avertissement et garde le bouton « Publier quand même » (seul ``blocking``
    gèle la publication côté Laravel). Idempotent : purge son propre flag non
    résolu avant de recalculer (source='heuristic', type='ocr_quality_faible').

    Renvoie True si un flag a été émis (qualité sous le seuil), False sinon.
    """
    db.query(CurationFlag).filter(
        CurationFlag.document_id == document_id,
        CurationFlag.source == "heuristic",
        CurationFlag.type_probleme == "ocr_quality_faible",
        CurationFlag.resolved.is_(False),
    ).delete(synchronize_session=False)

    score = quality.get("score", 1.0)
    if score >= threshold:
        return False

    pct = round(score * 100)
    signals = quality.get("signals", {})
    detail_bits = []
    if signals.get("replacement_char_count"):
        detail_bits.append(f"{signals['replacement_char_count']} caractère(s) illisible(s) (U+FFFD)")
    if signals.get("ocr_artifact_count"):
        detail_bits.append(f"{signals['ocr_artifact_count']} artefact(s) OCR connu(s)")
    if signals.get("single_letter_word_count"):
        detail_bits.append(f"{signals['single_letter_word_count']} mot(s) fragmenté(s)")
    if signals.get("control_char_count"):
        detail_bits.append(f"{signals['control_char_count']} caractère(s) de contrôle")
    detail = " ; ".join(detail_bits) if detail_bits else "signaux de dégradation détectés"

    flag = CurationFlag(
        document_id=document_id,
        source="heuristic",
        type_probleme="ocr_quality_faible",
        severity="warning",  # informe l'éditeur SANS bloquer la publication
        description=(
            f"Qualité OCR estimée faible ({pct} %, seuil {round(threshold * 100)} %) : {detail}. "
            "Indicateur de LISIBILITÉ (non contractuel) — vérifier le texte contre le PDF source "
            "avant publication. Ne mesure pas la justesse juridique."
        ),
        resolved=False,
    )
    # anchor/confidence/run_id ne sont pas mappés par le modèle Python (colonnes
    # Laravel) ; on renseigne ce que l'ORM connaît. La confiance chiffrée vit dans
    # extraction_runs.meta.ocr_quality (source d'audit).
    db.add(flag)
    return True


VALID_LEGAL_SCOPES = {"national", "ohada", "communautaire"}


def resolve_legal_scope(title: str, requested_scope: Optional[str] = None) -> str:
    """Résout le périmètre juridique : valeur explicite sinon détection depuis le titre."""

    if requested_scope and requested_scope.strip().lower() in VALID_LEGAL_SCOPES:
        return requested_scope.strip().lower()

    normalized = title.upper()
    if "OHADA" in normalized or "ACTE UNIFORME" in normalized:
        return "ohada"
    if "CEMAC" in normalized or "UNION AFRICAINE" in normalized or "COMMUNAUTAIRE" in normalized:
        return "communautaire"
    return "national"


def detect_texte_type(title: str) -> str:
    """Déduit le type métier d’un acte juridique à partir de son titre."""

    normalized = title.strip()
    # Types prioritaires repérables n'importe où dans le titre. Le séparateur
    # tolère l'espace ou le tiret (titres issus de noms de fichiers).
    if re.search(r"\bconventions?[\s\-]+collectives?\b", normalized, flags=re.IGNORECASE):
        return "CONVENTION_COLLECTIVE"
    if re.search(r"\bacte[\s\-]+uniforme\b", normalized, flags=re.IGNORECASE):
        return "ACTE_UNIFORME"
    if re.match(r"^(?:Loi constitutionnelle|LOI CONSTITUTIONNELLE)\b", normalized, flags=re.IGNORECASE):
        return "LOI_CONSTITUTIONNELLE"
    if re.match(r"^(?:Loi|LOI)\b", normalized, flags=re.IGNORECASE):
        return "LOI"
    if re.match(r"^(?:Décret|Decret|DECRET|DÉCRET)\b", normalized, flags=re.IGNORECASE):
        return "DECRET"
    if re.match(r"^(?:Arrêté|Arrete|Arrété|ARRETE|ARRÊTÉ)\b", normalized, flags=re.IGNORECASE):
        return "ARRETE"
    if re.match(r"^(?:Ordonnance|ORDONNANCE)\b", normalized, flags=re.IGNORECASE):
        return "ORDONNANCE"
    if re.match(r"^(?:Décision|Decision|DECISION|DÉCISION)\b", normalized, flags=re.IGNORECASE):
        return "DECISION"
    if re.match(r"^(?:Circulaire|CIRCULAIRE)\b", normalized, flags=re.IGNORECASE):
        return "CIRCULAIRE"
    if re.match(r"^(?:Constitution|CONSTITUTION)\b", normalized, flags=re.IGNORECASE):
        return "CONSTITUTION"
    if re.match(r"^(?:Proclamation|PROCLAIMATION|PROCLAMATION)\b", normalized, flags=re.IGNORECASE):
        return "PROCLAMATION"
    if re.match(r"^(?:Discours|DISCOURS)\b", normalized, flags=re.IGNORECASE):
        return "DISCOURS"
    if re.match(r"^(?:Allocution|Allocation|ALLOCUTION|ALLOCATION)\b", normalized, flags=re.IGNORECASE):
        return "ALLOCUTION"
    if re.match(r"^(?:Délibération|Deliberation|DELIBERATION|DÉLIBÉRATION)\b", normalized, flags=re.IGNORECASE):
        return "DELIBERATION"
    if re.match(r"^(?:Communiqué|Communique|COMMUNIQUE)\b", normalized, flags=re.IGNORECASE):
        return "COMMUNIQUE"
    if re.match(r"^(?:Rapport|RAPPORT)\b", normalized, flags=re.IGNORECASE):
        return "RAPPORT"
    if re.match(r"^(?:Note|NOTE)\b", normalized, flags=re.IGNORECASE):
        return "NOTE"
    if re.match(r"^(?:Avis|AVIS)\b", normalized, flags=re.IGNORECASE):
        return "AVIS"

    uppercase_title = normalized.upper()
    if "DECRET" in uppercase_title or "DÉCRET" in uppercase_title:
        return "DECRET"
    if "ARRETE" in uppercase_title or "ARRÊTÉ" in uppercase_title:
        return "ARRETE"
    if "ORDONNANCE" in uppercase_title:
        return "ORDONNANCE"
    if "LOI" in uppercase_title:
        return "LOI"
    if "CONSTITUTION" in uppercase_title:
        return "CONSTITUTION"
    if "PROCLAMATION" in uppercase_title or "PROCLAIMATION" in uppercase_title:
        return "PROCLAMATION"
    if "DISCOURS" in uppercase_title:
        return "DISCOURS"
    if "ALLOCUTION" in uppercase_title or "ALLOCATION" in uppercase_title:
        return "ALLOCUTION"
    if "DELIBERATION" in uppercase_title or "DÉLIBÉRATION" in uppercase_title:
        return "DELIBERATION"

    return "TEXTE"


def map_detected_type_to_type_code(detected_type: str, db: Session) -> Optional[str]:
    """Mappe un type détecté vers un `document_types.code` réellement existant."""

    valid_type_codes = {row[0] for row in db.execute(text("SELECT code FROM document_types")).fetchall()}
    mapping = {
        "CONVENTION_COLLECTIVE": "CONV",
        "ACTE_UNIFORME": "AU",
        "LOI_CONSTITUTIONNELLE": "LOI",
        "LOI": "LOI",
        "DECRET": "DEC",
        "ARRETE": "ARR",
        "ORDONNANCE": "ORD",
        "CONSTITUTION": "CONST",
        "DECISION": "TEXTE",
        "CIRCULAIRE": "TEXTE",
        "PROCLAMATION": "TEXTE",
        "DISCOURS": "TEXTE",
        "ALLOCUTION": "TEXTE",
        "DELIBERATION": "TEXTE",
        "COMMUNIQUE": "TEXTE",
        "RAPPORT": "TEXTE",
        "NOTE": "TEXTE",
        "AVIS": "TEXTE",
        "TEXTE": "TEXTE",
    }
    resolved = mapping.get(detected_type, "TEXTE")
    return resolved if resolved in valid_type_codes else None


def extract_reference_from_title(title: str) -> Optional[str]:
    """Extrait une référence stable depuis un titre d’acte lorsqu’elle est identifiable."""

    match = re.search(
        r"\b(?:LOI|DECRET|D[ÉE]CRET|ARRETE|ARR[ÊE]T[ÉE]|ORDONNANCE|DECISION|D[ÉE]CISION)\s*(?:N[°ºOo]\s*)?([A-Z0-9./-]+)",
        title,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    return match.group(1).strip().upper()


def extract_french_date(text_value: str) -> Optional[datetime.date]:
    """Extrait une date française simple de type `28 mai 2025` depuis un titre."""

    month_map = {
        "janvier": 1,
        "fevrier": 2,
        "février": 2,
        "mars": 3,
        "avril": 4,
        "mai": 5,
        "juin": 6,
        "juillet": 7,
        "aout": 8,
        "août": 8,
        "septembre": 9,
        "octobre": 10,
        "novembre": 11,
        "decembre": 12,
        "décembre": 12,
    }
    match = re.search(
        r"\b(\d{1,2})\s+(janvier|fevrier|février|mars|avril|mai|juin|juillet|aout|août|septembre|octobre|novembre|decembre|décembre)\s+(\d{4})\b",
        text_value,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    day = int(match.group(1))
    month = month_map[match.group(2).lower()]
    year = int(match.group(3))
    return datetime.date(year, month, day)


def _act_dedup_key(title: str) -> Optional[str]:
    """Clé de dédoublonnage d'un acte : son numéro « n° X » s'il existe, sinon None.

    Couvre tous les types (Loi n°, Décret n°, Arrêté n°, Avis n°, Délibération n°…).
    Les actes sans numéro (Discours, Proclamation, Allocution…) renvoient None et
    ne sont jamais dédupliqués.
    """
    match = re.search(r"\bN[°ºo]\s*([0-9][0-9A-Za-z./\-]*)", title, flags=re.IGNORECASE)
    return match.group(1).upper().rstrip(".-/") if match else None


def _dedupe_official_journal_acts(texts: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Fusionne les doublons sommaire/corps d'un JO.

    Un Journal Officiel liste ses actes dans un sommaire PUIS les reproduit en
    intégralité dans le corps : chaque acte numéroté apparaît deux fois (entrée
    courte du sommaire + texte complet). Pour un même (type, n°) on ne conserve
    que l'occurrence au contenu le plus riche (le corps). Les actes sans numéro
    sont préservés tels quels (ils peuvent être nombreux et distincts : avis,
    discours…). Robuste md/json : converge les deux rendus vers le corps.
    """
    best_index: Dict[Tuple[str, str], int] = {}
    for index, act in enumerate(texts):
        key_num = _act_dedup_key(act["titre"])
        if key_num is None:
            continue
        key = (act["type"], key_num)
        if key not in best_index or len(act["contenu"]) > len(texts[best_index[key]]["contenu"]):
            best_index[key] = index

    kept = set(best_index.values())
    result: List[Dict[str, str]] = []
    for index, act in enumerate(texts):
        if _act_dedup_key(act["titre"]) is None or index in kept:
            result.append(act)
    return result


def _looks_like_real_act_title(rest_of_line: str) -> bool:
    """Vrai si la suite du mot-clé ressemble à un vrai titre d'acte (numéro,
    date, ou libellé en majuscules) — pas une formule de clôture qui finit par
    se retrouver en tête de ligne après un retour à la ligne du markdown.

    Remédiation 2026-08-02 : appliqué à TOUS les mots-clés de `title_regex`,
    plus seulement à NOTE/RAPPORT (l'ancien `_WEAK_ACT_KEYWORDS`). Confirmé en
    prod : « le présent arrêté pourra faire l'objet d'une suspension… » et
    « …sera publié et communiqué partout où besoin sera. » sont des clauses
    de clôture ordinaires du Journal Officiel congolais qui, une fois la ligne
    coupée par le rendu markdown, commencent par « arrêté »/« communiqué » —
    pris à tort pour le début d'un nouvel acte (24 et 21 documents fantômes
    trouvés dans un seul lot). Un vrai titre d'acte porte TOUJOURS l'un des
    trois marqueurs ci-dessous ; une clause de clôture, en minuscules et sans
    numéro ni date propres, n'en porte aucun."""
    if re.search(r"\bN[°ºo]\s*\d", rest_of_line, flags=re.IGNORECASE):
        return True
    if re.search(r"\b\d{1,2}\s+[a-zà-ÿ]+\s+\d{4}\b", rest_of_line, flags=re.IGNORECASE):
        return True
    # Libellé en majuscules (« RAPPORT DE PRÉSENTATION… ») vs clause de
    # clôture en minuscules (« pourra faire l'objet d'une suspension… »).
    return bool(re.match(r"[\s’'A-ZÀ-Ÿ\-]{8,}", rest_of_line))


# Formule d'autorité qui ouvre le dispositif (« Le Président de la République, »,
# « Le ministre d'Etat, ministre de… », « L'Assemblée nationale et le Sénat »,
# « Vu … », « Considérant … ») — jamais le prolongement d'un objet de titre
# (« portant… », « relatif à… », « fixant… »). Marqueur d'arrêt fiable pour la
# continuation multi-ligne du titre : constaté identique sur les lois, décrets
# ET arrêtés d'un même JO (congo-jo-2023-48). Un rôle générique (« Le ministre »,
# sans nommer le ministère) suffit : pas besoin d'énumérer chaque intitulé de
# portefeuille, qui varie à chaque remaniement.
_CORPS_ACTE_REGEX = re.compile(
    r"^(?:"
    r"L['’]ASSEMBL[ÉE]E\s+NATIONALE"
    r"|LE\s+S[ÉE]NAT\b"
    r"|LE\s+PR[ÉE]SIDENT\s+DE\s+LA\s+R[ÉE]PUBLIQUE"
    r"|LE\s+GOUVERNEMENT\b"
    r"|LE\s+PREMIER\s+MINISTRE"
    r"|LE\s+MINISTRE|LA\s+MINISTRE"
    r"|LE\s+SECR[ÉE]TAIRE\s+G[ÉE]N[ÉE]RAL"
    r"|LE\s+DIRECTEUR\s+G[ÉE]N[ÉE]RAL|LA\s+DIRECTRICE\s+G[ÉE]N[ÉE]RALE"
    r"|VU\b|CONSID[ÉE]RANT\b"
    r"|ARTICLE\s+(?:PREMIER|1(?:ER)?)\b"
    r")",
    flags=re.IGNORECASE,
)

# Bruit de saut de page (pied de page + en-tête répétée du JO) — jamais la
# continuation d'un titre. Constaté en avalant le pied de page dans le titre
# d'un « acte en abrégé » (avis de nomination/agrément court, sans structure
# Vu/Considérant, donc sans marqueur `_CORPS_ACTE_REGEX` pour arrêter avant) :
# « Arrêté n° 10444 du 20 décembre 2010. La \n société DELTA MARINE… \n 1108 \n
# Journal officiel de la République du Congo \n N° 52-2010 \n [[MIBEKO_PAGE:29]] »
# — sans ce garde-fou, le numéro de page et l'en-tête entraient dans le titre.
_SAUT_DE_PAGE_REGEX = re.compile(
    r"^(?:\[\[MIBEKO_PAGE:\d+\]\]|\d{1,5}|Journal\s+off[ﬁi]ciel\s+de\s+la\s+R[ÉE]publique|N°\s*\d+[\-–]\d{4}\s*$)",
    flags=re.IGNORECASE,
)

# La continuation multi-ligne n'est activée QUE pour les genres normatifs
# (LOI/DÉCRET/ARRÊTÉ/ORDONNANCE/DÉCISION), où le motif « n° + date + portant/
# relatif à/fixant… » est stable et où `_CORPS_ACTE_REGEX` fournit un marqueur
# d'arrêt fiable. Les genres narratifs (COMMUNIQUÉ, DISCOURS, ALLOCUTION,
# RAPPORT, NOTE, AVIS, CIRCULAIRE…) n'ont pas cette structure — un communiqué
# enchaîne souvent directement sur une phrase de récit, sans aucune formule
# d'autorité pour arrêter la continuation. Gardés au comportement historique
# (titre = première ligne) plutôt que de risquer d'avaler le corps du texte.
_TYPES_ACTE_NORMATIF_REGEX = re.compile(
    r"^(LOI|D[ÉE]CRET|ARR[ÊE]T[ÉE]|ORDONNANCE|D[ÉE]CISION)\b", flags=re.IGNORECASE
)

# Un titre s'étend rarement sur plus de quelques lignes physiques — plafond de
# sécurité pour ne jamais avaler tout un acte si aucun marqueur d'arrêt n'est
# reconnu (texte atypique, formule d'autorité absente du référentiel ci-dessus).
_MAX_LIGNES_CONTINUATION_TITRE = 6


def _continuer_titre_multiligne(
    lignes: List[str], depart: int, title_regex: "re.Pattern[str]"
) -> tuple[List[str], int]:
    """Accumule les lignes qui prolongent un titre coupé par la mise en page du
    PDF (ex. « Loi n° 33-2023 du 17 novembre 2023 portant » / « gestion durable
    de l'environnement en République du » / « Congo »), jusqu'à un marqueur
    fiable de début de dispositif.

    Renvoie (lignes_de_continuation, index_de_la_première_ligne_non_consommée).
    Ne consomme rien si la ligne suivante est vide, démarre un nouvel acte, ou
    ouvre le dispositif : dans ces cas le titre initial reste tel quel, comme
    avant ce correctif.
    """
    suite: List[str] = []
    i = depart
    while i < len(lignes) and len(suite) < _MAX_LIGNES_CONTINUATION_TITRE:
        candidate = lignes[i].strip()
        if not candidate:
            break
        if title_regex.match(candidate):
            break
        if _CORPS_ACTE_REGEX.match(candidate):
            break
        if _SAUT_DE_PAGE_REGEX.match(candidate):
            break
        suite.append(candidate)
        i += 1
    return suite, i


def split_official_journal_markdown(markdown_text: str) -> List[Dict[str, str]]:
    """Découpe un Journal Officiel en actes unitaires à partir du markdown OCRisé."""

    content = sanitize_legal_text(markdown_text)
    lines = content.split("\n")
    texts: List[Dict[str, str]] = []
    current_lines: List[str] = []
    current_title: Optional[str] = None
    title_regex = re.compile(
        r"^(PROCLAIMATION|PROCLAMATION|DISCOURS|ALLOCUTION|ALLOCATION|DELIBERATION|LOI|D[ÉE]CRET|ARR[ÊE]T[ÉE]|ORDONNANCE|D[ÉE]CISION|CIRCULAIRE|AVIS|COMMUNIQU[ÉE]|RAPPORT|NOTE)\b.*$",
        flags=re.IGNORECASE,
    )
    # Entrées du sommaire : « Loi n° 1/58 du ... (page 25). » — à ne pas
    # confondre avec le début réel d'un acte plus loin dans le document.
    toc_entry_regex = re.compile(r"\(\s*p(?:age)?\.?\s*\d+\s*\)\s*\.?\s*$", flags=re.IGNORECASE)

    def flush() -> None:
        if current_title:
            texts.append(
                {
                    "titre": current_title,
                    "contenu": "\n".join(current_lines),
                    "type": detect_texte_type(current_title),
                }
            )

    index = 0
    while index < len(lines):
        raw_line = lines[index]
        line = raw_line.strip()
        # Retire les décorations markdown (#, gras) avant détection du titre.
        cleaned = re.sub(r"^[#>\s]*[*_]{0,3}\s*", "", line)
        cleaned = re.sub(r"\s*[*_]{1,3}$", "", cleaned)

        title_match = title_regex.match(cleaned)
        # Un vrai titre d'acte porte une suite (numéro, date, objet). Une ligne
        # réduite au mot-clé (« Arrête : », « Décrète : ») est le verbe du
        # dispositif, pas un nouvel acte.
        has_substance = title_match and re.search(r"[0-9A-Za-zÀ-ÿ]", cleaned[title_match.end(1):])
        is_act_start = bool(title_match and has_substance and not toc_entry_regex.search(cleaned))
        if is_act_start:
            is_act_start = _looks_like_real_act_title(cleaned[title_match.end(1):])
        if is_act_start:
            flush()
            if _TYPES_ACTE_NORMATIF_REGEX.match(cleaned):
                suite, index = _continuer_titre_multiligne(lines, index + 1, title_regex)
            else:
                suite, index = [], index + 1
            current_title = " ".join([cleaned, *suite]) if suite else cleaned
            # La ligne de titre (et ses éventuelles lignes de continuation) n'est
            # PAS reversée dans le contenu : elle est déjà conservée comme titre
            # de l'acte (titre_officiel). Évite de répéter le titre en tête du
            # préambule.
            current_lines = []
            continue

        if current_title:
            current_lines.append(raw_line)
        index += 1

    flush()
    return _dedupe_official_journal_acts(texts)


def clear_document_structure(db: Session, document_id: uuid.UUID) -> None:
    """Supprime proprement la structure et les articles d’un document avant réimport."""

    db.query(ArticleVersion).filter(
        ArticleVersion.article_id.in_(db.query(Article.id).filter(Article.document_id == document_id))
    ).delete(synchronize_session=False)
    db.query(Article).filter(Article.document_id == document_id).delete(synchronize_session=False)
    db.query(StructureNode).filter(StructureNode.document_id == document_id).delete(synchronize_session=False)
    db.flush()


_DOUBLON_SUFFIX_RE = re.compile(r"^(?P<origine>.*)_doublon_(?P<n>\d+)$")


def derive_numero_origine(numero_article: str) -> Optional[str]:
    """Inverse de la logique de suffixage de `unique_article_number`
    (`ingest_hierarchy`) : retrouve le numéro d'origine d'un `numero_article`
    déjà suffixé ``_doublon_N``, ou ``None`` s'il n'est pas suffixé.

    Utilisé par `scripts/backfill_doublon_flags.py` (audit
    docs/audit-ingestion-2026-08-02.md, phase 2) pour rattraper les articles
    déjà renommés par une ingestion antérieure au correctif, sans re-parser
    ni re-ingérer quoi que ce soit.
    """
    match = _DOUBLON_SUFFIX_RE.match(numero_article)
    return match.group("origine") if match else None


_LATIN_SUB_SUFFIX_RE = re.compile(r"\b(?:bis|ter|quater|quinquies|sexies|septies)\b", re.IGNORECASE)
_HYPHEN_SUB_NUMBER_RE = re.compile(r"-\d+$")
_DECIMAL_SUB_NUMBER_RE = re.compile(r"^\d+\.\d+")
_LETTERED_SUB_ITEM_RE = re.compile(r"\d\s+[a-z]\)?$", re.IGNORECASE)


def ordinal_from_raw_number(raw_number: str) -> Optional[int]:
    """Numéro ordinal d'un article pour le contrôle de séquence, à partir de
    son numéro D'ORIGINE (pas encore suffixé `_doublon_N` — appliquer
    `derive_numero_origine` d'abord si nécessaire) :

    - « premier » → 1 (forme légale officielle, non normalisée à l'affichage
      où `numero_article` reste « premier ») ;
    - « X nouveau » → hors séquence, ``None`` : c'est le texte de remplacement
      cité dans un acte modificatif, pas un article séquentiel (sinon faux
      doublon avec l'article d'exécution de même numéro) ;
    - « 66 bis », « 16-1 », « 1.11 », « 14 a) » → hors séquence, ``None``
      également : insertions ultérieures ou sous-paragraphes lettrés
      légitimes (Code civil, Code Pénal, règlements CEMAC à numérotation
      décimale) qui partagent le numéro DE BASE d'un article déjà existant
      sans en être des doublons — remédiation 2026-08-02 phase 5, audit
      ingestion 652→346 flags `article_doublon` dont 285 sur 3 codes STOCK
      relevaient exactement de ce schéma. Seul le numéro de base (« 66 »,
      « 16 », « 1 », « 14 ») reste dans la séquence : le contrôle de trous
      (`find_missing_runs`) continue de le voir présent, la détection de
      doublons ne voit plus jamais ses insertions comme des répétitions du
      même ordinal.

    Extrait de `ingest_hierarchy` (`insert_nodes`) pour être réutilisé sans
    duplication par tout script qui reconstruit une séquence d'articles déjà
    en base (ex. `scripts/reclassify_embedded_series_flags.py`).
    """
    low_number = raw_number.lower()
    if "nouveau" in low_number or "nouvelle" in low_number:
        return None
    if low_number.startswith(("premier", "première", "premiere")):
        return 1
    if (
        _LATIN_SUB_SUFFIX_RE.search(raw_number)
        or _HYPHEN_SUB_NUMBER_RE.search(raw_number)
        or _DECIMAL_SUB_NUMBER_RE.match(raw_number)
        or _LETTERED_SUB_ITEM_RE.search(raw_number)
    ):
        return None
    seq_match = re.match(r"(\d+)", raw_number)
    return int(seq_match.group(1)) if seq_match else None


def find_missing_runs(distinct_sorted: List[int]) -> List[Tuple[int, int, int]]:
    """Plages d'entiers ABSENTES dans [min, max] de l'ensemble fourni.

    Basé sur l'ENSEMBLE (pas la séquence) : robuste à un OCR qui restitue les
    pages dans le désordre — un trou « 487→552 » réel est détecté quel que soit
    l'ordre d'apparition des articles. Chaque plage : (début, fin, taille).
    """
    runs: List[Tuple[int, int, int]] = []
    if not distinct_sorted:
        return runs

    present = set(distinct_sorted)
    lo, hi = distinct_sorted[0], distinct_sorted[-1]
    start: Optional[int] = None
    for value in range(lo, hi + 1):
        if value not in present:
            if start is None:
                start = value
        elif start is not None:
            runs.append((start, value - 1, value - start))
            start = None
    return runs


def count_series_restarts(ordinals: List[int], restart_low: int = 3, restart_prev_min: int = 10) -> int:
    """Compte les redémarrages de numérotation (chute vers un petit numéro après
    un numéro élevé) : signal d'une compilation de plusieurs textes dont chacun
    renumérote ses articles à partir de 1."""
    restarts = 0
    prev: Optional[int] = None
    for number in ordinals:
        if prev is not None and number <= restart_low and prev > restart_prev_min:
            restarts += 1
        prev = number
    return restarts


def find_embedded_series_runs(
    ordinals: List[int],
    restart_low: int = 3,
    min_run_length: int = 3,
) -> List[Tuple[int, int]]:
    """Repère une série secondaire INCRUSTÉE, manquée par `count_series_restarts`
    faute d'atteindre son seuil absolu `restart_prev_min` (calibré pour des
    recueils longs — un acte principal COURT (loi de ratification à 2-3
    articles, arrêté bref) suivi d'une annexe qui renumérote à partir de 1
    (traité ratifié, cahier des charges) ne fait jamais dépasser ce seuil au
    sommet atteint avant la chute — audit 2026-08-02 phase 4, remédiation :
    652 signalements `article_doublon` sur 70 documents, dont la majorité
    suivait exactement ce schéma sur 6 échantillons vérifiés (1959 à 2025).

    Signal retenu, indépendant de l'ampleur absolue : une VRAIE chute (`prev`
    STRICTEMENT supérieur — un doublon immédiat où les deux valeurs sont
    égales n'en est pas une) vers une petite valeur, confirmée seulement si ce
    qui suit reprend une ascension soutenue d'au moins `min_run_length`
    valeurs. Sans cette confirmation, ce n'est pas une série qui s'installe
    mais une réapparition isolée (erratum, doublon OCR) — qui doit rester
    signalée normalement (cf. audit phase 2 : doublons non consécutifs,
    `test_doublon_non_consecutif_detecte_hors_compilation`).

    Renvoie les plages (index début, index fin inclus, dans `ordinals`) des
    séries ainsi confirmées.
    """
    runs: List[Tuple[int, int]] = []
    i = 1
    n = len(ordinals)
    while i < n:
        prev, number = ordinals[i - 1], ordinals[i]
        if number <= restart_low and prev > number:
            j = i
            while j + 1 < n and ordinals[j + 1] > ordinals[j]:
                j += 1
            if j - i + 1 >= min_run_length:
                runs.append((i, j))
                i = j + 1
                continue
        i += 1
    return runs


def analyze_article_sequence(
    sequence: List[Tuple[Optional[int], uuid.UUID]],
    *,
    block_threshold: int = 10,
    restart_low: int = 3,
    restart_prev_min: int = 10,
    min_restarts: int = 2,
    min_embedded_series_run: int = 3,
    max_anomalies: int = 200,
    already_flagged: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Analyse PURE d'une séquence d'articles (ordinal, id) → anomalies à signaler.

    Vise le RAPPEL sur les vrais défauts d'ingestion tout en restant robuste à un
    OCR désordonné (où l'analyse séquentielle des trous produit des faux positifs
    massifs : « saut de 86 à 553 » alors que l'article 553 a juste été restitué
    trop tôt). Trois familles d'anomalies :

    - ``article_doublon`` : un même numéro ordinal apparaît plusieurs fois. Hors
      compilation, TOUTE réapparition compte (consécutive ou non — audit
      docs/audit-ingestion-2026-08-02.md phase 2 : « 12 » puis, bien plus loin,
      « 12 bis » partagent le même ordinal sans être adjacents dans la séquence
      ni entrer en collision de CHAÎNE, donc jamais renommés par
      ``unique_article_number``). En compilation, seuls les doublons CONSÉCUTIFS
      restent signalés : la réapparition d'un numéro sur un acte encapsulé
      différent est le signal normal d'une nouvelle série, pas une erreur — tout
      signaler produirait exactement le bruit que cette fonction évite déjà pour
      les petits trous (cf. plus bas). Hors compilation *au sens de
      `count_series_restarts`*, une série secondaire INCRUSTÉE peut cependant
      être confirmée par `find_embedded_series_runs` (acte principal trop
      court pour franchir `restart_prev_min`, ex. loi de ratification à 2
      articles + traité annexé qui renumérote) : ses réapparitions ne sont
      alors PAS comptées comme doublons, un seul ``compilation_suspectee``
      les remplace (remédiation 2026-08-02 phase 4) ;
    - ``compilation_suspectee`` : la numérotation redémarre plusieurs fois
      (plusieurs séries) → le document devrait être segmenté avant publication ;
    - ``bloc_manquant`` / ``article_manquant`` : plages absentes calculées sur
      l'ENSEMBLE des numéros (indépendant de l'ordre). Un bloc contigu long
      (≥ ``block_threshold``) est signalé même en compilation (peu probable que ce
      soit un acte encapsulé, lesquels sont peu numérotés) ; les petits trous ne
      sont signalés QUE hors compilation (sinon bruit dû à l'entrelacement des
      séries d'une compilation).

    ``already_flagged`` : ensemble d'``article_id`` déjà couverts par un
    `CurationFlag` `article_doublon` distinct (collision de CHAÎNE détectée et
    renommée par ``unique_article_number`` — cf. `ingest_hierarchy`). Ces
    articles sont exclus de la détection ci-dessous pour éviter un double
    signalement du même défaut (mission « corrections post-audit » phase 2 :
    un flag par collision, pas deux).

    Renvoie une liste de dicts {type_probleme, article_id, description}.
    """
    anomalies: List[Dict[str, Any]] = []
    already_flagged = already_flagged or set()

    # `filtered` et `ordinals` restent alignés index à index (indispensable
    # pour reprojeter les plages de `find_embedded_series_runs`, calculées sur
    # `ordinals`, vers les `article_id` correspondants).
    filtered = [(number, article_id) for number, article_id in sequence if number is not None]
    ordinals = [number for number, _ in filtered]
    if not ordinals:
        return anomalies[:max_anomalies]

    # 1. Compilation (plusieurs séries de numérotation) ? Calculé AVANT les
    # doublons : leur détection en dépend (cf. docstring).
    restarts = count_series_restarts(ordinals, restart_low, restart_prev_min)
    is_compilation = restarts >= min_restarts
    embedded_runs: List[Tuple[int, int]] = (
        [] if is_compilation else find_embedded_series_runs(ordinals, restart_low, min_embedded_series_run)
    )

    # 2. Doublons.
    if is_compilation:
        # Cœur inchangé : seuls les doublons consécutifs.
        prev: Optional[int] = None
        for number, article_id in sequence:
            if number is None:
                continue
            if prev is not None and number == prev and article_id not in already_flagged:
                anomalies.append({
                    "type_probleme": "article_doublon",
                    "article_id": article_id,
                    "description": f"Numéro d'article {number} répété consécutivement.",
                })
            prev = number
    else:
        # Hors compilation, un ordinal ne devrait apparaître qu'une fois où
        # qu'il soit dans la séquence — SAUF s'il appartient à une série
        # secondaire incrustée confirmée (`embedded_runs`) : celle-ci est
        # signalée une seule fois, globalement, plus bas.
        embedded_indices = {i for start, end in embedded_runs for i in range(start, end + 1)}
        seen_ordinals: Dict[int, uuid.UUID] = {}
        for idx, (number, article_id) in enumerate(filtered):
            if idx in embedded_indices:
                seen_ordinals[number] = article_id
                continue
            if number in seen_ordinals:
                if article_id not in already_flagged:
                    anomalies.append({
                        "type_probleme": "article_doublon",
                        "article_id": article_id,
                        "description": f"Numéro d'article {number} déjà rencontré ailleurs dans ce document.",
                    })
            else:
                seen_ordinals[number] = article_id

        for start, end in embedded_runs:
            anomalies.append({
                "type_probleme": "compilation_suspectee",
                "article_id": None,
                # 'warning' et non 'blocking' (défaut) : contrairement à une VRAIE
                # compilation multi-actes (cf. plus bas), une série incrustée
                # unique est le signal normal d'une annexe — informe sans
                # bloquer la publication (seul 'blocking' bloque côté Laravel).
                "severity": "warning",
                "description": (
                    f"La numérotation redémarre à l'article {ordinals[start]} après l'article "
                    f"{ordinals[start - 1]} et reprend une série de {end - start + 1} articles : "
                    "texte secondaire probable (annexe, traité ratifié, cahier des charges) plutôt "
                    "qu'un doublon. Vérifier si une segmentation est nécessaire avant publication."
                ),
            })

    if is_compilation:
        anomalies.append({
            "type_probleme": "compilation_suspectee",
            "article_id": None,
            "description": (
                f"La numérotation d'articles redémarre {restarts} fois "
                "(plusieurs séries détectées) : compilation probable de plusieurs "
                "textes. Segmentation recommandée avant publication."
            ),
        })

    # 3. Plages absentes (basé sur l'ensemble, robuste à l'ordre).
    for start, end, size in find_missing_runs(sorted(set(ordinals))):
        if size >= block_threshold:
            anomalies.append({
                "type_probleme": "bloc_manquant",
                "article_id": None,
                "description": (
                    f"{size} numéros d'articles consécutifs absents ({start}-{end}) : "
                    "perte de pages probable à l'extraction."
                ),
            })
        elif not is_compilation:
            label = f"{start}" if start == end else f"{start}-{end}"
            anomalies.append({
                "type_probleme": "article_manquant",
                "article_id": None,
                "description": f"Article(s) {label} absent(s) ({size} numéro(s)).",
            })

    return anomalies[:max_anomalies]


def flag_article_sequence_anomalies(
    db: Session,
    document_id: uuid.UUID,
    sequence: List[Tuple[Optional[int], uuid.UUID]],
    max_flags: int = 200,
    already_flagged: Optional[set] = None,
) -> int:
    """Persiste les anomalies de numérotation d'un document en `curation_flags`.

    Garde-fou de curation : tant que ces anomalies ne sont pas résolues, le
    document ne peut pas être publié (cf. `LegalDocumentController`). L'analyse est
    déléguée à `analyze_article_sequence` (pure, testée). Renvoie le nb d'anomalies.

    ``already_flagged`` : cf. `analyze_article_sequence` — articles déjà
    flagués par `unique_article_number` (collision de chaîne renommée), à ne
    pas re-signaler ici pour la même collision vue sous l'angle ordinal.
    """
    # Idempotence : on purge nos propres signalements heuristiques NON résolus
    # avant de recalculer (ré-import/replay), sans toucher aux flags résolus par un
    # humain ni aux autres couches (structural/llm). Les flags article-liés
    # partent déjà en CASCADE quand l'article est supprimé ; ceci couvre en plus
    # les flags niveau document (article_id NULL : bloc manquant, compilation).
    # NB : les flags de collision de `unique_article_number` (posés juste avant
    # cet appel, cf. `ingest_hierarchy`) sont volontairement `source="heuristic"`
    # eux aussi — non purgés ici : cette requête ne vise QUE ceux déjà en base
    # avant l'appel courant (rejeu), les flags fraîchement ajoutés dans cette
    # même transaction n'y sont pas encore visibles côté SQL.
    db.query(CurationFlag).filter(
        CurationFlag.document_id == document_id,
        CurationFlag.source == "heuristic",
        CurationFlag.resolved.is_(False),
    ).delete(synchronize_session=False)

    anomalies = analyze_article_sequence(sequence, max_anomalies=max_flags, already_flagged=already_flagged)
    for anomaly in anomalies:
        db.add(CurationFlag(
            document_id=document_id,
            article_id=anomaly["article_id"],
            source="heuristic",
            type_probleme=anomaly["type_probleme"],
            # Défaut du modèle ('blocking') sauf anomalie explicitement moins
            # sévère (ex. série incrustée confirmée — cf. `analyze_article_sequence`).
            severity=anomaly.get("severity", "blocking"),
            description=anomaly["description"],
        ))
    return len(anomalies)


def assign_dfs_order(hierarchy: List[Dict[str, Any]]) -> None:
    """Assigne un ordre d'affichage GLOBAL (pré-ordre DFS) à chaque nœud.

    Écrit la clé ``_order`` (entier croissant unique sur tout le document) sur
    chaque nœud, dans l'ordre de lecture (un parent précède ses enfants, qui
    précèdent le frère suivant du parent). Sans cela, ``ordre_affichage`` /
    ``sort_order`` ne sont monotones qu'au sein d'un groupe de frères (reset à 0
    par branche) : un listing à plat ``ORDER BY ordre_affichage`` ressort alors
    mélangé. Le viewer arborescent (qui regroupe par parent) reste correct dans
    les deux cas, mais une clé globale rend le modèle robuste pour tout
    consommateur à plat (API d'extraction paginée, exports).
    """

    counter = {"n": 0}

    def walk(nodes: List[Dict[str, Any]]) -> None:
        for node in nodes:
            node["_order"] = counter["n"]
            counter["n"] += 1
            children = node.get("children")
            if children:
                walk(children)

    walk(hierarchy)


def ingest_hierarchy(
    db: Session,
    document: LegalDocument,
    hierarchy: List[Dict[str, Any]],
    run_id: Optional[uuid.UUID] = None,
    media_id: Optional[uuid.UUID] = None,
    validation_status: str = "pending",
) -> None:
    """Insère une hiérarchie parsée dans `structure_nodes`, `articles` et `article_versions`."""

    clear_document_structure(db, document.id)
    seen_article_numbers: Dict[str, int] = {}
    rename_collisions: List[Dict[str, Any]] = []
    table_counter = {"n": 0}
    article_sequence: List[Tuple[Optional[int], uuid.UUID]] = []

    def unique_article_number(base: str, node_id: uuid.UUID) -> str:
        """Garantit l'unicité de (document_id, numero_article).

        Suffixe ``_doublon_N`` en cas de collision — la contrainte unique
        uq_articles_document_numero l'exige. S'applique aux vrais articles
        homonymes MAIS AUSSI aux feuilles PREAMBULE/SIGNATURE multiples : un acte
        compilé (Code bleu, recueil) peut contenir plusieurs préambules/signatures.

        Le renommage n'est JAMAIS silencieux (audit
        docs/audit-ingestion-2026-08-02.md, phase 2 : sur les 62 documents
        rescindés en phase 1, 5 525 renommages n'avaient donné lieu qu'à 315
        signalements individuels) : chaque collision est enregistrée dans
        ``rename_collisions``, transformée en `CurationFlag` `article_doublon`
        une fois l'article inséré (cf. fin de fonction), numéro d'origine
        conservé dans la description.
        """
        if base in seen_article_numbers:
            seen_article_numbers[base] += 1
            final = f"{base}_doublon_{seen_article_numbers[base]}"
            rename_collisions.append({"article_id": node_id, "numero_origine": base, "numero_final": final})
            return final
        seen_article_numbers[base] = 0
        return base

    def insert_nodes(nodes_list: List[Dict[str, Any]], parent_tree_path: Optional[str] = None, parent_node_id: Optional[uuid.UUID] = None) -> None:
        for node_data in nodes_list:
            # Ordre d'affichage GLOBAL (pré-ordre DFS) calculé par assign_dfs_order.
            display_order = node_data["_order"]
            node_id = uuid.uuid4()
            # Ltree labels must start with a letter. We prefix with 'n' (node)
            node_ltree_id = f"n_{str(node_id).replace('-', '_')}"
            current_tree_path = f"{parent_tree_path}.{node_ltree_id}" if parent_tree_path else node_ltree_id
            ltree_obj = Ltree(current_tree_path)

            if node_data["type"] == "ARTICLE":
                raw_number = str(node_data.get("number", "")).strip()
                article_number = unique_article_number(raw_number or f"SANS_NUM_{str(uuid.uuid4())[:8]}", node_id)

                article = Article(
                    id=node_id,
                    document_id=document.id,
                    parent_node_id=parent_node_id,
                    numero_article=article_number,
                    ordre_affichage=display_order,
                    validation_status=validation_status,
                )
                db.add(article)

                # Numéro ordinal pour le contrôle de séquence (cf. `ordinal_from_raw_number`).
                article_sequence.append((ordinal_from_raw_number(raw_number), article.id))

                version = ArticleVersion(
                    article_id=article.id,
                    contenu_texte=node_data.get("content", ""),
                    validity_period=DateRange(datetime.datetime.utcnow().date(), None),
                    source_run_id=run_id,
                    source_media_file_id=media_id,
                    source_locator={"page": node_data["page"]} if node_data.get("page") is not None else {},
                    validation_status=validation_status,
                )
                db.add(version)
            elif node_data["type"] == "PREAMBULE":
                # Préambule de l'acte (qualité du signataire, visas, considérants) :
                # feuille de tête marquée content_format=preamble dans source_locator
                # (même approche que TABLEAU, zéro migration). Hors contrôle de
                # séquence : ce n'est pas un article numéroté.
                preamble_locator: Dict[str, Any] = {"content_format": "preamble"}
                if node_data.get("page") is not None:
                    preamble_locator["page"] = node_data["page"]

                article = Article(
                    id=node_id,
                    document_id=document.id,
                    parent_node_id=parent_node_id,
                    numero_article=unique_article_number("PREAMBULE", node_id),
                    ordre_affichage=display_order,
                    validation_status=validation_status,
                )
                db.add(article)

                version = ArticleVersion(
                    article_id=article.id,
                    contenu_texte=node_data.get("content", ""),
                    validity_period=DateRange(datetime.datetime.utcnow().date(), None),
                    source_run_id=run_id,
                    source_media_file_id=media_id,
                    source_locator=preamble_locator,
                    validation_status=validation_status,
                )
                db.add(version)
            elif node_data["type"] == "SIGNATURE":
                # Formule finale (« Fait à … » + signataire) : feuille de pied
                # marquée content_format=signature (même approche que TABLEAU,
                # zéro migration). Hors contrôle de séquence d'articles.
                signature_locator: Dict[str, Any] = {"content_format": "signature"}
                if node_data.get("page") is not None:
                    signature_locator["page"] = node_data["page"]

                article = Article(
                    id=node_id,
                    document_id=document.id,
                    parent_node_id=parent_node_id,
                    numero_article=unique_article_number("SIGNATURE", node_id),
                    ordre_affichage=display_order,
                    validation_status=validation_status,
                )
                db.add(article)

                version = ArticleVersion(
                    article_id=article.id,
                    contenu_texte=node_data.get("content", ""),
                    validity_period=DateRange(datetime.datetime.utcnow().date(), None),
                    source_run_id=run_id,
                    source_media_file_id=media_id,
                    source_locator=signature_locator,
                    validation_status=validation_status,
                )
                db.add(version)
            elif node_data["type"] == "TABLEAU":
                # Tableau (grille salariale, etc.) : feuille de contenu marquée
                # content_format=table dans source_locator (option A, zéro migration).
                table_counter["n"] += 1
                table_locator: Dict[str, Any] = {"content_format": "table"}
                if node_data.get("page") is not None:
                    table_locator["page"] = node_data["page"]

                article = Article(
                    id=node_id,
                    document_id=document.id,
                    parent_node_id=parent_node_id,
                    numero_article=f"TABLEAU_{table_counter['n']}",
                    ordre_affichage=display_order,
                    validation_status=validation_status,
                )
                db.add(article)

                version = ArticleVersion(
                    article_id=article.id,
                    contenu_texte=node_data.get("content", ""),
                    validity_period=DateRange(datetime.datetime.utcnow().date(), None),
                    source_run_id=run_id,
                    source_media_file_id=media_id,
                    source_locator=table_locator,
                    validation_status=validation_status,
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
                    sort_order=display_order,
                    validation_status=validation_status,
                )
                db.add(node)
                db.flush()

                if node_data.get("children"):
                    insert_nodes(node_data["children"], parent_tree_path=current_tree_path, parent_node_id=node.id)

    if hierarchy:
        assign_dfs_order(hierarchy)
        insert_nodes(hierarchy)
        # Flush explicite obligatoire ici : la session est `autoflush=False`
        # (src/db/database.py) et `insert_nodes` ne flushe qu'au passage de
        # chaque `StructureNode` (ligne ci-dessus) — les articles/PREAMBULE/
        # SIGNATURE en fin d'arbre, après le dernier `StructureNode`, restent
        # donc en attente jusqu'ici. Sans ce flush, la boucle `rename_collisions`
        # plus bas ajoute des `CurationFlag` référençant CES MÊMES articles
        # encore non persistés → violation de clé étrangère au commit final
        # (constaté sur le Code pénal 1836 : 12 articles jamais flushés, dont 5
        # référencés par un flag de doublon).
        db.flush()
        # `flag_article_sequence_anomalies` PURGE d'abord (requête SQL directe
        # sur les flags heuristiques existants) puis ajoute les siens : les
        # flags de collision ci-dessous sont ajoutés APRÈS cet appel, jamais
        # avant — sinon un autoflush déclenché par la requête de purge les
        # écrirait en base juste à temps pour être supprimés par cette même
        # purge (elle filtre aussi `source="heuristic"`).
        already_renamed_ids = {collision["article_id"] for collision in rename_collisions}
        flag_article_sequence_anomalies(db, document.id, article_sequence, already_flagged=already_renamed_ids)

        # Séries secondaires incrustées confirmées sur la séquence ordinale
        # COMPLÈTE (article_sequence, remplie ci-dessus) : le renommage de
        # chaque collision reste TOUJOURS individuellement flagué — jamais
        # silencieux, audit 2026-08-02 phase 2, cf. test_zero_renommage_sans_flag
        # — mais un renommage qui appartient à une annexe confirmée n'est pas
        # de même nature qu'une vraie collision non résolue : description et
        # sévérité l'indiquent (remédiation phase 4), sans jamais réduire le
        # nombre de flags ni leur bijection avec les renommages.
        filtered_for_embedded = [(n, aid) for n, aid in article_sequence if n is not None]
        ordinals_for_embedded = [n for n, _ in filtered_for_embedded]
        embedded_run_ids = {
            filtered_for_embedded[i][1]
            for start, end in find_embedded_series_runs(ordinals_for_embedded)
            for i in range(start, end + 1)
        }

        for collision in rename_collisions:
            if collision["article_id"] in embedded_run_ids:
                severity = "warning"
                description = (
                    f"Numéro d'article « {collision['numero_origine']} » renommé en "
                    f"« {collision['numero_final']} » : appartient probablement à une série "
                    "secondaire (annexe, traité ratifié, cahier des charges) qui redémarre sa "
                    "propre numérotation — pas nécessairement une erreur, à confirmer."
                )
            else:
                severity = "blocking"
                description = (
                    f"Numéro d'article « {collision['numero_origine']} » en collision avec un autre "
                    f"article du même document — renommé en « {collision['numero_final']} » pour "
                    "respecter la contrainte d'unicité. Nécessite une revue humaine (fusion, "
                    "re-numérotation, ou confirmation qu'il s'agit bien de deux articles distincts)."
                )
            db.add(CurationFlag(
                document_id=document.id,
                article_id=collision["article_id"],
                source="heuristic",
                type_probleme="article_doublon",
                severity=severity,
                description=description,
            ))


# ---------------------------------------------------------------------------
# Rejouabilité non-destructive (staging) — un retraitement ne doit jamais
# écraser la curation humaine. La proposition est parquée dans extraction_runs.meta
# (JSONB) puis arbitrée (diff → promote/discard) par un éditeur.
# ---------------------------------------------------------------------------

LEAF_CONTENT_TYPES = {"ARTICLE", "PREAMBULE", "SIGNATURE", "TABLEAU"}


def document_has_curated_content(db: Session, document_id: uuid.UUID) -> bool:
    """Vrai si le document porte du travail humain à protéger d'un écrasement.

    Est considéré « précieux » : un document déjà publié, OU au moins une version
    d'article validée par un éditeur. Dans ce cas, un nouveau parsing est mis en
    attente d'arbitrage (staging) au lieu d'écraser le live.
    """
    document = db.get(LegalDocument, document_id)
    if document is not None and document.curation_status == "published":
        return True

    validated = (
        db.query(ArticleVersion.id)
        .join(Article, ArticleVersion.article_id == Article.id)
        .filter(
            Article.document_id == document_id,
            Article.deleted_at.is_(None),
            ArticleVersion.validation_status == "validated",
        )
        .first()
    )
    return validated is not None


def flatten_hierarchy_articles(hierarchy: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """Aplati une hiérarchie parsée en liste ordonnée de (numéro, contenu) pour
    les feuilles porteuses de texte (articles, préambule, signature, tableaux)."""
    flat: List[Tuple[str, str]] = []

    def walk(nodes: Optional[List[Dict[str, Any]]]) -> None:
        for node in nodes or []:
            if node.get("type") in LEAF_CONTENT_TYPES:
                number = str(node.get("number") or "").strip() or node["type"]
                flat.append((number, node.get("content", "") or ""))
            walk(node.get("children"))

    walk(hierarchy)
    return flat


def fetch_live_articles(db: Session, document_id: uuid.UUID) -> List[Tuple[str, str]]:
    """(numéro, contenu) de la version courante (validity_period ouverte) de
    chaque article vivant du document — base de comparaison pour le diff."""
    rows = (
        db.query(Article.numero_article, ArticleVersion.contenu_texte)
        .join(ArticleVersion, ArticleVersion.article_id == Article.id)
        .filter(
            Article.document_id == document_id,
            Article.deleted_at.is_(None),
            func.upper_inf(ArticleVersion.validity_period),
        )
        .order_by(Article.ordre_affichage)
        .all()
    )
    return [(row[0], row[1] or "") for row in rows]


def diff_articles(proposed: List[Tuple[str, str]], live: List[Tuple[str, str]]) -> Dict[str, Any]:
    """Compare deux jeux d'articles par numéro : ajouts, suppressions, modifications.

    Le contenu est comparé après normalisation des espaces. Les numéros dupliqués
    sont regroupés : c'est un résumé d'arbitrage, pas un diff ligne à ligne.
    """
    def index(pairs: List[Tuple[str, str]]) -> Dict[str, List[str]]:
        grouped: Dict[str, List[str]] = {}
        for number, content in pairs:
            grouped.setdefault(number, []).append(" ".join((content or "").split()))
        return grouped

    proposed_idx = index(proposed)
    live_idx = index(live)

    added = [n for n in proposed_idx if n not in live_idx]
    removed = [n for n in live_idx if n not in proposed_idx]
    changed = [n for n in proposed_idx if n in live_idx and proposed_idx[n] != live_idx[n]]
    unchanged = [n for n in proposed_idx if n in live_idx and proposed_idx[n] == live_idx[n]]

    return {
        "proposed_count": sum(len(v) for v in proposed_idx.values()),
        "live_count": sum(len(v) for v in live_idx.values()),
        "added": added,
        "removed": removed,
        "changed": changed,
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "unchanged": len(unchanged),
        },
    }


def create_or_update_jo_documents_from_markdown(
    db: Session,
    journal: OfficialJournal,
    markdown_text: str,
    curation_status: str = "review",
    page_source_json: Optional[Dict[str, Any]] = None,
) -> List[LegalDocument]:
    """Crée ou met à jour les actes FLUX d’un Journal Officiel à partir de son markdown.

    Le découpage en actes se fait TOUJOURS sur le markdown (fiable). Si
    `page_source_json` (JSON MinerU du même PDF) est fourni, on s'en sert
    uniquement pour TAMPONNER chaque nœud avec sa page PDF d'origine (citabilité
    « page N »), sans influencer le découpage.
    """

    extracted_texts = split_official_journal_markdown(markdown_text)
    created_documents: List[LegalDocument] = []
    journal_institution_id = resolve_institution_id(db, "JO")

    # Index de pages partagé (curseur avançant) : les actes sont dans l'ordre du
    # document, comme les pages du JSON.
    page_index = build_json_page_index(page_source_json) if page_source_json else []
    page_cursor = [0]

    for extracted in extracted_texts:
        title = extracted["titre"].strip()
        detected_type = extracted["type"]
        reference_nor = extract_reference_from_title(title)
        signature_date = extract_french_date(title)
        unique_scope = sanitize_path_component(reference_nor or title)
        document_key = f"jo:{journal.publication_date.isoformat()}:{sanitize_path_component(journal.number or str(journal.id))}:{unique_scope}"

        document = db.query(LegalDocument).filter(LegalDocument.document_key == document_key).first()
        if not document:
            document = LegalDocument(
                id=uuid.uuid4(),
                official_journal_id=journal.id,
                institution_id=journal_institution_id,
                type_code=map_detected_type_to_type_code(detected_type, db),
                document_key=document_key,
                document_role="FLUX",
                titre_officiel=title,
                reference_nor=reference_nor,
                date_signature=signature_date,
                date_publication=journal.publication_date,
                statut="vigueur",
                legal_scope=resolve_legal_scope(title),
                curation_status=curation_status,
                extraction_status="completed",
            )
            db.add(document)
            db.flush()
        else:
            document.official_journal_id = journal.id
            document.type_code = document.type_code or map_detected_type_to_type_code(detected_type, db)
            document.reference_nor = document.reference_nor or reference_nor
            document.date_signature = document.date_signature or signature_date
            document.date_publication = document.date_publication or journal.publication_date
            document.curation_status = curation_status
            document.extraction_status = "completed"

        act_content = extracted["contenu"]
        if page_index:
            act_content = annotate_markdown_with_pages(act_content, page_index, page_cursor)
        parser = LegalDocumentParser(text_content=act_content)
        hierarchy = parser.parse_hierarchy()
        
        # Fallback pour les documents sans structure (ex: Discours, Proclamation,
        # actes en abrégé) : tout le texte en un article « Unique », tamponné avec
        # la première page repérée pour rester citable.
        if not hierarchy and extracted["contenu"].strip():
            first_marker = re.search(r"\[\[MIBEKO_PAGE:(\d+)\]\]", act_content)
            hierarchy = [{
                "type": "ARTICLE",
                "number": "Unique",
                "title": "Texte intégral",
                "content": extracted["contenu"].strip(),
                "page": int(first_marker.group(1)) if first_marker else None,
                "children": []
            }]

        if hierarchy:
            ingest_hierarchy(db, document, hierarchy, validation_status="validated")

        merge_metadata(
            document,
            {
                "ingestion_mode": "official_journal_upload",
                "official_journal_id": str(journal.id),
                "jo_number": journal.number,
                "detected_type": detected_type,
            },
        )
        created_documents.append(document)

    return created_documents

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


def reap_orphaned_runs() -> None:
    """Marque en échec les traitements restés bloqués après un redémarrage.

    Le pipeline repose sur BackgroundTasks en mémoire process : un redémarrage
    du service abandonne les tâches en cours. Sans reaper, trois états restent
    bloqués indéfiniment :

    - ``extraction_runs.status`` « running »/« queued » (trace du run) ;
    - ``official_journals.transcription_status`` « running »/« queued » — un JO
      redéployé en plein MinerU reste sinon coincé, car la condition de relance
      de l'upload (``in (None, 'pending', 'failed')``) exclut queued/running ;
    - ``legal_documents.extraction_status`` « processing » — un STOCK/FLUX
      abandonné en cours de MinerU garde un statut trompeur.

    On bascule tout ça en « failed » au démarrage (meta tracée pour les runs).
    """

    db = SessionLocal()
    try:
        orphaned_runs = (
            db.query(ExtractionRun)
            .filter(ExtractionRun.status.in_(("running", "queued")))
            .all()
        )
        for run in orphaned_runs:
            run.status = "failed"
            run.finished_at = datetime.datetime.utcnow()
            run.meta = {**(run.meta or {}), "error": "orphaned: service restart"}

        orphaned_journals = (
            db.query(OfficialJournal)
            .filter(OfficialJournal.transcription_status.in_(("running", "queued")))
            .update({OfficialJournal.transcription_status: "failed"}, synchronize_session=False)
        )

        orphaned_documents = (
            db.query(LegalDocument)
            .filter(LegalDocument.extraction_status == "processing")
            .update({LegalDocument.extraction_status: "failed"}, synchronize_session=False)
        )

        db.commit()
        if orphaned_runs or orphaned_journals or orphaned_documents:
            logger.warning(
                "Reaper : %d run(s), %d JO, %d document(s) orphelin(s) marqué(s) en échec après redémarrage.",
                len(orphaned_runs),
                orphaned_journals,
                orphaned_documents,
            )
    except Exception:
        # Un souci DB au démarrage ne doit pas empêcher le service de se lancer
        # (le health check signalera l'indisponibilité de la base).
        db.rollback()
        logger.exception("Reaper : impossible de récupérer les traitements orphelins.")
    finally:
        db.close()


# Résultat du check de drift schéma (P2.3), calculé au démarrage puis mis en
# cache : le schéma DB (piloté par Laravel) ne change pas pendant la vie du
# process. None = pas encore évalué (health le tolère). Exposé dans /health.
_schema_check_cache: Dict[str, Any] = {"ok": None, "issues": []}


def run_schema_check() -> None:
    """Compare le schéma DB aux modèles au démarrage et met le résultat en cache.

    Non bloquant : tout échec est absorbé (le service démarre quand même), un
    écart n'empêche jamais le boot — il est seulement remonté via /health.
    """

    db = SessionLocal()
    try:
        ok, issues = check_schema(db)
        _schema_check_cache["ok"] = ok
        _schema_check_cache["issues"] = issues
        if not ok:
            logger.warning(
                "Schema check : %d écart(s) schéma DB↔modèles détecté(s). %s",
                len(issues),
                " | ".join(issues),
            )
    except Exception:
        logger.exception("Schema check : échec inattendu au démarrage.")
    finally:
        db.close()


@app.on_event("startup")
def on_startup() -> None:
    """Initialise la couche base de donnees au demarrage de l'API."""

    init_db()
    reap_orphaned_runs()
    run_schema_check()

@app.on_event("shutdown")
async def on_shutdown() -> None:
    """Ferme proprement les connexions SSE pour éviter que le serveur ne reste bloqué lors de l'arrêt."""
    for queue in list(event_queues):
        await queue.put((None, None))


# Cache du check DB du health check : l'endpoint est public, on ne veut pas
# qu'un appel anonyme déclenche une requête SQL à chaque hit (audit S4). Le
# statut est réévalué au plus toutes les DB_HEALTH_CACHE_TTL secondes.
DB_HEALTH_CACHE_TTL = 30.0
_db_health_cache: Dict[str, Any] = {"status": None, "checked_at": 0.0}


@app.get("/api/v1/health", response_model=HealthOut, tags=["health"])
def health_check(db: Session = Depends(get_db)):
    """Health check de l'API et de la connexion base de données (check DB mis en cache ~30 s)."""
    now = time.monotonic()
    if _db_health_cache["status"] is None or now - _db_health_cache["checked_at"] >= DB_HEALTH_CACHE_TTL:
        try:
            db.execute(text("SELECT 1"))
            _db_health_cache["status"] = "ok"
        except Exception:
            _db_health_cache["status"] = "error"
        _db_health_cache["checked_at"] = now

    db_status = _db_health_cache["status"]

    return HealthOut(
        status="ok",
        service="mibeko-python",
        version="1.0.0",
        db=db_status,
        timestamp=datetime.datetime.utcnow(),
        schema_ok=_schema_check_cache["ok"],
        schema_issues=_schema_check_cache["issues"],
    )


@app.get("/", include_in_schema=False)
async def read_root(request: Request):
    """Page d'identité du service interne (aucune console publique)."""

    payload = {
        "service": "mibeko-python",
        "role": "service interne d'ingestion et d'extraction",
        "status": "ok",
        "version": SERVICE_VERSION,
        "health": "/api/v1/health",
        "docs": "/api/v1/docs" if EXPOSE_API_DOCS else None,
    }
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(payload)

    return templates.TemplateResponse(
        "status.html",
        {"request": request, "version": SERVICE_VERSION, "docs_enabled": EXPOSE_API_DOCS},
    )


@app.get("/console", response_class=HTMLResponse, include_in_schema=False)
async def legacy_console(request: Request):
    """Console d'ingestion HTMX héritée — désactivée sauf activation explicite.

    Le véritable outil d'ingestion vit désormais dans le front éditeur
    (app.mibeko.fr). Cette console n'est rendue que si INGESTION_CONSOLE_ENABLED.
    """

    if not INGESTION_CONSOLE_ENABLED:
        raise HTTPException(status_code=404, detail="Console désactivée.")

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
    document = db.query(LegalDocument).filter(LegalDocument.id == doc_id).first()
    if document is not None:
        document.extraction_status = "processing"
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

        if result["status"] == "success":
            run.status = "succeeded" if has_markdown and has_json else "partial"
            set_document_status(document, has_markdown, has_json)
        else:
            run.status = "failed"
            document.extraction_status = "failed"
        run.finished_at = datetime.datetime.utcnow()
        run.meta = {**(run.meta or {}), "mineru_task_id": task_id}
        merge_metadata(document, {"latest_extraction_run_id": str(run.id)})

        db.commit()

        if run.status == "failed":
            await notify_clients("notification", json.dumps({"message": "MinerU n'a produit aucun artefact pour ce document.", "type": "error"}))
        else:
            await notify_clients("notification", json.dumps({"message": "MinerU a terminé le traitement du document.", "type": "success"}))
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

        await notify_clients("notification", json.dumps({"message": f"Échec de l'extraction MinerU: {str(exc)}", "type": "error"}))
    finally:
        if os.path.exists(pdf_path):
            os.remove(pdf_path)

        db.close()
        await notify_clients("update", "{}")


async def process_mineru_journal_extraction(
    journal_id: uuid.UUID,
    pdf_path: str,
) -> None:
    """Lance MinerU sur un Journal Officiel, puis crée les actes enfants depuis le markdown."""
    db = SessionLocal()
    journal = db.query(OfficialJournal).filter(OfficialJournal.id == journal_id).first()
    if not journal:
        db.close()
        return

    journal.transcription_status = "running"
    db.commit()
    await notify_clients("update", "{}")

    try:
        task_id = await mineru_service.submit_pdf(pdf_path)
        result = await mineru_service.get_results(task_id)

        if result["status"] == "success":
            # Le JSON (s'il existe) sert de source de pages ; le markdown reste la
            # source de découpage. Repli sur le texte reconstruit du JSON sinon.
            page_source_json: Optional[Dict[str, Any]] = None
            if result.get("json_url"):
                json_bytes = await mineru_service.download_result(result["json_url"])
                try:
                    page_source_json = json.loads(json_bytes.decode("utf-8", errors="ignore"))
                except json.JSONDecodeError:
                    page_source_json = None

            markdown_text = ""
            if result.get("md_url"):
                md_bytes = await mineru_service.download_result(result["md_url"])
                markdown_text = md_bytes.decode("utf-8", errors="ignore")
            elif page_source_json is not None:
                markdown_text = extract_text_from_mineru_json(page_source_json)

            if markdown_text:
                created_documents = create_or_update_jo_documents_from_markdown(
                    db,
                    journal,
                    markdown_text,
                    curation_status="review",
                    page_source_json=page_source_json,
                )
                journal.transcription_status = "completed" if created_documents else "pending"
            else:
                journal.transcription_status = "failed"
            db.commit()

            await notify_clients("notification", json.dumps({"message": "MinerU a terminé le traitement du JO.", "type": "success"}))
        else:
            journal.transcription_status = "failed"
            db.commit()
            await notify_clients("notification", json.dumps({"message": f"Échec de l'extraction MinerU pour le JO.", "type": "error"}))
            
    except Exception as exc:
        db.rollback()
        journal.transcription_status = "failed"
        db.commit()
        await notify_clients("notification", json.dumps({"message": f"Erreur lors de l'extraction MinerU du JO: {str(exc)}", "type": "error"}))
    finally:
        if os.path.exists(pdf_path):
            os.remove(pdf_path)
        db.close()
        await notify_clients("update", "{}")


async def _collapse_chunks(files: List[UploadFile], kind: str) -> Tuple[bytes, str, List[str]]:
    """Réduit une liste de fichiers (md ou json) en un seul artefact.

    Un seul fichier → ses octets tels quels (comportement historique). Plusieurs →
    fusion via `chunk_merger` (tri par nom `chunk_{début}_a_{fin}`, `page_idx`
    ré-offsetté pour le JSON). Renvoie (octets, nom_artefact, avertissements).
    """
    if not files:
        return b"", f"source.{kind}", []
    if len(files) == 1:
        data = await read_upload_capped(files[0], label=f"fichier .{kind}")
        return data, (files[0].filename or f"source.{kind}"), []

    items: List[Tuple[str, bytes]] = [
        (f.filename or "", await read_upload_capped(f, label=f"fichier .{kind}")) for f in files
    ]
    if kind == "md":
        merged_text, warnings = merge_markdown_chunks(items)
        return merged_text.encode("utf-8"), "merged.md", warnings
    merged_json, warnings = merge_json_chunks(items)
    return json.dumps(merged_json, ensure_ascii=False).encode("utf-8"), "merged.json", warnings


@app.post("/api/v1/documents/upload", tags=["documents"])
async def upload_document(
    background_tasks: BackgroundTasks,
    titre_officiel: str = Form(...),
    document_role: str = Form("STOCK"),
    stock_code: Optional[str] = Form(None),
    document_key: Optional[str] = Form(None),
    type_code: Optional[str] = Form(None),
    institution_sigle: Optional[str] = Form(None),
    reference_nor: Optional[str] = Form(None),
    date_signature: Optional[str] = Form(None),
    date_publication: Optional[str] = Form(None),
    date_entree_vigueur: Optional[str] = Form(None),
    legal_scope: Optional[str] = Form(None),
    curation_status: str = Form("draft"),
    pdf_file: UploadFile = File(...),
    md_file: List[UploadFile] = File(default=[]),
    json_file: List[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    _user: AuthenticatedUser = Depends(require_editor),
):
    """Depose un PDF et ses extractions optionnelles dans MinIO puis en base.

    `md_file` / `json_file` acceptent un ou plusieurs fichiers. Plusieurs morceaux
    (PDF volumineux découpé avant MinerU, ex. Code Bleu OHADA) sont fusionnés en
    un artefact unique à pagination globale via `chunk_merger`. Un seul fichier =
    comportement historique inchangé.
    """

    normalized_role = document_role.upper()
    normalized_stock_code = sanitize_path_component(stock_code) if stock_code else None

    if normalized_role not in {"STOCK", "FLUX"}:
        return JSONResponse(status_code=422, content={"message": "Le role doit etre STOCK ou FLUX."})

    # Le statut de curation d'un upload ne peut être que draft/review : accepter
    # « published » ici contournerait le garde-fou d'anomalies côté Laravel (S5).
    if curation_status not in {"draft", "review"}:
        return JSONResponse(status_code=422, content={"message": "Le statut de curation doit etre draft ou review."})

    if normalized_role == "STOCK" and not normalized_stock_code:
        return JSONResponse(status_code=422, content={"message": "Le champ code du stock est obligatoire pour un document de type STOCK."})

    # `legal_documents.stock_code` est un varchar(100) : un slug dérivé d'un titre
    # long déborde et fait échouer l'INSERT (StringDataRightTruncation → 500). On
    # renvoie ici un 422 explicite plutôt que de laisser planter la requête.
    if normalized_stock_code and len(normalized_stock_code) > 100:
        return JSONResponse(
            status_code=422,
            content={"message": "Le code du stock ne doit pas dépasser 100 caractères. Raccourcissez-le (il est dérivé du titre)."},
        )

    resolved_document_key = document_key or build_document_key(normalized_role, normalized_stock_code, titre_officiel)
    existing_document = db.query(LegalDocument).filter(LegalDocument.document_key == resolved_document_key).first()
    if existing_document:
        return JSONResponse(
            status_code=409,
            content={"message": "Un document avec cette cle existe deja.", "document_id": str(existing_document.id)},
        )

    # Le PDF est streamé par morceaux vers un fichier temporaire : plafond
    # MAX_UPLOAD_MB (413) + validation du magic « %PDF- » (422), sans jamais
    # charger le fichier entier en mémoire (audit S1).
    upload = await stream_upload_to_tmp(pdf_file, STORAGE_TMP_DIR)
    # Le fichier temporaire n'est conservé que s'il est confié à la tâche de
    # fond MinerU (qui le supprime elle-même en finally) ; sinon nettoyé ici.
    temp_owned_by_background = False
    try:
        if upload.size == 0:
            return JSONResponse(status_code=422, content={"message": "Le fichier PDF est vide."})

        doc_id = uuid.uuid4()
        pdf_checksum = upload.sha256
        pdf_object_key = build_object_key(
            normalized_role,
            normalized_stock_code,
            doc_id,
            "source/pdf",
            pdf_file.filename or "document.pdf",
        )
        # fput_object streame depuis le fichier temporaire (pas de bytes en mémoire).
        pdf_s3_path = minio_service.upload_file(pdf_object_key, upload.path, "application/pdf")
        if not pdf_s3_path:
            return JSONResponse(status_code=500, content={"message": "Echec de stockage du PDF dans MinIO."})

        resolved_type_code = resolve_document_type_code(db, type_code, normalized_role, title=titre_officiel)
        institution_id = resolve_institution_id(db, institution_sigle)

        new_doc = LegalDocument(
            id=doc_id,
            type_code=resolved_type_code,
            institution_id=institution_id,
            document_key=resolved_document_key,
            stock_code=normalized_stock_code,
            titre_officiel=titre_officiel,
            reference_nor=reference_nor,
            date_signature=parse_optional_date(date_signature),
            date_publication=parse_optional_date(date_publication),
            date_entree_vigueur=parse_optional_date(date_entree_vigueur),
            document_role=normalized_role,
            consolidation_as_of=datetime.datetime.utcnow().date() if normalized_role == "STOCK" else None,
            statut="vigueur",
            legal_scope=resolve_legal_scope(titre_officiel, legal_scope),
            curation_status=curation_status,
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
            payload_size=upload.size,
            checksum_sha256=pdf_checksum,
            description="PDF source depose depuis l'interface web",
        )

        db.add(new_doc)
        db.add(pdf_media)
        db.flush()

        md_files = [f for f in (md_file or []) if f is not None]
        json_files = [f for f in (json_file or []) if f is not None]

        if not (md_files or json_files):
            # Aucun artefact fourni : MinerU prend le relai depuis le fichier
            # temporaire déjà bufferisé (la tâche de fond le supprime en finally).
            db.commit()
            temp_owned_by_background = True
            background_tasks.add_task(
                process_mineru_extraction,
                doc_id,
                pdf_media.id,
                upload.path,
                normalized_role,
                normalized_stock_code,
            )
            return JSONResponse(content={"message": "Document depose avec succes", "document_id": str(doc_id)})

        provided_formats = []
        merge_info: Dict[str, Any] = {}
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

        if md_files:
            md_bytes, md_source_name, md_warnings = await _collapse_chunks(md_files, "md")
            if len(md_files) > 1:
                merge_info["md_chunks"] = [f.filename for f in md_files]
            if md_warnings:
                merge_info["md_warnings"] = md_warnings
            if md_bytes:
                md_object_key = build_object_key(
                    normalized_role,
                    normalized_stock_code,
                    doc_id,
                    "extractions/markdown",
                    md_source_name,
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
                    original_filename=md_source_name,
                    mime_type="text/markdown",
                    file_category="EXTRACTION_MARKDOWN",
                    payload_size=len(md_bytes),
                    checksum_sha256=compute_sha256(md_bytes),
                    description="Markdown fusionné depuis plusieurs morceaux" if len(md_files) > 1 else "Markdown fourni manuellement a l'upload",
                )
                db.add(media_md)
                db.flush()
                run.markdown_media_file_id = media_md.id
                provided_formats.append("md")
                has_markdown = True

        if json_files:
            json_bytes, json_source_name, json_warnings = await _collapse_chunks(json_files, "json")
            if len(json_files) > 1:
                merge_info["json_chunks"] = [f.filename for f in json_files]
            if json_warnings:
                merge_info["json_warnings"] = json_warnings
            if json_bytes:
                json_object_key = build_object_key(
                    normalized_role,
                    normalized_stock_code,
                    doc_id,
                    "extractions/json",
                    json_source_name,
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
                    original_filename=json_source_name,
                    mime_type="application/json",
                    file_category="EXTRACTION_JSON",
                    payload_size=len(json_bytes),
                    checksum_sha256=compute_sha256(json_bytes),
                    description="JSON fusionné depuis plusieurs morceaux" if len(json_files) > 1 else "JSON fourni manuellement a l'upload",
                )
                db.add(media_json)
                db.flush()
                run.json_media_file_id = media_json.id
                provided_formats.append("json")
                has_json = True

        run.status = "succeeded" if has_markdown and has_json else "partial"
        run.finished_at = datetime.datetime.utcnow()
        run.meta = {**(run.meta or {}), "provided_formats": provided_formats}
        if merge_info:
            run.meta = {**run.meta, "merge": merge_info}
        set_document_status(new_doc, has_markdown, has_json)
        merge_metadata(new_doc, {"latest_extraction_run_id": str(run.id)})
        db.commit()

        asyncio.create_task(notify_clients())

        return JSONResponse(content={"message": "Document depose avec succes", "document_id": str(doc_id)})
    finally:
        if not temp_owned_by_background and os.path.exists(upload.path):
            os.remove(upload.path)


@app.post("/api/v1/official-journals/upload", tags=["official-journals"])
async def upload_official_journal(
    background_tasks: BackgroundTasks,
    title: str = Form(...),
    publication_date: str = Form(...),
    number: Optional[str] = Form(None),
    is_published: bool = Form(True),
    documents_curation_status: str = Form("review"),
    pdf_file: UploadFile = File(...),
    md_file: Optional[UploadFile] = File(None),
    json_file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    _user: AuthenticatedUser = Depends(require_editor),
):
    """Crée un Journal Officiel puis charge les actes qu’il contient en `legal_documents`."""

    upload = None
    temp_owned_by_background = False
    try:
        # Le statut de curation des actes créés ne peut être que draft/review :
        # accepter « published » contournerait le garde-fou d'anomalies (S5).
        if documents_curation_status not in {"draft", "review"}:
            return JSONResponse(status_code=422, content={"message": "Le statut de curation doit etre draft ou review."})

        resolved_publication_date = parse_optional_date(publication_date)
        if not resolved_publication_date:
            return JSONResponse(status_code=422, content={"message": "La date de publication est obligatoire au format YYYY-MM-DD."})

        existing_journal_query = db.query(OfficialJournal).filter(OfficialJournal.publication_date == resolved_publication_date)
        if number:
            existing_journal_query = existing_journal_query.filter(OfficialJournal.number == number)
        else:
            existing_journal_query = existing_journal_query.filter(OfficialJournal.title == title)

        existing_journal = existing_journal_query.first()
        journal = existing_journal
        journal_created = False

        # Le PDF est streamé UNE SEULE fois vers un fichier temporaire (plafond
        # MAX_UPLOAD_MB + magic « %PDF- ») : il sert à la fois au stockage MinIO
        # (journal nouveau) et à la relance MinerU éventuelle — l'ancienne double
        # lecture en mémoire disparaît (audit S1).
        upload = await stream_upload_to_tmp(pdf_file, STORAGE_TMP_DIR, filename_prefix="jo_")

        if not journal:
            if upload.size == 0:
                return JSONResponse(status_code=422, content={"message": "Le fichier PDF du JO est vide."})

            journal = OfficialJournal(
                id=uuid.uuid4(),
                title=title,
                publication_date=resolved_publication_date,
                file_path="",
                transcription_status="pending",
                is_published=is_published,
                number=number,
            )

            pdf_object_key = build_journal_object_key(journal, pdf_file.filename or "journal-officiel.pdf")
            pdf_s3_path = minio_service.upload_file(pdf_object_key, upload.path, "application/pdf")
            if not pdf_s3_path:
                return JSONResponse(status_code=500, content={"message": "Echec de stockage du PDF du JO dans MinIO."})

            journal.file_path = pdf_s3_path
            db.add(journal)
            db.flush()
            journal_created = True

        markdown_text = ""
        if md_file:
            md_bytes = await read_upload_capped(md_file, label="fichier .md")
            if md_bytes:
                markdown_text = md_bytes.decode("utf-8", errors="ignore")

        # Le JSON sert de SOURCE DE PAGES (citabilité), pas de découpage. S'il n'y
        # a pas de markdown, on s'en sert aussi comme repli pour reconstruire le texte.
        page_source_json: Optional[Dict[str, Any]] = None
        if json_file:
            json_bytes = await read_upload_capped(json_file, label="fichier .json")
            if json_bytes:
                try:
                    page_source_json = json.loads(json_bytes.decode("utf-8", errors="ignore"))
                except json.JSONDecodeError:
                    page_source_json = None

        if not markdown_text and page_source_json is not None:
            markdown_text = extract_text_from_mineru_json(page_source_json)

        created_documents: List[LegalDocument] = []
        if markdown_text:
            created_documents = create_or_update_jo_documents_from_markdown(
                db,
                journal,
                markdown_text,
                curation_status=documents_curation_status,
                page_source_json=page_source_json,
            )
            journal.transcription_status = "completed" if created_documents else "pending"
        elif journal_created or journal.transcription_status in (None, "pending", "failed"):
            # Pas d'artefact fourni : on (re)lance MinerU, y compris pour un JO
            # existant dont la transcription n'a jamais abouti.
            if upload.size == 0:
                return JSONResponse(status_code=422, content={"message": "Le fichier PDF du JO est vide."})

            journal.transcription_status = "queued"
            # La tâche de fond supprime le fichier temporaire elle-même (finally).
            temp_owned_by_background = True
            background_tasks.add_task(
                process_mineru_journal_extraction,
                journal.id,
                upload.path
            )

        db.commit()

        return JSONResponse(
            content={
                "message": "Journal Officiel traité avec succès" if not journal_created else "Journal Officiel depose avec succes",
                "official_journal_id": str(journal.id),
                "created_documents_count": len(created_documents),
                "created_document_ids": [str(document.id) for document in created_documents],
                "created": journal_created,
            }
        )
    except HTTPException:
        # Erreurs métier explicites (413/422 du streaming, dates invalides…) :
        # on les laisse remonter telles quelles au lieu de les masquer en 500.
        db.rollback()
        raise
    except Exception:
        db.rollback()
        # Traceback réservée aux logs serveur : aucun détail interne ne doit
        # sortir dans la réponse (audit P0.8/S3).
        logger.exception("ERREUR 500 dans upload_official_journal")
        return JSONResponse(
            status_code=500,
            content={"message": "Erreur interne du serveur."}
        )
    finally:
        if upload is not None and not temp_owned_by_background and os.path.exists(upload.path):
            os.remove(upload.path)


@app.get("/api/v1/stream", tags=["stream"])
async def stream_events(_user: AuthenticatedUser = Depends(require_editor)):
    """Expose un flux SSE pour recharger le tableau en temps reel, avec heartbeat pour eviter les timeouts.

    Sécurité : réservé aux éditeurs/admins (``require_editor`` → 401 si token
    absent/invalide, 403 sinon). Le flux diffuse des métadonnées d'ingestion
    (titres, ids, statut) qui ne doivent pas fuiter anonymement. Le front le
    consomme via ``fetch``+``ReadableStream`` afin de porter l'en-tête standard
    ``Authorization: Bearer`` (impossible avec ``EventSource`` natif).
    """

    queue = asyncio.Queue()
    event_queues.append(queue)

    async def event_generator():
        try:
            while True:
                try:
                    event_name, payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
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

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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

        await notify_clients("notification", json.dumps({"message": f"Démarrage du parsing structurel...", "type": "info"}))
        await notify_clients("update", "{}")

        # Téléchargement depuis MinIO
        file_bytes = minio_service.get_file_bytes(media.object_key)
        if not file_bytes:
            raise ValueError(f"Impossible de télécharger {media.object_key} depuis MinIO")

        text_content = ""
        if format_type == "md":
            text_content = file_bytes.decode("utf-8", errors="ignore")
        elif format_type == "json":
            data = json.loads(file_bytes.decode("utf-8", errors="ignore"))
            text_content = extract_text_from_mineru_json(data, with_page_markers=True)

        # Indicateur de qualité OCR (lisibilité) calculé sur le texte source AVANT
        # réparation/découpage. Persisté dans extraction_runs.meta pour audit et
        # affichage éditeur ; sert de base au flag non bloquant plus bas.
        ocr_quality = compute_ocr_quality(text_content)

        # Parse le contenu textuel
        parser = LegalDocumentParser(text_content=text_content)
        hierarchy = parser.parse_hierarchy()

        # Fallback pour les documents sans structure détectable (circulaire,
        # instruction, discours, proclamation, décision courte : aucun
        # « Article »/« Titre »/« Fait à … le N »). Tout le texte devient un
        # article « Unique » / « Texte intégral », tamponné avec la première page
        # repérée pour rester citable. Symétrique du chemin Journal Officiel
        # (cf. _ingest_journal_acts) : sans lui, le parsing « réussit » mais
        # n'ingère rien, et le document atterrit en review vide, impubliable.
        if not hierarchy and text_content.strip():
            first_marker = re.search(r"\[\[MIBEKO_PAGE:(\d+)\]\]", text_content)
            clean_text = "\n".join(
                ln for ln in text_content.splitlines()
                if not re.match(r"^\s*\[\[MIBEKO_PAGE:\d+\]\]\s*$", ln)
            ).strip()
            hierarchy = [{
                "type": "ARTICLE",
                "number": "Unique",
                "title": "Texte intégral",
                "content": clean_text,
                "page": int(first_marker.group(1)) if first_marker else None,
                "children": [],
            }]

        if hierarchy and document_has_curated_content(db, document.id):
            # Replay NON-DESTRUCTIF : le document porte de la curation humaine.
            # On parque la proposition dans le run (staging) au lieu d'écraser le
            # live ; un éditeur arbitrera via le diff puis promeut ou rejette.
            run.status = "needs_review"
            run.finished_at = datetime.datetime.utcnow()
            # ocr_quality conservé dans le staging : le flag ne sera émis qu'à la
            # PROMOTION (le live n'est pas encore modifié), cf. promote_run.
            run.meta = {**(run.meta or {}), "staged": True, "proposed_hierarchy": hierarchy, "ocr_quality": ocr_quality}
            document.curation_status = "review"
            db.commit()

            await notify_clients("notification", json.dumps({
                "message": "Nouveau parsing prêt : proposition en attente d'arbitrage (le contenu existant n'a pas été modifié).",
                "type": "info",
            }))
        else:
            if hierarchy:
                ingest_hierarchy(db, document, hierarchy, run_id=run.id, media_id=media.id, validation_status="pending")
                # Garde-fou qualité OCR (non bloquant) : sous le seuil, un flag
                # 'warning' remonte à la curation Laravel (l'éditeur garde
                # « Publier quand même »). N'écrase pas les flags de séquence.
                flag_low_ocr_quality(db, document.id, ocr_quality, run_id=run.id)

            run.status = "succeeded"
            run.finished_at = datetime.datetime.utcnow()
            run.meta = {**(run.meta or {}), "ocr_quality": ocr_quality}
            # Le document entre dans la file de validation : un éditeur doit
            # contrôler le parsing avant publication (curation Laravel).
            document.curation_status = "review"
            db.commit()

            await notify_clients("notification", json.dumps({"message": f"Parsing terminé avec succès.", "type": "success"}))

    except Exception as exc:
        db.rollback()
        run.status = "failed"
        run.finished_at = datetime.datetime.utcnow()
        run.meta = {**(run.meta or {}), "error": str(exc)}
        db.commit()

        await notify_clients("notification", json.dumps({"message": f"Erreur lors du parsing: {str(exc)}", "type": "error"}))
    finally:
        db.close()
        await notify_clients("update", "{}")

@app.post("/api/v1/documents/{doc_id}/reprocess", tags=["documents"])
async def reprocess_document(
    doc_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _user: AuthenticatedUser = Depends(require_editor),
):
    """Relance l'extraction MinerU d'un document depuis son PDF source stocké dans MinIO."""

    document = db.query(LegalDocument).filter(LegalDocument.id == doc_id, LegalDocument.deleted_at.is_(None)).first()
    if not document:
        return JSONResponse(status_code=404, content={"message": "Document non trouve"})

    pdf_media = next((f for f in document.files if f.file_category == "SOURCE_PDF"), None)
    if not pdf_media:
        return JSONResponse(status_code=400, content={"message": "Aucun PDF source pour ce document."})

    pdf_bytes = minio_service.get_file_bytes(pdf_media.object_key)
    if not pdf_bytes:
        return JSONResponse(status_code=500, content={"message": "Impossible de relire le PDF source depuis MinIO."})

    os.makedirs(STORAGE_TMP_DIR, exist_ok=True)
    # original_filename vient du client à l'upload : on l'assainit avant de le
    # réutiliser dans un chemin local (audit S8).
    safe_pdf_name = sanitize_filename(pdf_media.original_filename)
    temp_pdf_path = os.path.join(STORAGE_TMP_DIR, f"{uuid.uuid4()}_{safe_pdf_name}")
    with open(temp_pdf_path, "wb") as buffer:
        buffer.write(pdf_bytes)

    document.extraction_status = "processing"
    db.commit()

    background_tasks.add_task(
        process_mineru_extraction,
        document.id,
        pdf_media.id,
        temp_pdf_path,
        document.document_role or "FLUX",
        document.stock_code,
    )

    return {"message": "Relance de l'extraction MinerU en arrière-plan.", "document_id": str(document.id)}


@app.post("/api/v1/documents/{doc_id}/parse", tags=["documents"])
def parse_document(doc_id: str, background_tasks: BackgroundTasks, source_format: str = Form(...), db: Session = Depends(get_db), _user: AuthenticatedUser = Depends(require_editor)):
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


# ---------------------------------------------------------------------------
# Arbitrage d'un retraitement non-destructif (staging) : diff / promote / discard
# ---------------------------------------------------------------------------

def _staged_run(db: Session, doc_id: str, run_id: str) -> Optional[ExtractionRun]:
    """Run du document portant une proposition en attente, ou None."""
    return (
        db.query(ExtractionRun)
        .filter(ExtractionRun.id == run_id, ExtractionRun.document_id == doc_id)
        .first()
    )


@app.get("/api/v1/documents/{doc_id}/runs/{run_id}/diff", tags=["documents"])
def get_run_diff(doc_id: str, run_id: str, db: Session = Depends(get_db), _user: AuthenticatedUser = Depends(require_editor)):
    """Compare la proposition d'un run en attente (staging) au contenu live."""
    run = _staged_run(db, doc_id, run_id)
    if not run:
        return JSONResponse(status_code=404, content={"message": "Run introuvable pour ce document."})

    proposed_hierarchy = (run.meta or {}).get("proposed_hierarchy")
    if not proposed_hierarchy:
        return JSONResponse(status_code=400, content={"message": "Ce run ne porte aucune proposition en attente (staging)."})

    proposed = flatten_hierarchy_articles(proposed_hierarchy)
    live = fetch_live_articles(db, run.document_id)
    return diff_articles(proposed, live)


@app.post("/api/v1/documents/{doc_id}/runs/{run_id}/promote", tags=["documents"])
def promote_run(doc_id: str, run_id: str, db: Session = Depends(get_db), _user: AuthenticatedUser = Depends(require_editor)):
    """Applique la proposition d'un run (staging) au live — action humaine explicite."""
    document = db.query(LegalDocument).filter(LegalDocument.id == doc_id, LegalDocument.deleted_at.is_(None)).first()
    if not document:
        return JSONResponse(status_code=404, content={"message": "Document non trouve."})

    run = _staged_run(db, str(document.id), run_id)
    if not run:
        return JSONResponse(status_code=404, content={"message": "Run introuvable pour ce document."})

    proposed_hierarchy = (run.meta or {}).get("proposed_hierarchy")
    if not proposed_hierarchy:
        return JSONResponse(status_code=400, content={"message": "Ce run ne porte aucune proposition a promouvoir."})

    # C'est ICI, et seulement ici, que l'écrasement du contenu a lieu — sur
    # décision humaine explicite (et non plus automatiquement à chaque parsing).
    ingest_hierarchy(db, document, proposed_hierarchy, run_id=run.id, media_id=run.source_media_file_id, validation_status="pending")

    # Le flag qualité OCR n'est émis qu'au moment où le contenu devient live (ici),
    # à partir du score calculé et stocké au parsing. Non bloquant ('warning').
    staged_quality = (run.meta or {}).get("ocr_quality")
    if isinstance(staged_quality, dict):
        flag_low_ocr_quality(db, document.id, staged_quality, run_id=run.id)

    meta = dict(run.meta or {})
    meta.pop("proposed_hierarchy", None)
    meta.update({"staged": False, "promoted_at": datetime.datetime.utcnow().isoformat()})
    run.meta = meta
    run.status = "succeeded"
    document.curation_status = "review"
    db.commit()

    return {"message": "Proposition promue : le contenu a ete remplace et repasse en file de validation.", "document_id": str(document.id)}


@app.post("/api/v1/documents/{doc_id}/runs/{run_id}/discard", tags=["documents"])
def discard_run(doc_id: str, run_id: str, db: Session = Depends(get_db), _user: AuthenticatedUser = Depends(require_editor)):
    """Rejette la proposition d'un run (staging) : le live reste intact."""
    run = _staged_run(db, doc_id, run_id)
    if not run:
        return JSONResponse(status_code=404, content={"message": "Run introuvable pour ce document."})

    meta = dict(run.meta or {})
    meta.pop("proposed_hierarchy", None)
    meta.update({"staged": False, "discarded_at": datetime.datetime.utcnow().isoformat()})
    run.meta = meta
    run.status = "discarded"
    db.commit()

    return {"message": "Proposition rejetee : aucun changement applique au contenu existant."}
