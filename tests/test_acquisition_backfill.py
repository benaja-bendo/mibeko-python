"""Tests du rétro-remplissage du manifeste depuis data/sources/ existant."""

import json
from pathlib import Path

from src.acquisition.backfill import backfill
from src.acquisition.manifest import Manifest


def _fake_data_tree(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    (data / "sources" / "sgg" / "JO").mkdir(parents=True)
    (data / "sources" / "sgg" / "codes").mkdir(parents=True)
    (data / "sources" / "avocat_alban").mkdir(parents=True)
    (data / "sources" / "sgg" / "JO" / "congo-jo-2026-13.pdf").write_bytes(b"jo-13")
    (data / "sources" / "sgg" / "JO" / "congo-jo-2023-05-sp.pdf").write_bytes(b"jo-sp")
    (data / "sources" / "sgg" / "JO" / "pas-la-grammaire.pdf").write_bytes(b"bizarre")
    (data / "sources" / "sgg" / "codes" / "congo-code-1975-travail.pdf").write_bytes(b"code")
    # Extension .PDF majuscule (cas réel du lot avocat) + doublon de contenu.
    (data / "sources" / "avocat_alban" / "Convention.PDF").write_bytes(b"conv")
    (data / "sources" / "avocat_alban" / "Convention-bis.pdf").write_bytes(b"conv")
    return data


def test_backfill_cree_les_entrees_avec_provenance(tmp_path: Path):
    data = _fake_data_tree(tmp_path)
    summary = backfill(data)

    assert summary["ajoutes"] == 6
    assert summary["hors_grammaire_jo"] == ["pas-la-grammaire.pdf"]
    assert summary["doublons_sha"] == 1

    jo = Manifest(data / "manifests" / "sgg-jo.jsonl")
    entry = jo.get("sgg-jo/congo-jo-2026-13")
    assert entry.source_url == "https://www.sgg.cg/JO/2026/congo-jo-2026-13.pdf"
    assert entry.jo_annee == 2026 and entry.jo_numero == "13"
    assert entry.retroactif is True
    assert entry.fetched_at is None  # date d'origine inconnue
    assert entry.statut == "telecharge"
    assert entry.fichier == "sources/sgg/JO/congo-jo-2026-13.pdf"

    hors_grammaire = jo.get("sgg-jo/pas-la-grammaire")
    assert hors_grammaire.source_url is None  # pas d'URL inventée

    codes = Manifest(data / "manifests" / "sgg-codes.jsonl")
    assert codes.get("sgg-codes/congo-code-1975-travail").type_source == "code"

    alban = Manifest(data / "manifests" / "avocat-alban.jsonl")
    assert alban.get("avocat-alban/Convention").type_source == "lot_prive"
    # Le second fichier de même contenu (ordre lexicographique) porte l'événement.
    flagged = [
        entry
        for entry in alban.iter_entries()
        if any(e.quoi == "doublon_checksum" for e in entry.evenements)
    ]
    assert len(flagged) == 1


def test_backfill_est_idempotent(tmp_path: Path):
    data = _fake_data_tree(tmp_path)
    backfill(data)
    before = (data / "manifests" / "sgg-jo.jsonl").read_text(encoding="utf-8")

    summary = backfill(data)
    assert summary["ajoutes"] == 0
    assert summary["existants"] == 6
    after = (data / "manifests" / "sgg-jo.jsonl").read_text(encoding="utf-8")
    assert before == after  # aucun churn sur relance


def test_backfill_signale_un_sha_modifie_sans_ecraser(tmp_path: Path):
    data = _fake_data_tree(tmp_path)
    backfill(data)
    # Violation de l'immuabilité : le contenu change sous le même nom.
    (data / "sources" / "sgg" / "JO" / "congo-jo-2026-13.pdf").write_bytes(b"altere")
    backfill(data)

    jo = Manifest(data / "manifests" / "sgg-jo.jsonl")
    entry = jo.get("sgg-jo/congo-jo-2026-13")
    assert any(e.quoi == "sha_changed" for e in entry.evenements)
    # Le SHA d'origine est conservé (l'entrée n'est pas écrasée en silence).
    import hashlib

    assert entry.sha256 == hashlib.sha256(b"jo-13").hexdigest()


def test_classification_tolere_les_noms_nfd_macos(tmp_path: Path):
    """macOS stocke « décret » en NFD : la classification doit matcher quand même."""
    import unicodedata

    data = tmp_path / "data"
    nfd_decret = unicodedata.normalize("NFD", "décret")
    (data / "sources" / "sgg" / nfd_decret).mkdir(parents=True)
    (data / "sources" / "sgg" / nfd_decret / "congo-decret-2020-1.pdf").write_bytes(b"d")
    backfill(data)

    decrets = Manifest(data / "manifests" / "sgg-decrets.jsonl")
    assert len(decrets) == 1
    assert not (data / "manifests" / "divers.jsonl").exists()


def test_manifeste_est_du_jsonl_valide(tmp_path: Path):
    data = _fake_data_tree(tmp_path)
    backfill(data)
    for line in (data / "manifests" / "sgg-jo.jsonl").read_text(encoding="utf-8").splitlines():
        parsed = json.loads(line)
        assert {"id", "fichier", "sha256", "size_bytes", "type_source", "statut"} <= set(parsed)
