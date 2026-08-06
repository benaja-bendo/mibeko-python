"""Tests de `structure_document` (étage 3) : LLM mocké, DB fake (aucun réseau,
aucune Postgres réelle). Se concentre sur la règle mission « jamais d'insertion
partielle » : un curation_flag doit tracer l'échec final, sans document créé.
"""

from pathlib import Path

import pytest

import src.structuration.structurer as structurer
from src.acquisition.manifest import ManifestEntry
from src.db.models import CurationFlag, MediaFile
from src.structuration.structurer import structure_document

CLEAN_MD = "ARTICLE PREMIER : La presente loi regit les relations de travail."

MD_AVEC_MARQUEURS = (
    "[[MIBEKO_PAGE:1]]\n"
    "Loi n° 12-2026 du 3 janvier 2026 portant code du travail\n"
    "ARTICLE PREMIER : La presente loi regit les relations de travail.\n"
    "[[MIBEKO_PAGE:2]]\n"
    "Article 2 : Elle entre en vigueur des sa promulgation.\n"
)


class FakeQuery:
    def __init__(self, result=None):
        self._result = result

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._result


class FakeSession:
    def __init__(self, existing_document=None):
        self._existing_document = existing_document
        self.added = []
        self.committed = False
        self.rolled_back = False

    def query(self, model):
        if model is CurationFlag:
            return FakeQuery(None)
        return FakeQuery(self._existing_document)

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class RaisingMistralClient:
    async def extract_metadata(self, texte, instructions):
        raise ValueError("MISTRAL_API_KEY est vide ou absente.")


class InvalidMetadataMistralClient:
    async def extract_metadata(self, texte, instructions):
        # nature de mauvais type : invalide même après le repli manifeste
        return {"nature": 123, "numero": "12-2026"}


class NatureAbsenteMistralClient:
    async def extract_metadata(self, texte, instructions):
        return {"nature": None, "numero": None}  # Mistral ne sait pas : repli manifeste attendu


class ValidMetadataMistralClient:
    async def extract_metadata(self, texte, instructions):
        return {"nature": "Loi", "numero": "12-2026", "date_signature": None, "date_publication": None, "autorite": None}


class MultiAutoriteMistralClient:
    """Simule un acte cosigné : Mistral renvoie `autorite` en liste (cas réel
    observé sur ~23% des JO du dry-run complet, ex. arrêtés interministériels)."""

    async def extract_metadata(self, texte, instructions):
        return {
            "nature": "Arrêté",
            "numero": "1234",
            "date_signature": None,
            "date_publication": None,
            "autorite": ["La ministre des transports", "Le ministre du budget"],
        }


class FakeMinioService:
    """Double minimal de minio_service : mémorise les appels, aucun réseau réel.

    `fail_on` simule un échec d'upload pour un content_type donné (ex. "application/pdf").
    """

    def __init__(self, fail_on: set | None = None):
        self.fail_on = fail_on or set()
        self.uploads: list[tuple[str, str, str]] = []

    def upload_file(self, object_name, file_path, content_type="application/pdf"):
        self.uploads.append((object_name, file_path, content_type))
        if content_type in self.fail_on:
            return None
        return f"s3://fake-bucket/{object_name}"


class CapturingMistralClient(ValidMetadataMistralClient):
    """Capture le texte d'en-tête réellement envoyé au LLM (métadonnées valides)."""

    def __init__(self):
        self.textes = []

    async def extract_metadata(self, texte, instructions):
        self.textes.append(texte)
        return await super().extract_metadata(texte, instructions)


def _seed_entry(
    data_dir: Path,
    entry_id: str = "sgg-jo/congo-jo-2026-20",
    type_source: str = "journal_officiel",
) -> ManifestEntry:
    md_path = data_dir / "pipeline" / "md" / f"{entry_id}.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(CLEAN_MD, encoding="utf-8")
    return ManifestEntry(
        id=entry_id,
        fichier=f"sources/sgg/JO/{entry_id.split('/')[-1]}.pdf",
        sha256="0" * 64,
        size_bytes=100,
        type_source=type_source,
        statut="parse",
    )


def test_nature_inconnue_du_llm_est_reprise_du_manifeste(tmp_path: Path):
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo/congo-jo-2026-26")
    db = FakeSession()

    result = structure_document(db, data_dir, entry, mistral_client=NatureAbsenteMistralClient(), dry_run=True)

    assert result["statut"] == "structure"


def test_nature_inconnue_sans_repli_manifeste_reste_une_erreur(tmp_path: Path):
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "avocat-alban/doc-prive", type_source="lot_prive")
    db = FakeSession()

    result = structure_document(db, data_dir, entry, mistral_client=NatureAbsenteMistralClient(), dry_run=True)

    assert result["statut"] == "erreur"
    assert "validation du schéma" in result["motif"]


def test_echec_apres_retry_cree_un_curation_flag_llm_et_ne_cree_aucun_document(tmp_path: Path):
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir)
    db = FakeSession()

    result = structure_document(db, data_dir, entry, mistral_client=InvalidMetadataMistralClient())

    assert result["statut"] == "erreur"
    assert result["document_id"] is None
    assert "validation du schéma" in result["motif"]
    assert db.committed is True
    flags = [obj for obj in db.added if isinstance(obj, CurationFlag)]
    assert len(flags) == 1
    assert flags[0].document_id is None
    assert flags[0].source == "llm"
    assert flags[0].severity == "blocking"
    assert entry.id in flags[0].description


def test_echec_appel_mistral_persistant_cree_aussi_un_curation_flag(tmp_path: Path):
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo/congo-jo-2026-21")
    db = FakeSession()

    result = structure_document(db, data_dir, entry, mistral_client=RaisingMistralClient())

    assert result["statut"] == "erreur"
    assert "appel Mistral en échec" in result["motif"]
    flags = [obj for obj in db.added if isinstance(obj, CurationFlag)]
    assert len(flags) == 1


def test_dry_run_ne_cree_aucun_curation_flag_meme_en_echec(tmp_path: Path):
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo/congo-jo-2026-22")
    db = FakeSession()

    result = structure_document(db, data_dir, entry, mistral_client=InvalidMetadataMistralClient(), dry_run=True)

    assert result["statut"] == "erreur"
    assert db.added == []
    assert db.committed is False


def test_markdown_illisible_renvoie_erreur_sans_planter_le_lot(tmp_path: Path):
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo/congo-jo-2026-23")
    md_path = data_dir / "pipeline" / "md" / f"{entry.id}.md"
    md_path.write_bytes(b"\xff\xfe invalide non-utf8 \x80\x81")
    db = FakeSession()

    result = structure_document(db, data_dir, entry, mistral_client=InvalidMetadataMistralClient())

    assert result["statut"] == "erreur"
    assert "lecture/parsing du markdown" in result["motif"]
    assert db.added == []
    assert db.committed is False


def test_markdown_introuvable_ne_cree_aucun_curation_flag(tmp_path: Path):
    data_dir = tmp_path / "data"
    entry = ManifestEntry(
        id="sgg-jo/absent",
        fichier="sources/sgg/JO/absent.pdf",
        sha256="0" * 64,
        size_bytes=100,
        type_source="journal_officiel",
        statut="parse",
    )
    db = FakeSession()

    result = structure_document(db, data_dir, entry, mistral_client=InvalidMetadataMistralClient())

    assert result["statut"] == "erreur"
    assert "markdown introuvable" in result["motif"]
    assert db.added == []
    assert db.committed is False


def test_markdown_uniquement_a_l_emplacement_legacy_est_structure_normalement(tmp_path: Path):
    """Document pré-usine : md à la racine `pipeline/md/<basename>.md` (sans
    sous-dossier source) — le repli legacy doit le retrouver, zéro re-OCR."""
    data_dir = tmp_path / "data"
    entry = ManifestEntry(
        id="sgg-jo/congo-jo-2026-24",
        fichier="sources/sgg/JO/congo-jo-2026-24.pdf",
        sha256="0" * 64,
        size_bytes=100,
        type_source="journal_officiel",
        statut="parse",
    )
    legacy_md = data_dir / "pipeline" / "md" / "congo-jo-2026-24.md"
    legacy_md.parent.mkdir(parents=True, exist_ok=True)
    legacy_md.write_text(CLEAN_MD, encoding="utf-8")
    db = FakeSession()

    result = structure_document(db, data_dir, entry, mistral_client=ValidMetadataMistralClient(), dry_run=True)

    assert result["statut"] == "structure"
    assert result["motif"] is None


def test_markdown_absent_des_deux_emplacements_mentionne_les_deux_chemins(tmp_path: Path):
    data_dir = tmp_path / "data"
    entry = ManifestEntry(
        id="sgg-jo/fantome",
        fichier="sources/sgg/JO/fantome.pdf",
        sha256="0" * 64,
        size_bytes=100,
        type_source="journal_officiel",
        statut="parse",
    )
    db = FakeSession()

    result = structure_document(db, data_dir, entry, mistral_client=InvalidMetadataMistralClient())

    assert result["statut"] == "erreur"
    assert "markdown introuvable" in result["motif"]
    assert str(data_dir / "pipeline" / "md" / "sgg-jo" / "fantome.md") in result["motif"]
    assert str(data_dir / "pipeline" / "md" / "fantome.md") in result["motif"]


def test_autorite_en_liste_est_jointe_en_chaine_unique(tmp_path: Path):
    """Régression dry-run complet (66 JO) : 15/17 échecs venaient d'un acte
    cosigné où Mistral renvoie `autorite` en liste au lieu d'une chaîne —
    la validation du schéma échouait alors qu'aucune information n'était
    réellement en cause."""
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo/congo-jo-2026-29")
    db = FakeSession()

    result = structure_document(db, data_dir, entry, mistral_client=MultiAutoriteMistralClient(), dry_run=True)

    assert result["statut"] == "structure"


def test_structuration_reussie_televerse_pdf_markdown_json_vers_minio(tmp_path: Path, monkeypatch):
    """Régression : structure_document doit téléverser PDF+markdown+JSON vers
    MinIO comme le flux d'upload manuel (src/api/main.py), pas seulement
    enregistrer des chemins locaux à la machine d'ingestion — sinon
    PdfProxyController (Laravel) ne retrouve aucun PDF source."""
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo/congo-jo-2026-27")
    json_path = data_dir / "pipeline" / "json" / f"{entry.id}.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text("{}", encoding="utf-8")
    db = FakeSession()
    fake_minio = FakeMinioService()

    monkeypatch.setattr(structurer, "minio_service", fake_minio)
    monkeypatch.setattr(structurer, "ingest_hierarchy", lambda *args, **kwargs: None)

    result = structure_document(db, data_dir, entry, mistral_client=ValidMetadataMistralClient())

    assert result["statut"] == "structure"
    assert db.committed is True

    media_by_category = {
        obj.file_category: obj for obj in db.added if isinstance(obj, MediaFile)
    }
    assert set(media_by_category) == {"SOURCE_PDF", "EXTRACTION_MARKDOWN", "EXTRACTION_JSON"}
    assert media_by_category["SOURCE_PDF"].mime_type == "application/pdf"
    assert media_by_category["SOURCE_PDF"].file_path.startswith("s3://")
    assert media_by_category["EXTRACTION_MARKDOWN"].mime_type == "text/markdown"
    assert media_by_category["EXTRACTION_JSON"].mime_type == "application/json"
    assert len(fake_minio.uploads) == 3


def test_hierarchie_vide_ne_marque_jamais_completed(tmp_path: Path, monkeypatch):
    """Un texte sans aucun marqueur structurel reconnu (ARTICLE/TITRE) produit
    une hiérarchie vide : `extraction_status` ne doit jamais rester "completed"
    dans ce cas (même défaut constaté dans `journals.py` sur 32 actes JO du
    02/08/2026) — sinon un document sans le moindre article se fait passer
    pour traité."""
    from src.db.models import LegalDocument

    data_dir = tmp_path / "data"
    entry_id = "avocat-alban/doc-prive-vide"
    md_path = data_dir / "pipeline" / "md" / f"{entry_id}.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("Ceci est un simple paragraphe sans structure juridique reconnaissable.", encoding="utf-8")
    entry = ManifestEntry(
        id=entry_id,
        fichier=f"sources/sgg/JO/{entry_id.split('/')[-1]}.pdf",
        sha256="0" * 64,
        size_bytes=100,
        type_source="lot_prive",
        statut="parse",
    )
    db = FakeSession()
    monkeypatch.setattr(structurer, "minio_service", FakeMinioService())
    monkeypatch.setattr(structurer, "ingest_hierarchy", lambda *args, **kwargs: None)

    result = structure_document(db, data_dir, entry, mistral_client=ValidMetadataMistralClient())

    assert result["statut"] == "structure"
    document = next(obj for obj in db.added if isinstance(obj, LegalDocument))
    assert document.extraction_status == "failed"


def test_hierarchie_vide_recupere_le_texte_integral_via_ingest_hierarchy(tmp_path: Path, monkeypatch):
    """Sibling de `test_jo_acte_sans_contenu_entre_deux_titres_est_marque_en_echec`
    (journals.py) pour le chemin plat (non-JO) : à la différence de
    `test_hierarchie_vide_ne_marque_jamais_completed` ci-dessus, qui vérifie
    que `extraction_status` reste "failed", ce test vérifie le contenu
    RÉELLEMENT transmis à `ingest_hierarchy` — le même repli « Texte intégral »
    que `journals.py` (lignes ~268-280) doit rescaper le texte brut en un
    unique noeud ARTICLE plutôt que de laisser le document à zéro article."""
    from src.db.models import LegalDocument

    data_dir = tmp_path / "data"
    entry_id = "avocat-alban/doc-prive-sans-structure"
    md_path = data_dir / "pipeline" / "md" / f"{entry_id}.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(
        "[[MIBEKO_PAGE:1]]\nCeci est un simple paragraphe sans structure juridique reconnaissable.",
        encoding="utf-8",
    )
    entry = ManifestEntry(
        id=entry_id,
        fichier=f"sources/sgg/JO/{entry_id.split('/')[-1]}.pdf",
        sha256="0" * 64,
        size_bytes=100,
        type_source="lot_prive",
        statut="parse",
    )
    db = FakeSession()
    monkeypatch.setattr(structurer, "minio_service", FakeMinioService())
    captured = {}

    def fake_ingest_hierarchy(db, document, hierarchy, **kwargs):
        captured["hierarchy"] = hierarchy

    monkeypatch.setattr(structurer, "ingest_hierarchy", fake_ingest_hierarchy)

    result = structure_document(db, data_dir, entry, mistral_client=ValidMetadataMistralClient())

    assert result["statut"] == "structure"
    document = next(obj for obj in db.added if isinstance(obj, LegalDocument))
    # Le repli sauve le contenu, pas le statut : `extraction_status` reste
    # "failed" (calculé sur la hiérarchie d'origine, avant repli) — c'est
    # `test_hierarchie_vide_ne_marque_jamais_completed` qui couvre ce point.
    assert document.extraction_status == "failed"
    hierarchy = captured["hierarchy"]
    assert len(hierarchy) == 1
    node = hierarchy[0]
    assert node["type"] == "ARTICLE"
    assert node["number"] == "Unique"
    assert node["title"] == "Texte intégral"
    assert "Ceci est un simple paragraphe" in node["content"]
    assert node["page"] == 1


def test_echec_televersement_minio_du_pdf_annule_la_creation_du_document(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo/congo-jo-2026-28")
    db = FakeSession()
    fake_minio = FakeMinioService(fail_on={"application/pdf"})

    monkeypatch.setattr(structurer, "minio_service", fake_minio)
    monkeypatch.setattr(structurer, "ingest_hierarchy", lambda *args, **kwargs: None)

    result = structure_document(db, data_dir, entry, mistral_client=ValidMetadataMistralClient())

    assert result["statut"] == "erreur"
    assert "MinIO" in result["motif"]
    assert db.rolled_back is True
    assert db.committed is False


def test_preambule_trop_court_envoie_le_debut_du_markdown_sans_marqueurs_de_page(tmp_path: Path):
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo/congo-jo-2026-25")
    md_path = data_dir / "pipeline" / "md" / f"{entry.id}.md"
    md_path.write_text(MD_AVEC_MARQUEURS, encoding="utf-8")
    client = CapturingMistralClient()
    db = FakeSession()

    result = structure_document(db, data_dir, entry, mistral_client=client, dry_run=True)

    assert result["statut"] == "structure"
    assert len(client.textes) == 1
    texte_envoye = client.textes[0]
    assert texte_envoye  # non vide malgré le préambule quasi inexistant
    assert "Loi n° 12-2026" in texte_envoye
    assert "[[MIBEKO_PAGE:" not in texte_envoye


class DatedMetadataMistralClient:
    async def extract_metadata(self, texte, instructions):
        return {
            "nature": "Code",
            "numero": None,
            "date_signature": "1975-03-15",
            "date_publication": "1975-04-01",
            "autorite": None,
        }


def test_stock_recoit_consolidation_as_of_sinon_contrainte_db_violee(tmp_path: Path, monkeypatch):
    """Régression (bug trouvé au rechargement complet du 21/07/2026) : la
    contrainte chk_legal_documents_role_logic exige consolidation_as_of NOT NULL
    pour un STOCK — sans elle, TOUTE structuration de code échouait en
    CheckViolation. La date de publication du texte est préférée, puis la
    signature, puis la date du jour (même repli que l'upload manuel)."""
    from src.db.models import LegalDocument

    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-codes/congo-code-1975-travail", type_source="code")
    db = FakeSession()

    monkeypatch.setattr(structurer, "minio_service", FakeMinioService())
    monkeypatch.setattr(structurer, "ingest_hierarchy", lambda *args, **kwargs: None)

    result = structure_document(db, data_dir, entry, mistral_client=DatedMetadataMistralClient())

    assert result["statut"] == "structure"
    document = next(obj for obj in db.added if isinstance(obj, LegalDocument))
    assert document.document_role == "STOCK"
    assert str(document.consolidation_as_of) == "1975-04-01"  # date_publication d'abord


def test_flux_garde_consolidation_as_of_null(tmp_path: Path, monkeypatch):
    """Le pendant FLUX de la contrainte : consolidation_as_of doit rester NULL."""
    from src.db.models import LegalDocument

    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo/congo-jo-2026-30")
    db = FakeSession()

    monkeypatch.setattr(structurer, "minio_service", FakeMinioService())
    monkeypatch.setattr(structurer, "ingest_hierarchy", lambda *args, **kwargs: None)

    result = structure_document(db, data_dir, entry, mistral_client=ValidMetadataMistralClient())

    assert result["statut"] == "structure"
    document = next(obj for obj in db.added if isinstance(obj, LegalDocument))
    assert document.document_role == "FLUX"
    assert document.consolidation_as_of is None


def test_stock_sans_aucune_date_llm_replie_sur_la_date_du_jour(tmp_path: Path, monkeypatch):
    from src.db.models import LegalDocument

    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-codes/congo-code-sans-date", type_source="code")
    db = FakeSession()

    monkeypatch.setattr(structurer, "minio_service", FakeMinioService())
    monkeypatch.setattr(structurer, "ingest_hierarchy", lambda *args, **kwargs: None)

    result = structure_document(db, data_dir, entry, mistral_client=ValidMetadataMistralClient())

    assert result["statut"] == "structure"
    document = next(obj for obj in db.added if isinstance(obj, LegalDocument))
    assert document.document_role == "STOCK"
    assert document.consolidation_as_of is not None


class ListeSommaireMistralClient:
    """Simule le sommaire d'un JO multi-actes : Mistral renvoie une LISTE
    d'objets (un par acte) au lieu de l'objet unique attendu — cas réel observé
    sur 3 JO au rechargement complet du 21/07/2026."""

    async def extract_metadata(self, texte, instructions):
        return [
            {"nature": "loi", "numero": "24-2023", "date_signature": None, "date_publication": None, "autorite": None},
            {"nature": "décret", "numero": "2023-1577", "date_signature": None, "date_publication": None, "autorite": None},
        ]


def test_sommaire_en_liste_replie_sur_jo_numero_annee_du_manifeste(tmp_path: Path, monkeypatch):
    """Régression : une réponse LLM en liste plantait en AttributeError
    ('list' object has no attribute 'get') avec un message trompeur. Pour un JO
    dont le manifeste porte jo_numero/jo_annee, l'identité du journal doit se
    replier sur le manifeste, pas échouer."""
    from src.db.models import LegalDocument

    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo/congo-jo-2023-39")
    entry.jo_numero = 39
    entry.jo_annee = 2023
    db = FakeSession()

    monkeypatch.setattr(structurer, "minio_service", FakeMinioService())
    monkeypatch.setattr(structurer, "ingest_hierarchy", lambda *args, **kwargs: None)

    result = structure_document(db, data_dir, entry, mistral_client=ListeSommaireMistralClient())

    assert result["statut"] == "structure"
    document = next(obj for obj in db.added if isinstance(obj, LegalDocument))
    assert document.titre_officiel == "Journal officiel n° 39-2023"


def test_sommaire_en_liste_sans_jo_numero_echoue_avec_message_clair(tmp_path: Path):
    """Sans jo_numero/jo_annee, l'échec doit être un message explicite, pas un
    AttributeError déguisé en « validation du schéma en échec »."""
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-lois/loi-mystere", type_source="loi")

    db = FakeSession()
    result = structure_document(db, data_dir, entry, mistral_client=ListeSommaireMistralClient(), dry_run=True)

    assert result["statut"] == "erreur"
    assert "liste d'actes" in result["motif"]
    assert "no attribute" not in result["motif"]


def test_structuration_jo_cree_et_rattache_la_fiche_official_journal(tmp_path: Path, monkeypatch):
    """Régression (constat du 21/07/2026) : les JO ingérés par le pipeline
    n'apparaissaient pas dans /editor/journals car aucune fiche
    official_journals n'était créée ni rattachée. Le titre de la fiche vient du
    MANIFESTE (jo_numero/jo_annee), pas du LLM qui titre souvent le JO d'après
    le premier acte de son sommaire."""
    from src.db.models import LegalDocument, OfficialJournal

    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo/congo-jo-2026-31")
    entry.jo_numero = 31
    entry.jo_annee = 2026

    class DatedJoMistralClient:
        async def extract_metadata(self, texte, instructions):
            return {"nature": "Loi", "numero": "12-2026", "date_signature": None,
                    "date_publication": "2026-07-02", "autorite": None}

    db = FakeSession()
    monkeypatch.setattr(structurer, "minio_service", FakeMinioService())
    monkeypatch.setattr(structurer, "ingest_hierarchy", lambda *args, **kwargs: None)

    result = structure_document(db, data_dir, entry, mistral_client=DatedJoMistralClient())

    assert result["statut"] == "structure"
    document = next(obj for obj in db.added if isinstance(obj, LegalDocument))
    journal = next(obj for obj in db.added if isinstance(obj, OfficialJournal))
    assert journal.title == "Journal officiel n° 31-2026"
    assert journal.number == "31"
    assert str(journal.publication_date) == "2026-07-02"
    assert journal.file_path.startswith("s3://")
    assert document.official_journal_id == journal.id
    # L'identité du DOCUMENT vient aussi du manifeste : le LLM (« Loi n° 12-2026 »,
    # premier acte du sommaire) ne doit plus titrer le journal, et un JO ne
    # porte pas de reference_nor (collisions d'unicité avec de vrais actes).
    assert document.titre_officiel == "Journal officiel n° 31-2026"
    assert document.reference_nor is None


def test_structuration_jo_sans_date_ne_cree_pas_de_fiche(tmp_path: Path, monkeypatch):
    """Sans date de publication réelle, AUCUNE fiche n'est créée (date NOT NULL
    en base ; une date inventée serait une fausse provenance) — complétion
    manuelle via /editor/journals."""
    from src.db.models import OfficialJournal

    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo/congo-jo-1959-99")
    entry.jo_numero = 99
    entry.jo_annee = 1959

    db = FakeSession()
    monkeypatch.setattr(structurer, "minio_service", FakeMinioService())
    monkeypatch.setattr(structurer, "ingest_hierarchy", lambda *args, **kwargs: None)

    result = structure_document(db, data_dir, entry, mistral_client=ValidMetadataMistralClient())

    assert result["statut"] == "structure"
    assert not any(isinstance(obj, OfficialJournal) for obj in db.added)


def test_titre_jo_depuis_manifeste_couvre_la_grammaire_sgg():
    """Le titre JO est déterministe depuis le manifeste et reste UNIQUE entre
    variantes d'un même numéro (volumes, spécial, doublon, zéro-padding)."""
    from src.structuration.journals import titre_jo_depuis_manifeste

    def entry(numero, annee):
        from src.acquisition.manifest import ManifestEntry
        return ManifestEntry(id="sgg-jo/x", fichier="sources/x.pdf", sha256="0" * 64,
                             size_bytes=1, type_source="journal_officiel", statut="parse",
                             jo_numero=str(numero), jo_annee=annee)

    assert titre_jo_depuis_manifeste(entry(39, 2023), "congo-jo-2023-39") == "Journal officiel n° 39-2023"
    assert titre_jo_depuis_manifeste(entry(2, 1959), "congo-jo-1959-02") == "Journal officiel n° 2-1959"  # zéro-padding
    assert titre_jo_depuis_manifeste(entry(5, 2025), "congo-jo-2025-5-volume-xxv") == "Journal officiel n° 5-2025 — volume xxv"
    assert titre_jo_depuis_manifeste(entry(7, 1961), "congo-jo-1961-07-sp") == "Journal officiel n° 7-1961 — spécial"
    assert titre_jo_depuis_manifeste(entry(24, 2022), "congo-jo-2022-24-2") == "Journal officiel n° 24-2022 — 2"
    assert titre_jo_depuis_manifeste(entry(1, 2020), "CongoConjSurvivant") == "Journal officiel n° 1-2020 — CongoConjSurvivant"
    # Deux volumes du même numéro → titres distincts → document_key distincts.
    t1 = titre_jo_depuis_manifeste(entry(5, 2025), "congo-jo-2025-5-volume-i")
    t2 = titre_jo_depuis_manifeste(entry(5, 2025), "congo-jo-2025-5-volume-ii")
    assert t1 != t2
