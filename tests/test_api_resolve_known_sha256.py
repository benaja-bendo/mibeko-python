"""`_resolve_known_sha256` (mibeko-python#23, § 3.6 identité n°1 : l'empreinte
SHA-256 complète est la seule façon fiable de reconnaître un fichier déjà
connu, jamais son titre) — contre une vraie base Postgres : c'est la
déduplication qui protège `POST /api/v1/depots` contre un doublon (critère
de clôture de L1 : « le même PDF déposé deux fois renvoie 409 »).
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.acquisition.manifest import Manifest, ManifestEntry
from src.api.main import _resolve_known_sha256
from src.db.database import SessionLocal
from src.db.models import IngestionProvenance, LegalDocument, MediaFile


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


def _sha(suffix: str) -> str:
    return (suffix * 64)[:64]


def test_sha256_inedit_ne_trouve_rien(db, tmp_path):
    assert _resolve_known_sha256(db, _sha("a"), manifests_directory=tmp_path) is None


def test_sha256_deja_un_document_renvoie_son_id(db, tmp_path):
    sha = _sha("b")
    doc = LegalDocument(
        titre_officiel="Test resolve sha256 — document",
        document_role="FLUX",
        document_key=f"test-resolve-sha256-{uuid.uuid4()}",
        curation_status="draft",
    )
    db.add(doc)
    db.flush()
    media = MediaFile(
        document_id=doc.id, file_path="s3://fake/x.pdf", object_key="x.pdf",
        checksum_sha256=sha, file_category="SOURCE_PDF",
    )
    db.add(media)
    db.commit()

    try:
        resultat = _resolve_known_sha256(db, sha, manifests_directory=tmp_path)
        assert resultat == {"document_id": str(doc.id), "manifest_id": None}
    finally:
        db.delete(doc)  # ondelete=CASCADE emporte le MediaFile
        db.commit()


def test_sha256_deja_une_provenance_sans_document_renvoie_le_manifest_id(db, tmp_path):
    sha = _sha("c")
    provenance = IngestionProvenance(
        manifest_id="depots/deja-en-provenance", type_source="acte", sha256=sha,
    )
    db.add(provenance)
    db.commit()

    try:
        resultat = _resolve_known_sha256(db, sha, manifests_directory=tmp_path)
        assert resultat == {"document_id": None, "manifest_id": "depots/deja-en-provenance"}
    finally:
        db.delete(provenance)
        db.commit()


def test_sha256_dans_un_manifeste_heritage_sans_provenance(db, tmp_path):
    """Un manifeste acquis en lot avant l'existence d'IngestionProvenance
    (§ 3.7) reste détecté — sinon un PDF déjà acquis par la veille avant ce
    ticket serait redéposé comme neuf."""
    sha = _sha("d")
    manifest = Manifest(tmp_path / "legacy.jsonl")
    manifest.upsert(ManifestEntry(
        id="legacy/deja-acquis", fichier="sources/legacy/x.pdf",
        sha256=sha, size_bytes=10, type_source="acte",
    ))
    manifest.save()

    resultat = _resolve_known_sha256(db, sha, manifests_directory=tmp_path)
    assert resultat == {"document_id": None, "manifest_id": "legacy/deja-acquis"}


def test_media_file_prime_sur_la_provenance(db, tmp_path):
    """Un document existe déjà (le job a réussi) : on renvoie son id, pas
    juste le manifest_id — sinon « ouvrir le dossier » (une des trois actions
    du 409) n'aurait rien à ouvrir alors qu'un document existe bel et bien."""
    sha = _sha("e")
    doc = LegalDocument(
        titre_officiel="Test resolve sha256 — priorité document",
        document_role="FLUX",
        document_key=f"test-resolve-sha256-priorite-{uuid.uuid4()}",
        curation_status="draft",
    )
    db.add(doc)
    db.flush()
    media = MediaFile(
        document_id=doc.id, file_path="s3://fake/y.pdf", object_key="y.pdf",
        checksum_sha256=sha, file_category="SOURCE_PDF",
    )
    db.add(media)
    provenance = IngestionProvenance(manifest_id="depots/deja-traite", type_source="acte", sha256=sha)
    db.add(provenance)
    db.commit()

    try:
        resultat = _resolve_known_sha256(db, sha, manifests_directory=tmp_path)
        assert resultat["document_id"] == str(doc.id)
    finally:
        db.delete(provenance)
        db.delete(doc)
        db.commit()
