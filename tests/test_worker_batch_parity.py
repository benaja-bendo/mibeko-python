"""Test de parité (mibeko-python#23, critère de clôture de L1) : le même
contenu ingéré par le chemin worker (dépôt web, `process_job`) et par le
chemin batch (`process-batch` + `structure-batch`, appelés comme les
commandes CLI le font) doit produire des lignes identiques — `legal_documents`
(hors identifiants/horodatages : `id`, `document_key`, `stock_code`,
`titre_officiel` dépendent légitimement de l'identité du manifeste, deux
entrées distinctes ne peuvent pas les partager), `articles`, `article_versions`
(contenu + `validation_status`), `media_files.file_category`, `curation_flags`.

Preuve directe, pas seulement raisonnée : `process_job` (`src/worker/runner.py`)
et `run_batch` (`src/structuration/batch.py`) appellent tous deux LA MÊME
fonction `structure_document` — ce test protège cette unification contre une
régression future qui ferait diverger l'un des deux chemins (exactement la
classe de défaut qui existait avant #23 : le JO web écrivait `validated`
pendant que `structure_document` écrit `pending`).

Client Mistral figé (déterministe) injecté des deux côtés : la LLM elle-même
n'est pas sous test ici (déjà couverte ailleurs), seule l'ORCHESTRATION l'est
— un vrai appel LLM introduirait une variance qui n'aurait rien à voir avec
une régression de parité.

Vraie base Postgres + vrai MinIO (docker compose local), comme
tests/test_worker_runner.py et tests/test_api_depot_concurrence.py.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fitz
import pytest

from src.acquisition.manifest import Manifest, ManifestEntry, sha256_file
from src.db.database import SessionLocal
from src.db.models import Article, ArticleVersion, CurationFlag, IngestionJob, LegalDocument, MediaFile
from src.parsing.batch import process_entry
from src.structuration.structurer import structure_document
from src.worker.runner import process_job, reserve_job

# Même exemple que tests/test_structuration_journals_split.py (déjà éprouvé
# sur le découpage en actes) : un sommaire suivi de deux actes distincts.
MD_JO_SOMMAIRE_PUIS_DEUX_ACTES = (
    "[[MIBEKO_PAGE:1]]\n"
    "SOMMAIRE\n"
    "Loi n° 12-2026 du 3 janvier 2026 portant code du travail (page 3).\n"
    "Décret n° 45-2026 du 5 janvier 2026 portant nomination (page 8).\n"
    "[[MIBEKO_PAGE:3]]\n"
    "LOI N° 12-2026 DU 3 JANVIER 2026 PORTANT CODE DU TRAVAIL\n"
    "ARTICLE PREMIER : La presente loi regit les relations de travail.\n"
    "Article 2 : Elle entre en vigueur des sa promulgation.\n"
    "[[MIBEKO_PAGE:8]]\n"
    "DECRET N° 45-2026 DU 5 JANVIER 2026 PORTANT NOMINATION\n"
    "Article 1 : Est nomme M. X au poste de Y.\n"
    "Article 2 : Le present decret sera publie.\n"
)


class FixedMetadataMistralClient:
    """Réponse déterministe : ni numéro ni nature (repli sur
    NATURE_PAR_TYPE_SOURCE), une date de publication réelle pour satisfaire
    chk_legal_documents_role_logic sans déclencher le repli
    information_manquante (mibeko-python#23, § objectif n°9)."""

    async def extract_metadata(self, texte, instructions):
        return {
            "nature": None, "numero": None, "date_signature": None,
            "date_publication": "2020-01-01", "autorite": None,
        }


CODE_TEXT = (
    "CODE DE TEST PARITE\n"
    "ARTICLE PREMIER : Premiere disposition du code de test.\n"
    "ARTICLE 2 : Deuxieme disposition du code de test.\n"
) * 4


def _make_pdf(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(50, 50, 545, 792), text, fontsize=9)
    doc.save(str(path))
    doc.close()
    return path


def _seed_entry(
    data_dir: Path, manifest_key: str, entry_id: str, rel_path: str, text: str, type_source: str
) -> ManifestEntry:
    pdf_path = _make_pdf(data_dir / rel_path, text)
    entry = ManifestEntry(
        id=entry_id, fichier=rel_path, sha256=sha256_file(pdf_path), size_bytes=pdf_path.stat().st_size,
        type_source=type_source, statut="telecharge",
    )
    manifest = Manifest(data_dir / "manifests" / f"{manifest_key}.jsonl")
    manifest.upsert(entry)
    manifest.save()
    return entry


def _structure_avec_client_fige(db, data_dir, entry, dry_run=False):
    return structure_document(db, data_dir, entry, mistral_client=FixedMetadataMistralClient(), dry_run=dry_run)


def _seed_markdown_entry(data_dir: Path, manifest_key: str, entry_id: str, markdown: str) -> ManifestEntry:
    """Place directement l'artefact markdown (étape parse déjà « faite »),
    comme tests/test_structuration_journals_split.py — le découpage en actes
    (structuration) est ce que ce test compare, pas le triage/OCR, déjà
    couvert ailleurs (tests/test_parsing_batch.py). Un vrai PDF minimal est
    quand même nécessaire sur disque : structure_document (chemin JO comme
    chemin acte isolé) y lit le PDF SOURCE pour l'upload MinIO, indépendamment
    du markdown déjà là.
    """
    rel_path = f"sources/{entry_id}.pdf"
    pdf_path = _make_pdf(data_dir / rel_path, "peu importe : jamais relu comme texte ici")

    md_path = data_dir / "pipeline" / "md" / f"{entry_id}.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(markdown, encoding="utf-8")

    entry = ManifestEntry(
        id=entry_id, fichier=rel_path, sha256=sha256_file(pdf_path), size_bytes=pdf_path.stat().st_size,
        type_source="journal_officiel", statut="parse",
    )
    manifest = Manifest(data_dir / "manifests" / f"{manifest_key}.jsonl")
    manifest.upsert(entry)
    manifest.save()
    return entry


def _process_entry_deja_fait(data_dir, entry, force=False):
    """`process_entry_fn` injecté pour le chemin web : l'artefact markdown
    est déjà placé par `_seed_markdown_entry`, comme un vrai triage natif
    l'aurait laissé — jamais de vraie triage/OCR ici, hors périmètre de ce
    test (voir sa docstring)."""
    return {"id": entry.id, "skipped": True, "methode": "native"}


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


def _champs_comparables(db, document_id) -> dict:
    """Tout ce que le critère de clôture demande de comparer — jamais
    `id`/`document_key`/`stock_code`/`titre_officiel`, qui dépendent
    légitimement de l'identité du manifeste (deux entrées distinctes)."""
    doc = db.query(LegalDocument).filter(LegalDocument.id == document_id).first()
    articles = (
        db.query(Article)
        .filter(Article.document_id == document_id, Article.deleted_at.is_(None))
        .order_by(Article.ordre_affichage)
        .all()
    )
    contenu_articles = []
    for a in articles:
        versions = db.query(ArticleVersion).filter(ArticleVersion.article_id == a.id).all()
        contenu_articles.append((
            a.numero_article,
            sorted(v.contenu_texte for v in versions),
            sorted(v.validation_status for v in versions),
        ))
    media_categories = sorted(
        m.file_category for m in db.query(MediaFile).filter(MediaFile.document_id == document_id).all()
    )
    nb_flags = db.query(CurationFlag).filter(CurationFlag.document_id == document_id).count()
    return {
        "document_role": doc.document_role,
        "curation_status": doc.curation_status,
        "extraction_status": doc.extraction_status,
        "type_code": doc.type_code,
        "articles": contenu_articles,
        "media_categories": media_categories,
        "nb_curation_flags": nb_flags,
    }


def test_parite_code_web_vs_batch(db, tmp_path):
    """STOCK (« code ») : dépôt web (process_job, bout en bout) vs batch
    (process_entry + structure_document, appelés comme process-batch et
    structure-batch le font)."""
    data_dir = tmp_path / "data"

    entry_web = _seed_entry(data_dir, "test-parity", "test-parity/code-web", "sources/code-web.pdf", CODE_TEXT, "code")
    entry_batch = _seed_entry(data_dir, "test-parity", "test-parity/code-batch", "sources/code-batch.pdf", CODE_TEXT, "code")

    document_ids: dict[str, str] = {}
    job_ids: list = []
    try:
        # ── Chemin web : dépôt → job → worker (process_job, réel de bout en bout) ──
        job = IngestionJob(kind=IngestionJob.KIND_DEPOT, manifest_id=entry_web.id)
        db.add(job)
        db.commit()
        job_ids.append(job.id)

        reserved = reserve_job(db)
        assert reserved is not None and reserved.id == job.id
        ok = process_job(db, data_dir, reserved, structure_document_fn=_structure_avec_client_fige)
        assert ok is True

        job_final = db.query(IngestionJob).filter(IngestionJob.id == job.id).first()
        assert job_final.status == IngestionJob.STATUS_DONE
        document_ids["web"] = job_final.result["structure"]["document_ids"][0]

        # ── Chemin batch : process-batch puis structure-batch, appelés
        # directement comme le fait chaque commande CLI ──
        manifest_batch = Manifest(data_dir / "manifests" / "test-parity.jsonl")
        entry_batch_frais = manifest_batch.get(entry_batch.id)
        parse_result = process_entry(data_dir, entry_batch_frais, force=False)
        assert parse_result.get("methode") != "erreur"

        struct_result = structure_document(
            db, data_dir, entry_batch_frais, mistral_client=FixedMetadataMistralClient(), dry_run=False
        )
        assert struct_result["statut"] == "structure"
        document_ids["batch"] = str(struct_result["document_id"])

        # ── Comparaison ──
        champs_web = _champs_comparables(db, document_ids["web"])
        champs_batch = _champs_comparables(db, document_ids["batch"])

        assert champs_web == champs_batch
        assert champs_web["document_role"] == "STOCK"
        assert champs_web["curation_status"] == "draft"
        # Triage natif (PDF texte, pas de scan) : pas de JSON MinerU produit,
        # seulement le markdown + le PDF source.
        assert champs_web["media_categories"] == ["EXTRACTION_MARKDOWN", "SOURCE_PDF"]
        assert champs_web["articles"], "le test ne prouve rien si aucun article n'a été produit"
        assert all(vs == ["pending"] for (_, _, vs) in champs_web["articles"])
    finally:
        for doc_id in document_ids.values():
            db.query(LegalDocument).filter(LegalDocument.id == doc_id).delete()
        if job_ids:
            db.query(IngestionJob).filter(IngestionJob.id.in_(job_ids)).delete(synchronize_session=False)
        db.commit()


def test_parite_jo_multi_actes_web_vs_batch(db, tmp_path):
    """FLUX, Journal officiel scindé en actes : historiquement le chemin le
    plus à risque (mibeko-python#23 § 2.2 — le JO web écrivait `validated`
    au lieu de `draft`/`pending`, divergence diagnostiquée le 03/08 et jamais
    fermée avant ce ticket). Même contenu (sommaire + 2 actes), un dépôt web
    et un passage batch doivent produire les deux mêmes actes."""
    data_dir = tmp_path / "data"

    entry_web = _seed_markdown_entry(data_dir, "test-parity-jo", "test-parity-jo/web", MD_JO_SOMMAIRE_PUIS_DEUX_ACTES)
    entry_batch = _seed_markdown_entry(data_dir, "test-parity-jo", "test-parity-jo/batch", MD_JO_SOMMAIRE_PUIS_DEUX_ACTES)

    document_ids: dict[str, list[str]] = {}
    job_ids: list = []
    try:
        # ── Chemin web : dépôt → job → worker (process_job, réel de bout en bout) ──
        job = IngestionJob(kind=IngestionJob.KIND_DEPOT, manifest_id=entry_web.id)
        db.add(job)
        db.commit()
        job_ids.append(job.id)

        reserved = reserve_job(db)
        ok = process_job(
            db, data_dir, reserved,
            process_entry_fn=_process_entry_deja_fait,
            structure_document_fn=_structure_avec_client_fige,
        )
        assert ok is True

        job_final = db.query(IngestionJob).filter(IngestionJob.id == job.id).first()
        assert job_final.status == IngestionJob.STATUS_DONE
        document_ids["web"] = job_final.result["structure"]["document_ids"]
        assert len(document_ids["web"]) == 2, "le sommaire + 2 actes doit produire exactement 2 documents"

        # ── Chemin batch : structure-batch, appelé directement (le triage est
        # déjà fait, comme après un vrai process-batch) ──
        manifest_batch = Manifest(data_dir / "manifests" / "test-parity-jo.jsonl")
        entry_batch_frais = manifest_batch.get(entry_batch.id)
        struct_result = structure_document(
            db, data_dir, entry_batch_frais, mistral_client=FixedMetadataMistralClient(), dry_run=False
        )
        assert struct_result["statut"] == "structure"
        document_ids["batch"] = struct_result["document_ids"]
        assert len(document_ids["batch"]) == 2

        # ── Comparaison acte par acte, dans l'ordre du document (même
        # sommaire des deux côtés → même ordre de découpage) ──
        for doc_id_web, doc_id_batch in zip(document_ids["web"], document_ids["batch"]):
            champs_web = _champs_comparables(db, doc_id_web)
            champs_batch = _champs_comparables(db, doc_id_batch)
            assert champs_web == champs_batch
            assert champs_web["document_role"] == "FLUX"
            assert champs_web["curation_status"] == "draft"
            assert all(vs == ["pending"] for (_, _, vs) in champs_web["articles"])
    finally:
        for ids in document_ids.values():
            db.query(LegalDocument).filter(LegalDocument.id.in_(ids)).delete(synchronize_session=False)
        if job_ids:
            db.query(IngestionJob).filter(IngestionJob.id.in_(job_ids)).delete(synchronize_session=False)
        db.commit()
