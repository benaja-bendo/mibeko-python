"""
Tests des garde-fous d'upload (audit sécurité S1/S8/S13).

- sanitize_filename : anti path traversal sur les noms de fichiers clients ;
- stream_upload_to_tmp : plafond de taille (413), magic bytes %PDF- (422),
  intégrité (taille + SHA-256), nettoyage du temporaire en cas d'erreur ;
- read_upload_capped : lecture en mémoire bornée (413) ;
- parse_optional_date : date mal formée → 422 explicite (plus de 500).

Exécutable sans backend : python3 -m pytest tests/test_upload_security.py
(les modules MinIO/MinerU sont neutralisés pour pouvoir importer src.api.main.)
"""

import asyncio
import hashlib
import io
import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conftest import stub_service_modules  # noqa: E402

with stub_service_modules():
    from src.api.upload_utils import (  # noqa: E402
        read_upload_capped,
        sanitize_filename,
        stream_upload_to_tmp,
    )
    from src.api.main import parse_optional_date  # noqa: E402


class FakeUpload:
    """Double minimal d'UploadFile : seuls .read() et .filename sont consommés."""

    def __init__(self, data: bytes, filename="document.pdf"):
        self._buffer = io.BytesIO(data)
        self.filename = filename

    async def read(self, size=-1):
        return self._buffer.read(size)


PDF_HEADER = b"%PDF-1.7\n"


# ---------------------------------------------------------------------------
# sanitize_filename (S8)
# ---------------------------------------------------------------------------

def test_sanitize_filename_conserve_un_nom_simple():
    assert sanitize_filename("journal-2026_01.pdf") == "journal-2026_01.pdf"


def test_sanitize_filename_bloque_le_path_traversal():
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("..") == "document.pdf"
    # Séparateurs Windows : neutralisés en underscores (pas de remontée possible).
    assert "/" not in sanitize_filename("..\\..\\boot.ini")
    assert "\\" not in sanitize_filename("..\\..\\boot.ini")


def test_sanitize_filename_remplace_les_caracteres_exotiques():
    # basename coupe au dernier « / » : seul le segment final subsiste, assaini.
    assert sanitize_filename("loi n° 45/75 (mars).pdf") == "75_mars_.pdf"
    assert sanitize_filename("décret n°2026.pdf") == "d_cret_n_2026.pdf"


def test_sanitize_filename_retombe_sur_le_defaut():
    assert sanitize_filename(None) == "document.pdf"
    assert sanitize_filename("   ") == "document.pdf"
    assert sanitize_filename("///", fallback="journal.pdf") == "journal.pdf"


# ---------------------------------------------------------------------------
# stream_upload_to_tmp (S1)
# ---------------------------------------------------------------------------

def test_stream_upload_pdf_valide(tmp_path):
    data = PDF_HEADER + b"x" * 5000
    upload = FakeUpload(data, filename="code du travail.pdf")

    result = asyncio.run(stream_upload_to_tmp(upload, str(tmp_path)))

    assert os.path.exists(result.path)
    assert result.size == len(data)
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    with open(result.path, "rb") as handle:
        assert handle.read() == data
    # Le nom client est assaini dans le chemin temporaire.
    assert " " not in os.path.basename(result.path)


def test_stream_upload_rejette_un_faux_pdf(tmp_path):
    upload = FakeUpload(b"<html>pas un pdf</html>" * 100)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(stream_upload_to_tmp(upload, str(tmp_path)))

    assert exc_info.value.status_code == 422
    # Arrêt net : aucun fichier temporaire ne subsiste.
    assert os.listdir(tmp_path) == []


def test_stream_upload_tolere_le_magic_decale(tmp_path):
    # Certains scanners préfixent quelques octets parasites : le header doit
    # être accepté s'il apparaît dans la fenêtre des 1024 premiers octets.
    data = b"\x00" * 10 + PDF_HEADER + b"contenu"
    result = asyncio.run(stream_upload_to_tmp(FakeUpload(data), str(tmp_path)))
    assert result.size == len(data)


def test_stream_upload_applique_le_plafond(tmp_path):
    data = PDF_HEADER + b"x" * (2 * 1024 * 1024)
    upload = FakeUpload(data)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(stream_upload_to_tmp(upload, str(tmp_path), max_bytes=1024 * 1024))

    assert exc_info.value.status_code == 413
    assert os.listdir(tmp_path) == []


def test_stream_upload_fichier_vide(tmp_path):
    # Un fichier vide n'est pas rejeté ici : les endpoints renvoient leur
    # propre 422 « fichier vide » (message historique préservé).
    result = asyncio.run(stream_upload_to_tmp(FakeUpload(b""), str(tmp_path)))
    assert result.size == 0
    assert os.path.exists(result.path)


# ---------------------------------------------------------------------------
# read_upload_capped (S1 — artefacts md/json)
# ---------------------------------------------------------------------------

def test_read_upload_capped_renvoie_les_octets():
    data = b"# Markdown\n" * 1000
    assert asyncio.run(read_upload_capped(FakeUpload(data, "source.md"))) == data


def test_read_upload_capped_applique_le_plafond():
    data = b"x" * (2 * 1024 * 1024)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(read_upload_capped(FakeUpload(data, "source.md"), max_bytes=1024 * 1024))
    assert exc_info.value.status_code == 413


# ---------------------------------------------------------------------------
# parse_optional_date (S13)
# ---------------------------------------------------------------------------

def test_parse_optional_date_valide():
    import datetime

    assert parse_optional_date("2026-07-03") == datetime.date(2026, 7, 3)
    assert parse_optional_date(" 2026-07-03 ") == datetime.date(2026, 7, 3)
    assert parse_optional_date(None) is None
    assert parse_optional_date("") is None


def test_parse_optional_date_mal_formee_renvoie_422():
    with pytest.raises(HTTPException) as exc_info:
        parse_optional_date("03/07/2026")
    assert exc_info.value.status_code == 422
    assert "YYYY-MM-DD" in exc_info.value.detail


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
