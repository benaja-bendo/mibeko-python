"""Tests du découpage d'un Journal officiel en actes distincts (audit
docs/audit-ingestion-2026-08-02.md, phases 0-1 : `structure-batch` ne routait
jamais vers le découpeur en actes, produisant un document plat par JO). DB
fake (aucun réseau, aucune Postgres réelle), même style que
`test_structuration_structurer.py`.
"""

from pathlib import Path

import src.structuration.journals as journals_module
import src.structuration.structurer as structurer
from src.acquisition.manifest import ManifestEntry
from src.db.models import LegalDocument, MediaFile
from src.structuration.structurer import structure_document


def _patch_ingest_hierarchy_noop(monkeypatch):
    """`ingest_hierarchy` fait de vraies opérations SQLAlchemy (Article,
    ArticleVersion, sous-requête `.in_()`) incompatibles avec une session fake
    minimale — même convention que `test_structuration_structurer.py`, qui la
    neutralise systématiquement. Deux modules l'importent séparément
    (`structurer` pour le chemin plat, `journals` pour le découpage en actes) :
    les deux bindings doivent être patchés."""
    monkeypatch.setattr(structurer, "ingest_hierarchy", lambda *args, **kwargs: None)
    monkeypatch.setattr(journals_module, "ingest_hierarchy", lambda *args, **kwargs: None)

CLEAN_MD = "ARTICLE PREMIER : La presente loi regit les relations de travail."

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


class FakeQuery:
    """Filtre naïf sur `document_key` uniquement — suffisant pour ces tests :
    les autres modèles (CurationFlag, etc.) ne sont jamais interrogés par le
    chemin JO scindé."""

    def __init__(self, registry, model):
        self._registry = registry
        self._model = model
        self._document_key = None

    def filter(self, *criteria, **kwargs):
        for criterion in criteria:
            try:
                text = str(criterion)
            except Exception:
                continue
            if "document_key" in text:
                right = getattr(criterion, "right", None)
                value = getattr(right, "value", None)
                if value is not None:
                    self._document_key = value
        return self

    def first(self):
        if self._model is not LegalDocument:
            return None
        if self._document_key is None:
            return None
        for doc in self._registry:
            if doc.document_key == self._document_key:
                return doc
        return None


class FakeTypeCodesResult:
    """Simule le résultat de `db.execute(text("SELECT code FROM document_types"))`
    qu'utilise `map_detected_type_to_type_code` — mêmes codes que la table
    réelle (`database/migrations` côté Laravel), suffisant pour ces tests."""

    _CODES = ("LOI", "DEC", "ARR", "ORD", "CONST", "TEXTE", "CONV", "AU")

    def fetchall(self):
        return [(code,) for code in self._CODES]


class RegistryFakeSession:
    """Simule une persistance minimale par `document_key`, pour tester le
    find-or-update (idempotence) du découpage en actes — la `FakeSession` de
    `test_structuration_structurer.py` ne suffit pas ici : elle renvoie
    toujours le même objet constant pour toute requête, quel que soit le
    `document_key` demandé."""

    def __init__(self):
        self.added = []
        self.committed = False
        self.rolled_back = False
        self._legal_documents = []

    def query(self, model):
        return FakeQuery(self._legal_documents, model)

    def execute(self, *args, **kwargs):
        return FakeTypeCodesResult()

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, LegalDocument):
            self._legal_documents.append(obj)

    def flush(self):
        pass

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class FakeMinioService:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on or set()
        self.uploads = []

    def upload_file(self, object_name, file_path, content_type="application/pdf"):
        self.uploads.append((object_name, file_path, content_type))
        if content_type in self.fail_on:
            return None
        return f"s3://fake-bucket/{object_name}"


class ValidMetadataMistralClient:
    async def extract_metadata(self, texte, instructions):
        return {"nature": "Journal officiel", "numero": None, "date_signature": None,
                "date_publication": None, "autorite": None}


def _seed_entry(data_dir: Path, entry_id: str, markdown: str) -> ManifestEntry:
    md_path = data_dir / "pipeline" / "md" / f"{entry_id}.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(markdown, encoding="utf-8")
    return ManifestEntry(
        id=entry_id,
        fichier=f"sources/sgg/JO/{entry_id.split('/')[-1]}.pdf",
        sha256="a" * 64,
        size_bytes=100,
        type_source="journal_officiel",
        statut="parse",
    )


def test_jo_multi_actes_cree_n_documents_distincts(tmp_path: Path, monkeypatch):
    """Le cœur du correctif : un JO à deux actes doit produire DEUX
    `legal_documents` FLUX distincts, pas un document plat unique."""
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo/congo-jo-2026-40", MD_JO_SOMMAIRE_PUIS_DEUX_ACTES)
    db = RegistryFakeSession()
    _patch_ingest_hierarchy_noop(monkeypatch)
    monkeypatch.setattr(structurer, "minio_service", FakeMinioService())

    result = structure_document(db, data_dir, entry, mistral_client=ValidMetadataMistralClient())

    assert result["statut"] == "structure"
    documents = [obj for obj in db.added if isinstance(obj, LegalDocument)]
    assert len(documents) == 2
    titres = {d.titre_officiel for d in documents}
    assert any("LOI N° 12-2026" in t for t in titres)
    assert any("DECRET N° 45-2026" in t for t in titres)
    assert {d.document_role for d in documents} == {"FLUX"}
    assert {d.curation_status for d in documents} == {"draft"}
    document_keys = {d.document_key for d in documents}
    assert len(document_keys) == 2  # aucune collision de clé entre les deux actes
    for d in documents:
        assert d.metadata_.get("routage_jo_corrige") is True
        assert d.metadata_.get("sha256") == "a" * 64
    assert db.committed is True


def test_jo_multi_actes_televerse_une_seule_fois_pour_n_documents(tmp_path: Path, monkeypatch):
    """Un seul PDF/markdown source : un seul jeu d'uploads MinIO, référencé par
    N lignes `media_files` (une par acte) — pas de re-upload par acte."""
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo/congo-jo-2026-41", MD_JO_SOMMAIRE_PUIS_DEUX_ACTES)
    db = RegistryFakeSession()
    fake_minio = FakeMinioService()
    _patch_ingest_hierarchy_noop(monkeypatch)
    monkeypatch.setattr(structurer, "minio_service", fake_minio)

    result = structure_document(db, data_dir, entry, mistral_client=ValidMetadataMistralClient())

    assert result["statut"] == "structure"
    assert len(fake_minio.uploads) == 2  # 1 PDF + 1 markdown (pas de .json fourni)
    media_rows = [obj for obj in db.added if isinstance(obj, MediaFile)]
    # 2 actes x (SOURCE_PDF + EXTRACTION_MARKDOWN) = 4 lignes media_files
    assert len(media_rows) == 4
    object_keys = {m.object_key for m in media_rows if m.file_category == "SOURCE_PDF"}
    assert len(object_keys) == 1  # même objet MinIO référencé par les 2 actes


def test_jo_multi_actes_propage_le_nombre_de_pages_a_tous_les_actes(tmp_path: Path, monkeypatch):
    """Le contrôle aval des repères PDF ne doit pas attendre un backfill pour
    les actes nouvellement découpés depuis un JO déjà disponible localement."""
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo/congo-jo-2026-47", MD_JO_SOMMAIRE_PUIS_DEUX_ACTES)
    db = RegistryFakeSession()
    _patch_ingest_hierarchy_noop(monkeypatch)
    monkeypatch.setattr(structurer, "minio_service", FakeMinioService())
    monkeypatch.setattr(structurer, "compter_pages_pdf", lambda _path: 12)

    result = structure_document(db, data_dir, entry, mistral_client=ValidMetadataMistralClient())

    assert result["statut"] == "structure"
    pdf_rows = [
        obj for obj in db.added
        if isinstance(obj, MediaFile) and obj.file_category == "SOURCE_PDF"
    ]
    assert len(pdf_rows) == 2
    assert {media.page_count for media in pdf_rows} == {12}


def test_jo_un_seul_acte_reste_un_document_unique(tmp_path: Path, monkeypatch):
    """Mission phase 1 : « ou un seul [document] si le .md prouve que le JO ne
    contient qu'un acte » — le flux normal (document unique) doit continuer à
    s'appliquer, inchangé, quand le découpeur ne trouve qu'un acte."""
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo/congo-jo-2026-42", CLEAN_MD)
    db = RegistryFakeSession()
    _patch_ingest_hierarchy_noop(monkeypatch)
    monkeypatch.setattr(structurer, "minio_service", FakeMinioService())

    result = structure_document(db, data_dir, entry, mistral_client=ValidMetadataMistralClient())

    assert result["statut"] == "structure"
    documents = [obj for obj in db.added if isinstance(obj, LegalDocument)]
    assert len(documents) == 1


def test_jo_acte_ne_pose_jamais_reference_nor_sur_la_colonne_unique(tmp_path: Path, monkeypatch):
    """Régression (échec réel constaté à l'exécution de la phase 1 sur les 62
    documents du périmètre) : `legal_documents.reference_nor` porte une
    contrainte UNIQUE, mais un même numéro d'acte (« Arrêté n° 3705 ») se
    retrouve sur des actes distincts de JO différents (numérotation non
    globale) — l'écrire dedans fait violer la contrainte dès le second acte
    homonyme. La référence détectée doit être conservée en metadata (JSONB
    non contraint), jamais sur la colonne."""
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo/congo-jo-2026-44", MD_JO_SOMMAIRE_PUIS_DEUX_ACTES)
    db = RegistryFakeSession()
    _patch_ingest_hierarchy_noop(monkeypatch)
    monkeypatch.setattr(structurer, "minio_service", FakeMinioService())

    result = structure_document(db, data_dir, entry, mistral_client=ValidMetadataMistralClient())

    assert result["statut"] == "structure"
    documents = [obj for obj in db.added if isinstance(obj, LegalDocument)]
    assert len(documents) == 2
    for d in documents:
        assert d.reference_nor is None
    # Les deux titres portent bien un numéro extractible (« N° 12-2026 »,
    # « N° 45-2026 ») : la détection a eu lieu, seule l'écriture en colonne
    # unique est évitée.
    detectees = {d.metadata_.get("reference_nor_detectee") for d in documents}
    assert detectees == {"12-2026", "45-2026"}


def test_jo_deux_actes_de_titre_identique_ne_fusionnent_pas(tmp_path: Path, monkeypatch):
    """Régression (échec réel constaté à l'exécution de la phase 1, JO
    n°1-1958 : violation de `uq_media_files_document_object_key`) : deux
    actes du MÊME JO peuvent aboutir au même `document_key` (titres identiques
    après normalisation — `split_official_journal_markdown` ne les a pas tous
    dédupliqués dans ce cas réel). Sans désambiguïsation, le second acte
    retrouvait le document du premier et `ingest_hierarchy` écrasait son
    contenu (`clear_document_structure` vide la hiérarchie avant
    réinsertion) — perte de données silencieuse. On force ici le cas via
    `split_official_journal_markdown` mocké, pour tester la désambiguïsation
    de `split_and_persist_journal_acts` indépendamment de l'heuristique de
    dédoublonnage du découpeur (qui peut évoluer sans rapport avec ce test)."""
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo/congo-jo-1958-01", "[[MIBEKO_PAGE:1]]\npeu importe\n")
    db = RegistryFakeSession()
    _patch_ingest_hierarchy_noop(monkeypatch)
    monkeypatch.setattr(structurer, "minio_service", FakeMinioService())
    fake_split = lambda markdown_text: [
        {"titre": "ARRETE N° 100 DU 1 JANVIER 2026 PORTANT NOMINATION", "contenu": "Article 1 : Est nomme M. X.", "type": "ARRETE"},
        {"titre": "ARRETE N° 100 DU 1 JANVIER 2026 PORTANT NOMINATION", "contenu": "Article 1 : Est nomme Mme Y.", "type": "ARRETE"},
    ]
    # Deux bindings distincts du même import (`structurer` pour la
    # détection précoce/probe, `journals` pour la scission réelle).
    monkeypatch.setattr(structurer, "split_official_journal_markdown", fake_split)
    monkeypatch.setattr(journals_module, "split_official_journal_markdown", fake_split)

    result = structure_document(db, data_dir, entry, mistral_client=ValidMetadataMistralClient())

    assert result["statut"] == "structure"
    documents = [obj for obj in db.added if isinstance(obj, LegalDocument)]
    assert len(documents) == 2
    assert len({d.id for d in documents}) == 2
    assert len({d.document_key for d in documents}) == 2
    media_rows = [obj for obj in db.added if isinstance(obj, MediaFile)]
    assert len(media_rows) == 4  # 2 actes x (SOURCE_PDF + EXTRACTION_MARKDOWN), aucune collision


MD_JO_ACTE_VIDE_ENTRE_DEUX_TITRES = (
    "[[MIBEKO_PAGE:1]]\n"
    "LOI N° 12-2026 DU 3 JANVIER 2026 PORTANT CODE DU TRAVAIL\n"
    "DECRET N° 45-2026 DU 5 JANVIER 2026 PORTANT NOMINATION\n"
    "Article 1 : Est nomme M. X au poste de Y.\n"
    "Article 2 : Le present decret sera publie.\n"
)


def test_jo_acte_sans_contenu_entre_deux_titres_est_marque_en_echec(tmp_path: Path, monkeypatch):
    """Régression du 05/08/2026 : 32 actes prod du 02/08/2026 constatés
    `extraction_status="completed"` avec 0 `structure_nodes`/article — deux
    titres d'acte détectés côte à côte (bruit OCR, sommaire mal filtré) ne
    laissent aucun contenu au premier. Il ne doit plus jamais se marquer
    "completed" : "failed" le fait apparaître dans le filtre « Échecs »
    existant de `/editor/ingestion` (`Ingestion.tsx`) plutôt que de le faire
    passer, à tort, pour traité."""
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo/congo-jo-2026-45", MD_JO_ACTE_VIDE_ENTRE_DEUX_TITRES)
    db = RegistryFakeSession()
    _patch_ingest_hierarchy_noop(monkeypatch)
    monkeypatch.setattr(structurer, "minio_service", FakeMinioService())

    result = structure_document(db, data_dir, entry, mistral_client=ValidMetadataMistralClient())

    assert result["statut"] == "structure"
    documents = {d.titre_officiel: d for d in db.added if isinstance(d, LegalDocument)}
    assert len(documents) == 2
    loi = next(d for t, d in documents.items() if "LOI N° 12-2026" in t)
    decret = next(d for t, d in documents.items() if "DECRET N° 45-2026" in t)
    assert loi.extraction_status == "failed"
    assert decret.extraction_status == "completed"


def test_jo_multi_actes_est_idempotent_sur_rejeu(tmp_path: Path, monkeypatch):
    """Rejouer la même entrée ne doit pas dupliquer les actes déjà en base
    (retrouvés par `document_key`, comme le reste du pipeline)."""
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo/congo-jo-2026-43", MD_JO_SOMMAIRE_PUIS_DEUX_ACTES)
    db = RegistryFakeSession()
    _patch_ingest_hierarchy_noop(monkeypatch)
    monkeypatch.setattr(structurer, "minio_service", FakeMinioService())

    result1 = structure_document(db, data_dir, entry, mistral_client=ValidMetadataMistralClient())
    assert result1["statut"] == "structure"
    documents_apres_premier_run = [obj for obj in db.added if isinstance(obj, LegalDocument)]
    assert len(documents_apres_premier_run) == 2

    # Second appel : la probe sur le document_key du 1er acte doit court-circuiter.
    result2 = structure_document(db, data_dir, entry, mistral_client=ValidMetadataMistralClient())
    assert result2["statut"] == "deja_existant"
    documents_apres_second_run = [obj for obj in db.added if isinstance(obj, LegalDocument)]
    assert len(documents_apres_second_run) == 2  # aucun document supplémentaire créé
