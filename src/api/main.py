import asyncio
import datetime
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.api.routers import documents as documents_router
from src.api.auth import AuthenticatedUser, require_editor
from src.api.config import EXPOSE_API_DOCS, INGESTION_CONSOLE_ENABLED, IS_PRODUCTION, SERVICE_VERSION
from src.api.schemas import GlobalStatsOut, HealthOut
from src.api.upload_utils import BodySizeLimitMiddleware, read_upload_capped, sanitize_filename, stream_upload_to_tmp
from src.acquisition.config import data_dir, manifests_dir, sources_dir
from src.acquisition.manifest import Manifest, ManifestEntry, known_checksums, utc_now_iso
from src.db.database import SessionLocal, get_db, init_db
from src.db.schema_check import check_schema
from src.db.models import Article, ArticleVersion, CurationFlag, ExtractionRun, IngestionJob, IngestionProvenance, Institution, LegalDocument, MediaFile, OfficialJournal, StructureNode
from src.services.mineru_service import mineru_service
from src.services.minio_service import minio_service
from src.services.pdf_pages import compter_pages_pdf
from src.extractor.parser import LegalDocumentParser
from src.extractor.chunk_merger import merge_json_chunks, merge_markdown_chunks
# Extrait de ce module (mibeko-python#23) pour que structurer.py — et bientôt
# le worker de la file durable — puisse construire clés/hiérarchie sans
# importer l'app FastAPI ni déclencher la connexion MinIO à l'import
# (mibeko-python/CLAUDE.md, pièges connus). Réimporté ici tel quel : mêmes
# noms, mêmes appelants, un seul et même calcul.
from src.services.ingestion import (
    analyze_article_sequence,
    assign_dfs_order,
    build_document_key,
    build_domino_root,
    build_object_key,
    clear_document_structure,
    count_series_restarts,
    derive_numero_origine,
    find_embedded_series_runs,
    find_missing_runs,
    flag_article_sequence_anomalies,
    flag_table_anomalies,
    ingest_hierarchy,
    merge_metadata,
    ordinal_from_raw_number,
    sanitize_path_component,
)

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


def compute_sha256(payload: bytes) -> str:
    """Calcule l'empreinte SHA-256 d'un contenu binaire."""

    return hashlib.sha256(payload).hexdigest()


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
    page_count: Optional[int] = None,
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
        page_count=page_count,
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
    r"^(?:\[\[MIBEKO_PAGE:\d+\]\]|\d{1,5}\s*$|Journal\s+off[ﬁi]ciel\s+de\s+la\s+R[ÉE]publique|N°\s*\d+[\-–]\d{4}\s*$)",
    flags=re.IGNORECASE,
)

# --- Garde-fou d'ÉTAT, complémentaire du garde-fou de plausibilité ---
# `_looks_like_real_act_title` juge une ligne isolée : il accepte tout ce qui
# porte un « n° » ou une date. Or le bloc de visas d'un acte n'est QUE cela —
# une énumération de textes cités, chacun avec son numéro et sa date. Quand la
# mise en page du JO coupe un visa, la ligne suivante commence donc par
# « Loi n° 52.130 du 6 février 1952 relative à… » et franchit tous les
# contrôles (cas réel : congo-jo-1958-01, un faux acte issu du visa du décret
# n° 46.2374). Aucune propriété de la ligne seule ne permet de trancher : il
# faut savoir CE QUI précède.
#
# Un drapeau « bloc de visas ouvert » (armé par VU/CONSIDÉRANT, désarmé par le
# verbe du dispositif) a été écrit puis RETIRÉ le 10/08/2026, mesures à
# l'appui : les « actes en abrégé » (nominations, agréments, autorisations
# minières) n'ont ni verbe de dispositif isolé ni « ARTICLE PREMIER », donc
# aucune sortie ne se déclenche et le drapeau étouffe tout le reste du JO —
# congo-jo-2013-21 tombait de 14 actes à 2, douze arrêtés réels fondus dans
# leur voisin. La formule de légalisation « Vu pour la légalisation de la
# signature », qui figure en FIN d'acte, l'armait de surcroît juste avant le
# titre suivant. La continuité de phrase ci-dessous suffit au cas fondateur.

# Ponctuation qui clôt une phrase. La virgule en est délibérément absente :
# c'est la ponctuation NORMALE de fin de visa, donc le signe le plus fréquent
# qu'une énumération continue.
_PONCTUATION_FORTE_REGEX = re.compile(r"[.!?:;»”\"…]['’\"”\s]*$")

# Mots grammaticaux qui appellent obligatoirement un complément : une phrase
# française ne peut pas s'arrêter dessus. Une ligne qui finit ainsi est donc
# une phrase EN COURS, et la ligne d'après en est la suite — pas un titre.
# En minuscules uniquement, volontairement : la casse distingue le déterminant
# « la » de l'initiale « L. » et le « a » verbal du « A » de « S.A » /
# « A.E.F. ». Les visas en capitales échappent à ce test, mais le drapeau de
# bloc de visas les couvre — c'est l'intérêt d'avoir deux garde-fous.
_MOTS_APPELANT_UNE_SUITE = (
    "le|la|les|l|un|une|des|du|de|d|au|aux|ce|cet|cette|ces|son|sa|ses|leur|leurs|"
    "notre|nos|votre|vos|mon|ma|mes|à|a|en|par|pour|sur|sous|dans|avec|sans|vers|"
    "chez|entre|depuis|selon|et|ou|ni|mais|donc|or|car|que|qu|qui|dont|où|comme|si|"
    "ledit|ladite|lesdits|lesdites"
)
_PHRASE_EN_SUSPENS_REGEX = re.compile(
    r"(?:"
    rf"\b(?:{_MOTS_APPELANT_UNE_SUITE})\s*['’]?"  # « … et la », « … de l’ »
    r"|[A-Za-zÀ-ÿ]-"  # césure typographique : « … portant créa- »
    r")\s*$"
)

# Un titre de section markdown (« # DELIBERATION N° 112/58 ») est un bloc à
# part entière produit par MinerU : il ne peut pas être le prolongement de la
# phrase précédente, quoi qu'en dise la ligne d'avant.
_TITRE_MARKDOWN_REGEX = re.compile(r"^\s*#{1,6}\s")


def _coupure_autorisee_par_la_continuite(lignes: List[str], index: int) -> bool:
    """Vrai si la ligne `index` a le droit d'ouvrir un acte au vu de ce qui la précède.

    Une phrase qui continue ne peut pas être suivie d'un nouvel acte : c'est ce
    que le faux titre trahit toujours, puisque le fragment de visa qu'on prend
    pour un intitulé est précédé d'une ligne interrompue en plein milieu
    (« … Territoriales en A.E.F. et la » / « Loi n° 52.130 du 6 février 1952 »).

    La formulation littérale — « refuser la coupure si la ligne précédente ne
    finit pas par une ponctuation forte » — a été mesurée sur les 1 436
    markdowns de data/pipeline/md/ : elle fait tomber le corpus de 54 249 à
    37 195 actes. Le JO sépare en effet ses actes par des lignes de rubrique
    sans ponctuation (« PARTIE OFFICIELLE », « - LOI - », un folio, la fin
    tabulaire d'un acte de pension), et surtout les « actes en abrégé » se
    succèdent sans phrase de clôture. Le signal utile n'est donc pas l'absence
    de point mais la PRÉSENCE d'une marque de phrase en suspens (mot outil ou
    césure) : sur un échantillon de 131 de ces markdowns, à suppression égale
    des faux titres, elle coûte 64 actes au lieu de 1 497.

    Deux dérogations, chacune motivée par une borne au moins aussi forte qu'un
    point : le début du document, et un saut de page (marqueur de page, folio,
    en-tête répétée du JO), qui sépare bel et bien deux blocs.
    """
    i = index - 1
    while i >= 0:
        precedente = lignes[i].strip()
        if not precedente:
            i -= 1
            continue
        if _SAUT_DE_PAGE_REGEX.match(precedente):
            return True
        precedente = re.sub(r"^[#>\s]*[*_]{0,3}\s*", "", precedente)
        precedente = re.sub(r"\s*[*_]{1,3}$", "", precedente)
        if _PONCTUATION_FORTE_REGEX.search(precedente):
            return True
        return not _PHRASE_EN_SUSPENS_REGEX.search(precedente)
    return True


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
    # Renvoi de page nu en fin de ligne de sommaire (« … de commerce 727 »),
    # avec ou sans points de conduite. Ne sert QU'en conjonction avec un
    # contenu vide (voir `flush`) : seul, il désignerait aussi de vrais actes.
    _FIN_NUMERO_DE_PAGE_REGEX = re.compile(r"[\s.]\d{1,4}\.?$")

    def flush() -> None:
        if not current_title:
            return
        contenu = "\n".join(current_lines)
        # Entrée de SOMMAIRE sans parenthèses : « … aux chambres de commerce
        # 727 ». Le sommaire du Journal officiel congolais aligne un titre par
        # ligne suivi du seul numéro de page, hors de portée de
        # `toc_entry_regex` (qui n'attrape que « (page 25) ») — chaque ligne
        # ouvrait donc un acte, aussitôt refermé par la ligne suivante. Constat
        # prod du 13/08/2026 : 25 des 27 documents `extraction_status='failed'`
        # sont nés là, tous à 0 article, alors que le texte réel vit dans
        # l'acte homonyme découpé plus loin dans le corps du journal.
        #
        # Les DEUX conditions sont nécessaires, mesurées sur les 60 JO de
        # `data/pipeline/md/` :
        #   · contenu vide seul ne suffit pas — un « acte en abrégé »
        #     (nomination d'une ligne) voit tout son texte absorbé par la
        #     continuation multiligne du titre, et reste un acte réel ;
        #   · numéro de page final seul ne suffit pas — 129 actes RÉELS en
        #     portent un (le dernier du sommaire, qui absorbe le corps).
        # Leur conjonction ne décrit que le sommaire : 40 des 43 actes vides
        # du corpus local, et zéro acte porteur de texte.
        if not contenu.strip() and _FIN_NUMERO_DE_PAGE_REGEX.search(current_title.strip()):
            return
        texts.append(
            {
                "titre": current_title,
                "contenu": contenu,
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
        # Garde-fou d'état, en ET avec le garde-fou de plausibilité ci-dessus :
        # une ligne ne peut pas ouvrir un acte si la phrase précédente reste
        # en suspens. C'est ce qui distingue un intitulé d'une citation coupée
        # par la mise en page (« … et la » / « Loi n° 52.130 du 6 février… »).
        if (
            is_act_start
            and not _TITRE_MARKDOWN_REGEX.match(raw_line)
            and not _coupure_autorisee_par_la_continuite(lines, index)
        ):
            is_act_start = False

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


# ---------------------------------------------------------------------------
# Rejouabilité non-destructive (staging) — un retraitement ne doit jamais
# écraser la curation humaine. La proposition est parquée dans extraction_runs.meta
# (JSONB) puis arbitrée (diff → promote/discard) par un éditeur.
# ---------------------------------------------------------------------------

LEAF_CONTENT_TYPES = {"ARTICLE", "PREAMBULE", "SIGNATURE", "TABLEAU", "DISPOSITION", "NOTE"}


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
    les feuilles porteuses de texte (articles, préambule, signature, tableaux,
    dispositions implicites et notes)."""
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


DEPOT_TYPE_SOURCES = {"journal_officiel", "code", "acte_uniforme", "acte"}


def _resolve_known_sha256(
    db: Session, sha256: str, manifests_directory: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Un PDF déjà connu, sous une des formes possibles (§ 3.6, identité n°1 :
    l'empreinte SHA-256 complète est la seule façon fiable de reconnaître un
    même fichier, jamais le titre). Trois sources, dans l'ordre où elles
    apparaissent dans le temps : déjà un document en base (`MediaFile`), déjà
    la provenance d'un job (`IngestionProvenance`, § 3.7 — dépôt ou veille,
    traité ou pas encore), ou déjà dans un manifeste hérité d'avant
    l'existence d'`IngestionProvenance`. `None` si le SHA est inédit.

    `manifests_directory` : injection pour les tests (`None` = `manifests_dir()`
    réel).
    """
    media = (
        db.query(MediaFile)
        .filter(MediaFile.checksum_sha256 == sha256, MediaFile.file_category == "SOURCE_PDF")
        .first()
    )
    if media is not None:
        return {"document_id": str(media.document_id), "manifest_id": None}

    provenance = db.query(IngestionProvenance).filter(IngestionProvenance.sha256 == sha256).first()
    if provenance is not None:
        return {"document_id": None, "manifest_id": provenance.manifest_id}

    manifest_id = known_checksums(manifests_directory or manifests_dir()).get(sha256)
    if manifest_id is not None:
        return {"document_id": None, "manifest_id": manifest_id}

    return None


@app.post("/api/v1/depots", tags=["depots"])
async def deposer_document(
    type_source: str = Form(...),
    titre: Optional[str] = Form(None),
    source_url: Optional[str] = Form(None),
    jo_numero: Optional[str] = Form(None),
    jo_date: Optional[str] = Form(None),
    pdf_file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _user: AuthenticatedUser = Depends(require_editor),
):
    """Chemin unique de dépôt (mibeko-python#23, § 3.2/L1 du plan « boîte de
    réception ») : remplace à terme `/documents/upload` et
    `/official-journals/upload`. Ne traite rien lui-même — fabrique une
    entrée de manifeste, sa provenance, et dépose un job `kind=depot` ; le
    worker (`python main.py worker`) fait le reste, exactement comme la
    veille (§ 3.4). Trois branches pour une seule question à l'éditeur
    (« qu'est-ce que c'est ? ») : Journal officiel (FLUX, découpé en actes),
    texte consolidé (STOCK), ou acte isolé (FLUX, nature déduite de l'en-tête,
    jamais inventée).
    """
    if type_source not in DEPOT_TYPE_SOURCES:
        return JSONResponse(
            status_code=422,
            content={"message": f"type_source doit être l'un de : {sorted(DEPOT_TYPE_SOURCES)}."},
        )

    resolved_jo_date = None
    if type_source == "journal_officiel":
        if not jo_numero or not jo_date:
            return JSONResponse(
                status_code=422,
                content={"message": "jo_numero et jo_date sont obligatoires pour un Journal officiel."},
            )
        resolved_jo_date = parse_optional_date(jo_date)
        if not resolved_jo_date:
            return JSONResponse(status_code=422, content={"message": "jo_date doit être au format YYYY-MM-DD."})

    target_data_dir = data_dir()
    upload = await stream_upload_to_tmp(pdf_file, STORAGE_TMP_DIR, filename_prefix="depot_")
    try:
        if upload.size == 0:
            return JSONResponse(status_code=422, content={"message": "Le fichier PDF est vide."})

        connu = _resolve_known_sha256(db, upload.sha256)
        if connu is not None:
            return JSONResponse(
                status_code=409,
                content={
                    "message": "Ce fichier a déjà été déposé (même empreinte SHA-256).",
                    **connu,
                    "actions": ["ouvrir_le_dossier", "reprendre_le_traitement", "nouvelle_extraction"],
                },
            )

        # data/sources/ est immuable (CLAUDE.md racine) : le PDF déplacé ici
        # ne bouge plus jamais — process_entry/structure_document le lisent
        # depuis le disque local, jamais depuis MinIO (qui n'entre en jeu
        # qu'une fois le document créé, dans structure_document lui-même).
        #
        # entry_id dérive UNIQUEMENT du SHA-256, jamais du nom de fichier : le
        # même contenu déposé sous deux titres différents doit produire le
        # MÊME manifest_id pour que la contrainte UNIQUE de
        # ingestion_provenances.manifest_id serve de verrou de course (revue
        # technique du 15/09 — un id qui mélangeait aussi le nom de fichier
        # laissait passer deux dépôts concurrents du même PDF sous deux noms,
        # chacun avec un manifest_id distinct, donc aucun conflit détecté).
        final_filename = f"{upload.sha256}.pdf"
        entry_id = f"depots/{upload.sha256}"
        final_path = sources_dir() / "depots" / final_filename
        final_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(upload.path, str(final_path))

        entry = ManifestEntry(
            id=entry_id,
            fichier=str(final_path.relative_to(target_data_dir)),
            sha256=upload.sha256,
            size_bytes=upload.size,
            type_source=type_source,
            source_url=source_url or None,
            fetched_at=utc_now_iso(),
            jo_annee=resolved_jo_date.year if resolved_jo_date else None,
            jo_numero=jo_numero if type_source == "journal_officiel" else None,
            jo_date=resolved_jo_date.isoformat() if resolved_jo_date else None,
            titre=titre or None,
        )
        entry.add_event(
            "depot_web", f"depot:{_user.email}",
            detail=f"{type_source} ({pdf_file.filename or 'sans nom'})",
        )

        # data/manifests/depots.jsonl n'a pas de verrou lecture-modification-
        # écriture au niveau de Manifest (limite connue, IngestionProvenance
        # § 3.7) : deux dépôts concurrents de fichiers DIFFÉRENTS peuvent
        # sinon perdre silencieusement l'un des deux, alors que leurs lignes
        # DB ont bien été commitées — un document « déposé avec succès » qui
        # ne serait jamais traité. Un verrou fichier dédié à CET endpoint
        # (seul appelant vraiment concurrent : les commandes batch/veille
        # restent un daemon séquentiel) referme cette fenêtre sans toucher à
        # la classe Manifest partagée par tous les autres appelants.
        depot_lock_path = manifests_dir() / ".depots.lock"
        depot_lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(depot_lock_path, "w") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
            try:
                manifest = Manifest(manifests_dir() / "depots.jsonl")
                manifest.upsert(entry)
                manifest.save()
            finally:
                fcntl.flock(lock_handle, fcntl.LOCK_UN)

        if not source_url:
            # Signalement non bloquant (§ 3.2) : visible dans « À vérifier »,
            # ne retarde jamais le traitement.
            db.add(CurationFlag(
                document_id=None,
                source="human",
                type_probleme="provenance_url_absente",
                severity="warning",
                description=f"Dépôt web sans URL officielle : « {pdf_file.filename or entry_id} ».",
            ))

        db.add(IngestionProvenance(
            manifest_id=entry.id,
            type_source=type_source,
            source_url=source_url or None,
            sha256=upload.sha256,
            fetched_at=datetime.datetime.utcnow(),
            evenements=[{"quand": utc_now_iso(), "quoi": "depot_web", "par": _user.email}],
        ))

        job = IngestionJob(kind=IngestionJob.KIND_DEPOT, manifest_id=entry.id, requested_by=_user.email)
        db.add(job)
        try:
            db.commit()
        except IntegrityError:
            # Deux dépôts simultanés du même PDF (incident (a) du plan
            # « boîte de réception ») : `ingestion_provenances.manifest_id`
            # est UNIQUE en base — entry.id étant dérivé du SHA-256, les deux
            # requêtes calculent le MÊME id et une seule gagne la course.
            # `manifest.save()` (ci-dessus, hors transaction) a pu écrire deux
            # fois la même entrée avant que l'une des deux ne perde ici — sans
            # conséquence, contenu identique. Le perdant n'écrit ni provenance
            # ni job en double : il renvoie le même 409 que le second dépôt
            # explicite d'un fichier déjà connu.
            db.rollback()
            connu = _resolve_known_sha256(db, upload.sha256) or {"document_id": None, "manifest_id": entry.id}
            return JSONResponse(
                status_code=409,
                content={
                    "message": "Ce fichier vient d'être déposé par une autre requête (même empreinte SHA-256).",
                    **connu,
                    "actions": ["ouvrir_le_dossier", "reprendre_le_traitement", "nouvelle_extraction"],
                },
            )

        return JSONResponse(
            status_code=201,
            content={
                "message": "Document déposé, en file de traitement.",
                "job_id": str(job.id),
                "manifest_id": entry.id,
            },
        )
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("ERREUR 500 dans deposer_document")
        return JSONResponse(status_code=500, content={"message": "Erreur interne du serveur."})
    finally:
        if os.path.exists(upload.path):
            os.remove(upload.path)


_INGESTION_JOB_STATUSES = {
    IngestionJob.STATUS_PENDING, IngestionJob.STATUS_RUNNING,
    IngestionJob.STATUS_FAILED, IngestionJob.STATUS_DONE,
}


@app.get("/api/v1/ingestion/jobs", tags=["depots"])
def list_ingestion_jobs(
    status: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    _user: AuthenticatedUser = Depends(require_editor),
):
    """Liste les travaux de la file `ingestion_jobs`, plus récents d'abord —
    alimente l'onglet « En cours » (L5a, front#43). Pas de SSE (§ 2.5 :
    `notify_clients` vit en mémoire du processus `api`, un worker séparé ne
    peut pas l'appeler) : le front interroge à intervalle régulier, comme
    ailleurs.
    """
    if status is not None and status not in _INGESTION_JOB_STATUSES:
        return JSONResponse(status_code=422, content={"message": f"status doit être l'un de : {sorted(_INGESTION_JOB_STATUSES)}."})

    query = db.query(IngestionJob)
    if status is not None:
        query = query.filter(IngestionJob.status == status)
    jobs = query.order_by(IngestionJob.created_at.desc()).limit(min(max(limit, 1), 200)).all()

    return {
        "jobs": [
            {
                "id": str(job.id),
                "kind": job.kind,
                "manifest_id": job.manifest_id,
                "step": job.step,
                "status": job.status,
                "attempts": job.attempts,
                "max_attempts": job.max_attempts,
                "error_class": job.error_class,
                "last_error": job.last_error,
                "result": job.result,
                "created_at": job.created_at.isoformat() if job.created_at else None,
                "updated_at": job.updated_at.isoformat() if job.updated_at else None,
            }
            for job in jobs
        ]
    }


@app.post("/api/v1/ingestion/jobs/{job_id}/relancer", tags=["depots"])
def relancer_ingestion_job(
    job_id: str,
    db: Session = Depends(get_db),
    _user: AuthenticatedUser = Depends(require_editor),
):
    """Relance manuellement un job `failed`. Seul point d'entrée qui repose un
    troisième appel LLM identique après un échec `information_manquante`
    (§ L1 du plan : « ne redemande jamais la même chose sans signal
    nouveau ») — un humain qui relance délibérément EST le signal nouveau.
    Remet le job à `pending` SANS toucher `step` : une étape déjà réussie
    (`result`) n'est jamais rejouée (§ 3.6, identité n°2). `attempts` est
    remis à zéro — sinon un job déjà au plafond échouerait de nouveau
    immédiatement, sans laisser sa chance à la relance demandée.
    """
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        return JSONResponse(status_code=422, content={"message": "job_id invalide."})

    job = db.query(IngestionJob).filter(IngestionJob.id == job_uuid).first()
    if job is None:
        return JSONResponse(status_code=404, content={"message": "Travail introuvable."})
    if job.status != IngestionJob.STATUS_FAILED:
        return JSONResponse(
            status_code=409,
            content={"message": f"Seul un travail « failed » peut être relancé (statut actuel : {job.status})."},
        )

    job.status = IngestionJob.STATUS_PENDING
    job.attempts = 0
    job.last_error = None
    job.error_class = None
    db.commit()

    return {"message": "Travail relancé.", "id": str(job.id), "step": job.step}


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
            page_count=compter_pages_pdf(upload.path),
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
