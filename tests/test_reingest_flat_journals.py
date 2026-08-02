"""Garde-fou contre la double création d'actes constatée en prod le
2026-08-02 (mémoire `duplicate_documents_methodology_0803`, ticket
`task_5bfba414`) : `reingest_flat_journals.py` traitait indépendamment deux
documents plats FLUX/JO préexistants qui recouvraient en réalité le même PDF
source — un jamais routé (`official_journal_id=None`), l'autre correctement
rattaché — et les scindait chacun en son propre jeu complet d'actes, sans
jamais les comparer entre eux (`split_and_persist_journal_acts` n'est
idempotent que par `document_key`, lui-même scopé au `basename` de CHAQUE
document plat — nécessaire pour ne jamais fusionner deux VRAIS volumes
distincts d'un même numéro de JO). DB fake (aucune Postgres réelle), même
style que `test_structuration_journals_split.py`.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.reingest_flat_journals import (  # noqa: E402
    find_duplicate_source_groups,
    _arreter_si_perimetre_a_risque,
)
from src.db.models import LegalDocument, MediaFile  # noqa: E402

CHECKSUM_PDF_SEPTEMBRE_2025 = "b" * 64


class FakeMediaQuery:
    """Filtre naïf sur (document_id, file_category) — même limitation
    assumée que `FakeQuery` de `test_structuration_journals_split.py` :
    suffisant pour ce que `_media()` interroge réellement."""

    def __init__(self, media_files, document_id=None, file_category=None):
        self._media_files = media_files
        self._document_id = document_id
        self._file_category = file_category

    def filter(self, *criteria, **kwargs):
        document_id = self._document_id
        file_category = self._file_category
        for criterion in criteria:
            text = str(criterion)
            right = getattr(criterion, "right", None)
            value = getattr(right, "value", None)
            if "document_id" in text:
                document_id = value
            elif "file_category" in text:
                file_category = value
        return FakeMediaQuery(self._media_files, document_id, file_category)

    def first(self):
        for media in self._media_files:
            if media.document_id == self._document_id and media.file_category == self._file_category:
                return media
        return None


class FakeSession:
    def __init__(self, media_files):
        self._media_files = media_files

    def query(self, model):
        assert model is MediaFile
        return FakeMediaQuery(self._media_files)


def _flat_jo_document(*, official_journal_id=None, titre="Journal officiel n° 38-2025") -> LegalDocument:
    return LegalDocument(
        id=uuid.uuid4(),
        official_journal_id=official_journal_id,
        type_code="JO",
        document_role="FLUX",
        titre_officiel=titre,
        curation_status="draft",
    )


def _source_pdf(document_id, checksum: str) -> MediaFile:
    return MediaFile(document_id=document_id, file_category="SOURCE_PDF", checksum_sha256=checksum)


def test_detecte_deux_documents_plats_partageant_le_meme_pdf_source():
    """Reproduit le scénario prod du 2026-08-02 : deux documents plats du même
    JO de septembre 2025 (`official_journal_id=9f097996-3c8b-4fd6-a369-f6288c2cfac1`
    en prod), l'un jamais routé, l'autre correctement rattaché — mais tous
    deux référencent le MÊME PDF source (même SHA-256). Sans garde-fou,
    `reingest_flat_journals.py` les aurait scindés indépendamment, créant
    deux copies de chaque acte à 7 minutes d'écart."""
    non_route = _flat_jo_document(official_journal_id=None)
    route = _flat_jo_document(official_journal_id=uuid.UUID("9f097996-3c8b-4fd6-a369-f6288c2cfac1"))
    perimetre = [non_route, route]
    db = FakeSession([
        _source_pdf(non_route.id, CHECKSUM_PDF_SEPTEMBRE_2025),
        _source_pdf(route.id, CHECKSUM_PDF_SEPTEMBRE_2025),
    ])

    doublons = find_duplicate_source_groups(db, perimetre)

    assert list(doublons.keys()) == [CHECKSUM_PDF_SEPTEMBRE_2025]
    assert {d.id for d in doublons[CHECKSUM_PDF_SEPTEMBRE_2025]} == {non_route.id, route.id}


def test_ne_flague_pas_des_volumes_legitimement_distincts():
    """Deux VRAIS volumes distincts d'un même numéro de JO (PDF différents,
    donc checksums différents) ne doivent jamais être flagués : c'est le cas
    normal documenté (JO n°5-2025 acquis en 11 volumes PDF séparés,
    docs/_archive/cloture-audit-ingestion-2026-08-02.md §2)."""
    volume_1 = _flat_jo_document(titre="Journal officiel n° 5-2025 — volume 1")
    volume_2 = _flat_jo_document(titre="Journal officiel n° 5-2025 — volume 2")
    perimetre = [volume_1, volume_2]
    db = FakeSession([
        _source_pdf(volume_1.id, "c" * 64),
        _source_pdf(volume_2.id, "d" * 64),
    ])

    assert find_duplicate_source_groups(db, perimetre) == {}


def test_ignore_les_documents_sans_pdf_source():
    """Un document plat sans PDF source (`sans_pdf`, déjà géré par
    `reingest_one`) ne doit jamais faire planter la détection de doublons ni
    être groupé avec un autre document sans PDF (checksum absent, jamais
    considéré comme une valeur de groupement)."""
    sans_pdf_1 = _flat_jo_document()
    sans_pdf_2 = _flat_jo_document()
    db = FakeSession([])

    assert find_duplicate_source_groups(db, [sans_pdf_1, sans_pdf_2]) == {}


def test_arret_leve_system_exit_sur_doublon_source(capsys):
    """`main()` doit s'arrêter (pas deviner) quand le périmètre contient une
    acquisition dupliquée — même posture que le garde-fou published/STOCK déjà
    en place."""
    non_route = _flat_jo_document(official_journal_id=None)
    route = _flat_jo_document(official_journal_id=uuid.UUID("9f097996-3c8b-4fd6-a369-f6288c2cfac1"))
    perimetre = [non_route, route]
    doublons = {CHECKSUM_PDF_SEPTEMBRE_2025: perimetre}

    try:
        _arreter_si_perimetre_a_risque(perimetre, doublons)
        raised = False
    except SystemExit as exc:
        raised = True
        assert exc.code == 1

    assert raised is True
    sortie = capsys.readouterr().out
    assert "PDF source" in sortie
    assert str(non_route.id) in sortie
    assert str(route.id) in sortie


def test_arret_ne_se_declenche_pas_sur_perimetre_sain():
    """Un périmètre sans intersection published/STOCK ni doublon source ne
    doit jamais lever `SystemExit`."""
    volume_1 = _flat_jo_document(titre="Journal officiel n° 5-2025 — volume 1")
    volume_2 = _flat_jo_document(titre="Journal officiel n° 5-2025 — volume 2")
    _arreter_si_perimetre_a_risque([volume_1, volume_2], {})
