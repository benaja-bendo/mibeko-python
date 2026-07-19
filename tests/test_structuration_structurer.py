"""Tests de `structure_document` (étage 3) : LLM mocké, DB fake (aucun réseau,
aucune Postgres réelle). Se concentre sur la règle mission « jamais d'insertion
partielle » : un curation_flag doit tracer l'échec final, sans document créé.
"""

from pathlib import Path

import pytest

from src.acquisition.manifest import ManifestEntry
from src.db.models import CurationFlag
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
