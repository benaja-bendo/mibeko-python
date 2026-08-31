"""Suppression d'un acte : soft-delete réversible, aucun artefact détruit."""

import os
import sys
import uuid
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from conftest import stub_service_modules
from src.db.database import SessionLocal
from src.db.models import Article, LegalDocument, MediaFile, OfficialJournal

with stub_service_modules():
    from src.api.routers import documents as documents_router


@pytest.fixture
def shared_source_documents():
    db = SessionLocal()
    object_key = f"documents/flux/tests/{uuid.uuid4()}/source.pdf"

    first_document = LegalDocument(
        titre_officiel="Premier acte de test",
        document_role="FLUX",
        curation_status="draft",
    )
    second_document = LegalDocument(
        titre_officiel="Second acte de test",
        document_role="FLUX",
        curation_status="draft",
    )
    db.add_all([first_document, second_document])
    db.flush()

    for document in (first_document, second_document):
        db.add(
            MediaFile(
                document_id=document.id,
                file_path=f"s3://mibeko-documents/{object_key}",
                storage_provider="MINIO",
                bucket_name="mibeko-documents",
                object_key=object_key,
                original_filename="source.pdf",
                mime_type="application/pdf",
                file_category="SOURCE_PDF",
            )
        )

    db.commit()

    try:
        yield db, first_document.id, second_document.id, object_key
    finally:
        db.rollback()
        db.query(MediaFile).filter(MediaFile.document_id.in_([first_document.id, second_document.id])).delete(
            synchronize_session=False
        )
        db.query(LegalDocument).filter(LegalDocument.id.in_([first_document.id, second_document.id])).delete(
            synchronize_session=False
        )
        db.commit()
        db.close()


def test_supprimer_deux_documents_partageant_un_objet_conserve_les_medias(shared_source_documents):
    db, first_document_id, second_document_id, object_key = shared_source_documents

    documents_router.delete_document(str(first_document_id), db, _user=object())

    first = db.query(LegalDocument).filter(LegalDocument.id == first_document_id).one()
    assert first.deleted_at is not None
    assert db.query(LegalDocument).filter(LegalDocument.id == second_document_id).first() is not None
    assert db.query(MediaFile).filter(MediaFile.document_id == first_document_id).count() == 1
    assert db.query(MediaFile).filter(MediaFile.document_id == second_document_id).count() == 1

    documents_router.delete_document(str(second_document_id), db, _user=object())

    second = db.query(LegalDocument).filter(LegalDocument.id == second_document_id).one()
    assert second.deleted_at is not None
    assert db.query(MediaFile).filter(MediaFile.object_key == object_key).count() == 2


def test_un_objet_reference_par_un_journal_officiel_n_est_pas_purge():
    db = SessionLocal()
    object_key = f"documents/flux/tests/{uuid.uuid4()}/journal.pdf"
    journal = OfficialJournal(
        title="Journal officiel de test",
        publication_date=date(2025, 8, 14),
        file_path=f"s3://mibeko-documents/{object_key}",
    )
    document = LegalDocument(
        titre_officiel="Acte extrait du journal de test",
        document_role="FLUX",
        curation_status="draft",
        official_journal=journal,
    )
    db.add(document)
    db.flush()
    db.add(
        MediaFile(
            document_id=document.id,
            file_path=journal.file_path,
            storage_provider="MINIO",
            bucket_name="mibeko-documents",
            object_key=object_key,
            original_filename="journal.pdf",
            mime_type="application/pdf",
            file_category="SOURCE_PDF",
        )
    )
    db.commit()

    try:
        documents_router.delete_document(str(document.id), db, _user=object())

        db.refresh(document)
        assert document.deleted_at is not None
        assert db.query(MediaFile).filter(MediaFile.document_id == document.id).count() == 1
        assert db.query(OfficialJournal).filter(OfficialJournal.id == journal.id).first() is not None
    finally:
        db.rollback()
        db.query(MediaFile).filter(MediaFile.document_id == document.id).delete(synchronize_session=False)
        db.query(LegalDocument).filter(LegalDocument.id == document.id).delete(synchronize_session=False)
        db.query(OfficialJournal).filter(OfficialJournal.id == journal.id).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_un_document_supprime_est_restaurable_avec_ses_articles():
    db = SessionLocal()
    document = LegalDocument(
        titre_officiel="Acte restaurable",
        document_role="FLUX",
        curation_status="draft",
    )
    db.add(document)
    db.flush()
    article = Article(
        document_id=document.id,
        numero_article="1",
        ordre_affichage=1,
        validation_status="pending",
    )
    article_deja_retire = Article(
        document_id=document.id,
        numero_article="2",
        ordre_affichage=2,
        validation_status="pending",
        deleted_at=datetime.now() - timedelta(days=1),
    )
    db.add_all([article, article_deja_retire])
    db.commit()

    try:
        documents_router.delete_document(str(document.id), db, _user=object())
        db.refresh(document)
        db.refresh(article)
        db.refresh(article_deja_retire)

        assert document.deleted_at is not None
        assert article.deleted_at is not None
        assert article_deja_retire.deleted_at is not None

        documents_router.restore_document(str(document.id), db, _user=object())
        db.refresh(document)
        db.refresh(article)
        db.refresh(article_deja_retire)

        assert document.deleted_at is None
        assert article.deleted_at is None
        assert article_deja_retire.deleted_at is not None
    finally:
        db.rollback()
        db.query(Article).filter(Article.document_id == document.id).delete(synchronize_session=False)
        db.query(LegalDocument).filter(LegalDocument.id == document.id).delete(synchronize_session=False)
        db.commit()
        db.close()
