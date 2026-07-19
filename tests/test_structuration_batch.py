"""Tests de l'orchestration de la structuration en lot (étage 3), pilotée par
le manifeste. `structure_document` est mocké — aucun appel réseau Mistral réel,
aucune DB réelle : seul l'enchaînement manifeste ↔ statuts est testé ici.
"""

import uuid
from pathlib import Path

import pytest

from src.acquisition.manifest import Manifest, ManifestEntry, sha256_file
import src.structuration.batch as batch_module
from src.structuration.batch import dry_run_report, run_batch

CLEAN_TEXT = "ARTICLE PREMIER : La presente loi regit les relations de travail."


def _seed_entry(
    data_dir: Path,
    manifest_key: str,
    entry_id: str,
    rel_path: str,
    statut: str = "parse",
    type_source: str = "journal_officiel",
) -> ManifestEntry:
    pdf_path = data_dir / rel_path
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.4 contenu de test")
    entry = ManifestEntry(
        id=entry_id,
        fichier=rel_path,
        sha256=sha256_file(pdf_path),
        size_bytes=pdf_path.stat().st_size,
        type_source=type_source,
        statut=statut,
    )
    manifest = Manifest(data_dir / "manifests" / f"{manifest_key}.jsonl")
    manifest.upsert(entry)
    manifest.save()
    return entry


def _fake_structure_document_ok(db, data_dir, entry, mistral_client=None, dry_run=False):
    return {"statut": "structure", "document_id": uuid.uuid4(), "motif": None}


def _fake_structure_document_erreur(db, data_dir, entry, mistral_client=None, dry_run=False):
    return {"statut": "erreur", "document_id": None, "motif": "validation du schéma en échec : nature manquant"}


def _fake_structure_document_deja_existant(db, data_dir, entry, mistral_client=None, dry_run=False):
    return {"statut": "deja_existant", "document_id": uuid.uuid4(), "motif": None}


class FakeSession:
    """Session minimale : ce module ne touche jamais la DB directement, il se
    contente de la faire transiter à `structure_document` (mocké ici)."""


def test_succes_nominal_passe_le_statut_manifeste_a_structure(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(batch_module, "structure_document", _fake_structure_document_ok)
    data_dir = tmp_path / "data"
    _seed_entry(data_dir, "sgg-jo", "sgg-jo/congo-jo-2026-13", "sources/sgg/JO/congo-jo-2026-13.pdf")

    summary = run_batch(FakeSession(), data_dir)

    assert summary["traites"] == 1
    assert summary["erreurs"] == []
    manifest = Manifest(data_dir / "manifests" / "sgg-jo.jsonl")
    entry = manifest.get("sgg-jo/congo-jo-2026-13")
    assert entry.statut == "structure"
    assert any(e.quoi == "structure" for e in entry.evenements)


def test_echec_de_validation_apres_retry_marque_erreur(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(batch_module, "structure_document", _fake_structure_document_erreur)
    data_dir = tmp_path / "data"
    _seed_entry(data_dir, "sgg-jo", "sgg-jo/congo-jo-2026-14", "sources/sgg/JO/congo-jo-2026-14.pdf")

    summary = run_batch(FakeSession(), data_dir)

    assert summary["traites"] == 0
    assert len(summary["erreurs"]) == 1
    assert "validation du schéma" in summary["erreurs"][0]["erreur"]
    manifest = Manifest(data_dir / "manifests" / "sgg-jo.jsonl")
    entry = manifest.get("sgg-jo/congo-jo-2026-14")
    assert entry.statut == "erreur"
    assert any(e.quoi == "erreur" for e in entry.evenements)


def test_idempotence_document_deja_existant_est_saute_sans_doublon(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(batch_module, "structure_document", _fake_structure_document_deja_existant)
    data_dir = tmp_path / "data"
    _seed_entry(data_dir, "sgg-jo", "sgg-jo/congo-jo-2026-15", "sources/sgg/JO/congo-jo-2026-15.pdf")

    summary = run_batch(FakeSession(), data_dir)

    assert summary["traites"] == 0
    assert summary["deja_existants"] == 1
    assert summary["erreurs"] == []
    manifest = Manifest(data_dir / "manifests" / "sgg-jo.jsonl")
    entry = manifest.get("sgg-jo/congo-jo-2026-15")
    assert entry.statut == "structure"
    assert any(e.quoi == "structure" and e.detail == "déjà existant" for e in entry.evenements)


def test_dry_run_ne_fait_aucune_ecriture_manifeste(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(batch_module, "structure_document", _fake_structure_document_ok)
    data_dir = tmp_path / "data"
    _seed_entry(data_dir, "sgg-jo", "sgg-jo/congo-jo-2026-16", "sources/sgg/JO/congo-jo-2026-16.pdf")

    report = dry_run_report(FakeSession(), data_dir)

    assert report == [{"id": "sgg-jo/congo-jo-2026-16", "statut_prevu": "structure", "motif": None}]
    manifest = Manifest(data_dir / "manifests" / "sgg-jo.jsonl")
    assert manifest.get("sgg-jo/congo-jo-2026-16").statut == "parse"  # inchangé


def test_traites_internationaux_et_lots_prives_hors_perimetre_par_defaut(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(batch_module, "structure_document", _fake_structure_document_ok)
    data_dir = tmp_path / "data"
    _seed_entry(data_dir, "sgg-jo", "sgg-jo/congo-jo-2026-17", "sources/sgg/JO/congo-jo-2026-17.pdf")
    _seed_entry(
        data_dir, "sgg-traites", "sgg-traites/acte-ohada-x", "sources/sgg/traitées-internationaux/x.pdf",
        type_source="traite_international",
    )
    _seed_entry(
        data_dir, "avocat-alban", "avocat-alban/convention-y", "sources/avocat_alban/y.pdf",
        type_source="lot_prive",
    )

    summary = run_batch(FakeSession(), data_dir)

    assert summary["traites"] == 1  # seul le JO
    assert summary["hors_perimetre"] == 2
    traites_manifest = Manifest(data_dir / "manifests" / "sgg-traites.jsonl")
    assert traites_manifest.get("sgg-traites/acte-ohada-x").statut == "parse"  # jamais touché


def test_include_hors_perimetre_les_reintegre(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(batch_module, "structure_document", _fake_structure_document_ok)
    data_dir = tmp_path / "data"
    _seed_entry(
        data_dir, "sgg-traites", "sgg-traites/acte-ohada-z", "sources/sgg/traitées-internationaux/z.pdf",
        type_source="traite_international",
    )

    summary = run_batch(FakeSession(), data_dir, include_hors_perimetre=True)

    assert summary["traites"] == 1
    assert summary["hors_perimetre"] == 0


def test_manifeste_sauvegarde_apres_chaque_document_pas_a_la_fin(tmp_path: Path, monkeypatch):
    """Un crash sur le 2e document ne doit pas faire perdre la persistance du
    1er, déjà traité avec succès (manifeste sauvegardé après CHAQUE entrée)."""
    data_dir = tmp_path / "data"
    _seed_entry(data_dir, "sgg-jo", "sgg-jo/a", "sources/sgg/JO/a.pdf")
    _seed_entry(data_dir, "sgg-jo", "sgg-jo/b", "sources/sgg/JO/b.pdf")
    _seed_entry(data_dir, "sgg-jo", "sgg-jo/c", "sources/sgg/JO/c.pdf")

    calls = {"n": 0}

    def crashing_structure_document(db, data_dir, entry, mistral_client=None, dry_run=False):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("crash simulé (ex. disque plein, kill -9…)")
        return {"statut": "structure", "document_id": uuid.uuid4(), "motif": None}

    monkeypatch.setattr(batch_module, "structure_document", crashing_structure_document)

    with pytest.raises(RuntimeError, match="crash simulé"):
        run_batch(FakeSession(), data_dir)

    reloaded = Manifest(data_dir / "manifests" / "sgg-jo.jsonl")
    assert reloaded.get("sgg-jo/a").statut == "structure"  # traité avant le crash : persisté
    assert reloaded.get("sgg-jo/c").statut == "parse"      # jamais atteint
