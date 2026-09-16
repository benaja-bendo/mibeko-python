"""Export ingestion_provenances → data/manifests/*.jsonl (mibeko-python#28,
reliquat #23, § 3.7 du plan « boîte de réception ») — contre une vraie base
Postgres, comme test_veille_deposer_jobs.py : l'objet de ce ticket est
justement que Postgres, pas une session fake, porte l'état source.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datetime as _dt

import pytest

from src.acquisition.manifest import Manifest
from src.db.database import SessionLocal
from src.db.models import IngestionProvenance
from src.services.provenance_export import export_provenances_to_jsonl


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


def _cleanup(*manifest_ids: str) -> None:
    cleanup_db = SessionLocal()
    try:
        cleanup_db.query(IngestionProvenance).filter(IngestionProvenance.manifest_id.in_(manifest_ids)).delete(
            synchronize_session=False
        )
        cleanup_db.commit()
    finally:
        cleanup_db.close()


def test_exporte_une_ligne_complete_vers_le_bon_namespace(db, tmp_path):
    db.add(IngestionProvenance(
        manifest_id="sgg-jo/export-complet",
        type_source="journal_officiel",
        fichier="sources/sgg-jo/export-complet.pdf",
        statut="structure",
        size_bytes=1234,
        source_url="https://sgg.cg/jo/export-complet",
        jo_numero="99",
        jo_date=_dt.date(2026, 5, 1),
        jo_annee=2026,
        titre="Journal officiel n° 99 du 1er mai 2026",
        sha256="a" * 64,
        fetched_at=_dt.datetime(2026, 9, 16, 8, 0, 0),
        retroactif=False,
        variantes_multiples=None,
        evenements=[{"quand": "2026-09-16T08:00:00+00:00", "quoi": "telecharge", "par": "MibekoBot/acquire", "detail": None}],
    ))
    db.commit()

    try:
        # Base de dev réelle et partagée : on n'affirme que la présence de CE
        # qu'on vient d'écrire, jamais l'égalité stricte d'un ensemble qui
        # peut porter d'autres lignes.
        resultat = export_provenances_to_jsonl(db, tmp_path)

        assert "sgg-jo/export-complet" in resultat["exportes"].get("sgg-jo", [])
        assert "sgg-jo/export-complet" not in [i for ids in resultat["ignores"].values() for i in ids]

        manifest = Manifest(tmp_path / "sgg-jo.jsonl")
        entree = manifest.get("sgg-jo/export-complet")
        assert entree is not None
        assert entree.fichier == "sources/sgg-jo/export-complet.pdf"
        assert entree.size_bytes == 1234
        assert entree.jo_numero == "99"
        assert entree.jo_date == "2026-05-01"
        assert entree.titre == "Journal officiel n° 99 du 1er mai 2026"
        assert entree.evenements[0].quoi == "telecharge"
    finally:
        _cleanup("sgg-jo/export-complet")


def test_ignore_une_ligne_sans_fichier_ni_size_bytes(db, tmp_path):
    """Ligne écrite avant mibeko-python#28 (dédoublonnage SHA-256 seul) : pas
    de quoi produire une entrée de manifeste exploitable — jamais exportée."""
    db.add(IngestionProvenance(
        manifest_id="depots/avant-28",
        type_source="acte",
        sha256="b" * 64,
    ))
    db.commit()

    try:
        resultat = export_provenances_to_jsonl(db, tmp_path)

        assert "depots/avant-28" not in [i for ids in resultat["exportes"].values() for i in ids]
        assert "depots/avant-28" in resultat["ignores"].get("depots", [])
    finally:
        _cleanup("depots/avant-28")


def test_fusionne_sans_effacer_les_entrees_deja_ecrites_par_acquire(db, tmp_path):
    """Le cœur du ticket : `acquire`/`backfill-manifest` n'écrivent jamais
    IngestionProvenance — un export qui régénérerait le fichier depuis
    Postgres seul effacerait leurs entrées. Doit fusionner, jamais remplacer."""
    from src.acquisition.manifest import ManifestEntry

    manifest = Manifest(tmp_path / "sgg-jo.jsonl")
    manifest.upsert(ManifestEntry(
        id="sgg-jo/deja-la",
        fichier="sources/sgg-jo/deja-la.pdf",
        sha256="c" * 64,
        size_bytes=999,
        type_source="journal_officiel",
    ))
    manifest.save()

    db.add(IngestionProvenance(
        manifest_id="sgg-jo/nouvelle-du-postgres",
        type_source="journal_officiel",
        fichier="sources/sgg-jo/nouvelle-du-postgres.pdf",
        size_bytes=555,
        sha256="d" * 64,
    ))
    db.commit()

    try:
        export_provenances_to_jsonl(db, tmp_path)

        apres = Manifest(tmp_path / "sgg-jo.jsonl")
        assert apres.get("sgg-jo/deja-la") is not None
        assert apres.get("sgg-jo/nouvelle-du-postgres") is not None
    finally:
        _cleanup("sgg-jo/nouvelle-du-postgres")
