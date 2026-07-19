"""Utilitaires de réception d'uploads : plafond de taille, magic bytes, noms sûrs.

Répond à l'audit sécurité (S1/S8) :
- plus de « await pdf_file.read() » intégral en mémoire : les fichiers sont
  streamés par morceaux vers un fichier temporaire, avec arrêt net (413) dès
  que le plafond MAX_UPLOAD_MB est dépassé ;
- validation du magic bytes « %PDF- » sur les premiers octets (422 sinon) ;
- les noms de fichiers fournis par le client sont assainis avant toute
  utilisation dans un chemin local (anti path traversal).
"""

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, UploadFile

# Plafond configurable via variable d'environnement (en Mo). Défaut : 100 Mo,
# suffisant pour les plus gros Journaux Officiels scannés.
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "100"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# Plafond GLOBAL du corps de requête, appliqué au niveau ASGI (cf. BodySizeLimit
# middleware) AVANT que Starlette ne spoole le multipart sur disque et AVANT
# l'authentification. On ajoute une marge à MAX_UPLOAD_BYTES : un upload
# multipart transporte, en plus du PDF, des champs de formulaire, des artefacts
# md/json optionnels et l'overhead d'encodage (frontières, en-têtes de parties).
# La marge est configurable ; défaut : plafond upload + 32 Mo.
MAX_BODY_MARGIN_MB = int(os.getenv("MAX_BODY_MARGIN_MB", "32"))
MAX_BODY_BYTES = int(os.getenv("MAX_BODY_MB", str(MAX_UPLOAD_MB + MAX_BODY_MARGIN_MB))) * 1024 * 1024

# Taille des morceaux lus depuis le flux multipart (1 Mo).
CHUNK_SIZE = 1024 * 1024

# En-tête PDF. La spec impose « %PDF- » en tête de fichier, mais les lecteurs
# réels (Acrobat inclus) tolèrent jusqu'à 1024 octets parasites avant — certains
# scanners d'administration en produisent. On applique la même tolérance.
_PDF_MAGIC = b"%PDF-"
_PDF_MAGIC_WINDOW = 1024


def sanitize_filename(raw_name: Optional[str], fallback: str = "document.pdf") -> str:
    """Réduit un nom de fichier client à un composant de chemin sûr.

    Supprime tout séparateur de répertoire (basename + remplacement des
    caractères hors [A-Za-z0-9._-]) pour interdire le path traversal quand le
    nom entre dans un os.path.join local. Renvoie `fallback` si rien d'utile
    ne subsiste.
    """

    name = os.path.basename(raw_name or "").strip()
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    # Un nom réduit à des points/underscores (ex : "..") n'a aucune valeur et
    # pourrait surprendre en aval : on retombe sur le nom par défaut.
    name = name.lstrip(".")
    if not name or set(name) <= {".", "_", "-"}:
        return fallback
    return name


@dataclass
class BufferedUpload:
    """Fichier uploadé bufferisé sur disque : chemin temporaire, taille, empreinte."""

    path: str
    size: int
    sha256: str


async def stream_upload_to_tmp(
    upload: UploadFile,
    tmp_dir: str,
    *,
    filename_prefix: str = "",
    require_pdf: bool = True,
    fallback_name: str = "document.pdf",
    max_bytes: Optional[int] = None,
) -> BufferedUpload:
    """Streame un UploadFile vers un fichier temporaire, sans le charger en mémoire.

    - 413 dès que la taille dépasse `max_bytes` (défaut : MAX_UPLOAD_BYTES) ;
    - 422 si `require_pdf` et que l'en-tête « %PDF- » est absent des premiers octets ;
    - le fichier temporaire est supprimé en cas d'erreur ; sinon, c'est à
      l'appelant de le nettoyer (finally) ou de le transmettre à une tâche de fond.
    """

    limit = max_bytes if max_bytes is not None else MAX_UPLOAD_BYTES
    os.makedirs(tmp_dir, exist_ok=True)
    safe_name = sanitize_filename(upload.filename, fallback=fallback_name)
    path = os.path.join(tmp_dir, f"{uuid.uuid4()}_{filename_prefix}{safe_name}")

    digest = hashlib.sha256()
    size = 0
    first_chunk = True
    try:
        with open(path, "wb") as buffer:
            while True:
                chunk = await upload.read(CHUNK_SIZE)
                if not chunk:
                    break
                if first_chunk:
                    first_chunk = False
                    if require_pdf and _PDF_MAGIC not in chunk[:_PDF_MAGIC_WINDOW]:
                        raise HTTPException(
                            status_code=422,
                            detail="Le fichier fourni n'est pas un PDF valide (en-tête %PDF- absent).",
                        )
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Fichier trop volumineux : la limite est de {limit // (1024 * 1024)} Mo.",
                    )
                digest.update(chunk)
                buffer.write(chunk)
    except Exception:
        # Arrêt net : on ne laisse jamais traîner un temporaire partiel.
        if os.path.exists(path):
            os.remove(path)
        raise

    return BufferedUpload(path=path, size=size, sha256=digest.hexdigest())


async def read_upload_capped(
    upload: UploadFile,
    *,
    label: str = "fichier",
    max_bytes: Optional[int] = None,
) -> bytes:
    """Lit un UploadFile en mémoire par morceaux, avec arrêt net (413) au plafond.

    Pour les artefacts md/json dont la fusion (`chunk_merger`) exige des octets
    en mémoire : on garde la lecture complète mais bornée.
    """

    limit = max_bytes if max_bytes is not None else MAX_UPLOAD_BYTES
    parts: list[bytes] = []
    size = 0
    while True:
        chunk = await upload.read(CHUNK_SIZE)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise HTTPException(
                status_code=413,
                detail=f"Le {label} est trop volumineux : la limite est de {limit // (1024 * 1024)} Mo.",
            )
        parts.append(chunk)
    return b"".join(parts)


class _BodyTooLarge(Exception):
    """Signale en interne un corps ASGI qui dépasse le plafond global."""


class BodySizeLimitMiddleware:
    """Middleware ASGI pur qui plafonne la TAILLE DU CORPS avant tout le reste.

    Pourquoi au niveau ASGI et pas dans une dépendance FastAPI : Starlette spoole
    l'intégralité du multipart sur disque AVANT que les dépendances (dont l'auth)
    ne s'exécutent. Un anonyme pouvait donc pousser 50 Go et saturer le disque
    sans jamais être authentifié. Ici on intervient au plus tôt :

    - si ``Content-Length`` est présent et dépasse le plafond → 413 immédiat,
      sans lire un seul octet de corps (défense contre un Content-Length honnête) ;
    - sinon (chunked / Content-Length absent ou menteur) on enveloppe ``receive``
      pour COMPTER les octets réellement reçus et couper net à la limite : dès que
      le cumul dépasse ``max_body_bytes``, on renvoie un 413 propre et on cesse de
      lire (défense contre un Content-Length menteur ou un transfert chunked).

    Le plafond est global et généreux (cf. MAX_BODY_BYTES) : les routes sans gros
    corps ne le touchent jamais, et la validation fine (magic bytes, plafond
    upload exact) reste assurée en aval par ``stream_upload_to_tmp``.
    """

    def __init__(self, app, max_body_bytes: int = MAX_BODY_BYTES):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 1) Content-Length honnête et trop grand → 413 immédiat, corps jamais lu.
        content_length = self._declared_length(scope)
        if content_length is not None and content_length > self.max_body_bytes:
            await self._send_413(send)
            return

        # 2) Comptage réel des octets (chunked / Content-Length absent ou menteur).
        received = 0
        limit = self.max_body_bytes

        async def counting_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge()
            return message

        try:
            await self.app(scope, counting_receive, send)
        except _BodyTooLarge:
            # Le corps a dépassé le plafond en cours de lecture. On tente un 413
            # propre ; si la réponse a déjà commencé (rare : l'app a streamé avant
            # de finir de lire), on ne peut plus rien émettre et on laisse tomber.
            try:
                await self._send_413(send)
            except RuntimeError:
                pass

    @staticmethod
    def _declared_length(scope) -> Optional[int]:
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    return int(value.decode("latin-1").strip())
                except (ValueError, AttributeError):
                    return None
        return None

    async def _send_413(self, send) -> None:
        limit_mb = self.max_body_bytes // (1024 * 1024)
        body = (
            b'{"message":"Corps de requete trop volumineux : la limite est de '
            + str(limit_mb).encode("ascii")
            + b' Mo."}'
        )
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})
