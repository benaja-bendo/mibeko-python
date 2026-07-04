"""Tests de l'orchestration du parsing en lot (étage 2), pilotée par le manifeste.

DoD Phase 2 : une commande unique traite tout le carnet sans intervention
humaine ; sorties dans data/pipeline/ avec métriques de qualité par document.
MinerU est injecté (mineru_runner) — aucun réseau, aucun Docker requis ici ;
le triage natif, lui, tourne sur de vrais PDF générés à la volée (fitz).
"""

import json
from pathlib import Path

import fitz
import pytest

from src.acquisition.manifest import Manifest, ManifestEntry, sha256_file
from src.parsing.batch import (
    ParsingError,
    artefact_paths,
    dry_run_report,
    is_already_processed,
    process_entry,
    run_batch,
)

CLEAN_TEXT = "ARTICLE PREMIER : La presente loi regit les relations de travail. " * 20


def _make_pdf(data_dir: Path, rel_path: str, text: str | None) -> Path:
    pdf_path = data_dir / rel_path
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page()
    if text:
        page.insert_textbox(fitz.Rect(50, 50, 545, 792), text, fontsize=9)
    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


def _seed_entry(
    data_dir: Path,
    manifest_key: str,
    entry_id: str,
    rel_path: str,
    text: str | None,
    type_source: str = "journal_officiel",
) -> ManifestEntry:
    pdf_path = _make_pdf(data_dir, rel_path, text)
    entry = ManifestEntry(
        id=entry_id,
        fichier=rel_path,
        sha256=sha256_file(pdf_path),
        size_bytes=pdf_path.stat().st_size,
        type_source=type_source,
        statut="telecharge",
    )
    manifest = Manifest(data_dir / "manifests" / f"{manifest_key}.jsonl")
    manifest.upsert(entry)
    manifest.save()
    return entry


async def _fake_mineru_ok(pdf_path: Path):
    return f"# markdown MinerU pour {pdf_path.name}", '{"pdf_info": []}'


async def _fake_mineru_fail(pdf_path: Path):
    raise ParsingError("panne simulée du serveur MinerU")


async def _fake_mineru_timeout(pdf_path: Path):
    # Reproduit un cas réel constaté (04/07/2026) : httpx.ReadTimeout() n'a
    # aucun message (str(exc) == "") — vu sur un JO 25 pages en MinerU local.
    raise TimeoutError()


def test_document_natif_produit_md_et_metriques_sans_mineru(tmp_path: Path):
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo", "sgg-jo/congo-jo-2026-13", "sources/sgg/JO/congo-jo-2026-13.pdf", CLEAN_TEXT)

    result = process_entry(data_dir, entry, mineru_runner=_fake_mineru_fail)

    assert result["skipped"] is False
    assert result["methode"] == "native"
    paths = artefact_paths(data_dir, entry.id)
    assert paths["md"].is_file()
    assert not paths["json"].is_file()  # pas d'artefact JSON sur le chemin natif
    assert paths["metrics"].is_file()
    metrics = json.loads(paths["metrics"].read_text(encoding="utf-8"))
    assert metrics["source_sha256"] == entry.sha256
    assert metrics["methode"] == "native"
    assert "ARTICLE PREMIER" in paths["md"].read_text(encoding="utf-8")


def test_document_scanne_route_vers_mineru_injecte(tmp_path: Path):
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo", "sgg-jo/scan-2026-1", "sources/sgg/JO/scan-2026-1.pdf", text=None)

    result = process_entry(data_dir, entry, mineru_runner=_fake_mineru_ok)

    assert result["methode"] != "erreur"
    assert result["methode"].startswith("mineru")
    paths = artefact_paths(data_dir, entry.id)
    assert paths["md"].is_file()
    assert paths["json"].is_file()
    assert "markdown MinerU" in paths["md"].read_text(encoding="utf-8")


def test_echec_mineru_est_capture_sans_lever(tmp_path: Path):
    """Un échec MinerU ne fait PAS planter le lot : il est encodé dans les
    métriques ('erreur') pour que run_batch route l'entrée vers statut='erreur'."""
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo", "sgg-jo/scan-2026-2", "sources/sgg/JO/scan-2026-2.pdf", text=None)

    result = process_entry(data_dir, entry, mineru_runner=_fake_mineru_fail)

    assert result["methode"] == "erreur"
    assert "panne simulée" in result["erreur"]
    paths = artefact_paths(data_dir, entry.id)
    assert not paths["md"].is_file()  # aucun artefact partiel écrit sur échec


def test_exception_sans_message_produit_une_erreur_lisible(tmp_path: Path):
    """Une exception à str() vide (ex. httpx.ReadTimeout()) ne doit jamais
    laisser 'erreur': '' dans les métriques — un libellé de repli est utilisé."""
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo", "sgg-jo/scan-timeout", "sources/sgg/JO/scan-timeout.pdf", text=None)

    result = process_entry(data_dir, entry, mineru_runner=_fake_mineru_timeout)

    assert result["methode"] == "erreur"
    assert result["erreur"] == "TimeoutError (sans message)"
    assert result["erreur"] != ""


def test_idempotence_relance_saute_le_document_deja_traite(tmp_path: Path):
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo", "sgg-jo/congo-jo-2026-14", "sources/sgg/JO/congo-jo-2026-14.pdf", CLEAN_TEXT)

    first = process_entry(data_dir, entry, mineru_runner=_fake_mineru_fail)
    assert first["skipped"] is False

    second = process_entry(data_dir, entry, mineru_runner=_fake_mineru_fail)
    assert second["skipped"] is True
    assert is_already_processed(data_dir, entry) is True


def test_force_retraite_malgre_lidempotence(tmp_path: Path):
    data_dir = tmp_path / "data"
    entry = _seed_entry(data_dir, "sgg-jo", "sgg-jo/congo-jo-2026-15", "sources/sgg/JO/congo-jo-2026-15.pdf", CLEAN_TEXT)

    process_entry(data_dir, entry, mineru_runner=_fake_mineru_fail)
    forced = process_entry(data_dir, entry, force=True, mineru_runner=_fake_mineru_fail)
    assert forced["skipped"] is False


def test_run_batch_met_a_jour_le_statut_du_manifeste(tmp_path: Path):
    data_dir = tmp_path / "data"
    _seed_entry(data_dir, "sgg-jo", "sgg-jo/congo-jo-2026-16", "sources/sgg/JO/congo-jo-2026-16.pdf", CLEAN_TEXT)
    _seed_entry(data_dir, "sgg-jo", "sgg-jo/scan-2026-3", "sources/sgg/JO/scan-2026-3.pdf", text=None)

    summary = run_batch(data_dir, mineru_runner=_fake_mineru_ok)

    assert summary["traites"] == 2
    assert summary["erreurs"] == []
    manifest = Manifest(data_dir / "manifests" / "sgg-jo.jsonl")
    assert manifest.get("sgg-jo/congo-jo-2026-16").statut == "parse"
    assert manifest.get("sgg-jo/scan-2026-3").statut == "parse"


def test_run_batch_marque_erreur_et_relance_au_prochain_tour(tmp_path: Path):
    data_dir = tmp_path / "data"
    _seed_entry(data_dir, "sgg-jo", "sgg-jo/scan-2026-4", "sources/sgg/JO/scan-2026-4.pdf", text=None)

    first = run_batch(data_dir, mineru_runner=_fake_mineru_fail)
    assert first["traites"] == 0
    assert len(first["erreurs"]) == 1
    manifest = Manifest(data_dir / "manifests" / "sgg-jo.jsonl")
    assert manifest.get("sgg-jo/scan-2026-4").statut == "erreur"

    # Une entrée en erreur est retentée au tour suivant (pas d'exclusion permanente).
    second = run_batch(data_dir, mineru_runner=_fake_mineru_ok)
    assert second["traites"] == 1
    manifest = Manifest(data_dir / "manifests" / "sgg-jo.jsonl")
    assert manifest.get("sgg-jo/scan-2026-4").statut == "parse"


def test_run_batch_relance_immediate_ne_touche_a_rien(tmp_path: Path):
    """Une fois `statut='parse'`, l'entrée sort du champ du lot suivant :
    relancer ne retraite ni ne re-saute rien (elle n'est même plus regardée)."""
    data_dir = tmp_path / "data"
    _seed_entry(data_dir, "sgg-jo", "sgg-jo/congo-jo-2026-17", "sources/sgg/JO/congo-jo-2026-17.pdf", CLEAN_TEXT)

    run_batch(data_dir, mineru_runner=_fake_mineru_fail)
    md_path = artefact_paths(data_dir, "sgg-jo/congo-jo-2026-17")["md"]
    written_at = md_path.stat().st_mtime_ns

    summary = run_batch(data_dir, mineru_runner=_fake_mineru_fail)
    assert summary["traites"] == 0
    assert summary["sautes"] == 0
    assert md_path.stat().st_mtime_ns == written_at


def test_reset_manuel_vers_telecharge_est_saute_si_deja_a_jour(tmp_path: Path):
    """Si l'entrée redevient éligible (remise manuelle à 'telecharge') mais que
    les artefacts sont déjà à jour pour le même SHA, elle est SAUTÉE — pas de
    second appel MinerU inutile."""
    data_dir = tmp_path / "data"
    _seed_entry(data_dir, "sgg-jo", "sgg-jo/congo-jo-2026-19", "sources/sgg/JO/congo-jo-2026-19.pdf", CLEAN_TEXT)
    run_batch(data_dir, mineru_runner=_fake_mineru_fail)

    manifest = Manifest(data_dir / "manifests" / "sgg-jo.jsonl")
    manifest.get("sgg-jo/congo-jo-2026-19").statut = "telecharge"
    manifest.save()

    summary = run_batch(data_dir, mineru_runner=_fake_mineru_fail)
    assert summary["traites"] == 0
    assert summary["sautes"] == 1
    # Le statut est resynchronisé à 'parse' (le skip ne doit pas laisser
    # 'telecharge' en place indéfiniment, sinon chaque nuit le réexaminerait).
    reloaded = Manifest(data_dir / "manifests" / "sgg-jo.jsonl")
    assert reloaded.get("sgg-jo/congo-jo-2026-19").statut == "parse"


def test_crash_a_mi_lot_persiste_les_documents_deja_traites(tmp_path: Path, monkeypatch):
    """Le manifeste est sauvegardé après CHAQUE document, pas seulement en fin
    de manifeste : un crash inattendu (pas une simple erreur MinerU, celle-là
    déjà gérée) sur le 2e document ne doit pas faire perdre la persistance du
    1er, déjà traité avec succès."""
    import src.parsing.batch as batch_module

    data_dir = tmp_path / "data"
    _seed_entry(data_dir, "sgg-jo", "sgg-jo/a", "sources/sgg/JO/a.pdf", CLEAN_TEXT)
    _seed_entry(data_dir, "sgg-jo", "sgg-jo/b", "sources/sgg/JO/b.pdf", CLEAN_TEXT)
    _seed_entry(data_dir, "sgg-jo", "sgg-jo/c", "sources/sgg/JO/c.pdf", CLEAN_TEXT)

    real_process_entry = batch_module.process_entry
    calls = {"n": 0}

    def crashing_process_entry(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("crash simulé (ex. disque plein, kill -9…)")
        return real_process_entry(*args, **kwargs)

    monkeypatch.setattr(batch_module, "process_entry", crashing_process_entry)

    with pytest.raises(RuntimeError, match="crash simulé"):
        run_batch(data_dir, mineru_runner=_fake_mineru_fail)

    reloaded = Manifest(data_dir / "manifests" / "sgg-jo.jsonl")
    assert reloaded.get("sgg-jo/a").statut == "parse"     # traité avant le crash : persisté
    assert reloaded.get("sgg-jo/c").statut == "telecharge"  # jamais atteint


def test_run_batch_respecte_limit_et_filtre_par_source(tmp_path: Path):
    data_dir = tmp_path / "data"
    _seed_entry(data_dir, "sgg-jo", "sgg-jo/a", "sources/sgg/JO/a.pdf", CLEAN_TEXT)
    _seed_entry(data_dir, "sgg-jo", "sgg-jo/b", "sources/sgg/JO/b.pdf", CLEAN_TEXT)
    _seed_entry(data_dir, "sgg-codes", "sgg-codes/c", "sources/sgg/codes/c.pdf", CLEAN_TEXT)

    summary = run_batch(data_dir, source_key="sgg-jo", limit=1, mineru_runner=_fake_mineru_fail)
    assert summary["traites"] == 1

    codes_manifest = Manifest(data_dir / "manifests" / "sgg-codes.jsonl")
    assert codes_manifest.get("sgg-codes/c").statut == "telecharge"  # jamais touché (filtré)


def test_pdf_manquant_leve_parsing_error(tmp_path: Path):
    data_dir = tmp_path / "data"
    entry = ManifestEntry(
        id="sgg-jo/fantome",
        fichier="sources/sgg/JO/fantome.pdf",
        sha256="0" * 64,
        size_bytes=0,
        type_source="journal_officiel",
        statut="telecharge",
    )
    with pytest.raises(ParsingError):
        process_entry(data_dir, entry, mineru_runner=_fake_mineru_fail)


def test_traites_internationaux_et_lots_prives_hors_perimetre_par_defaut(tmp_path: Path):
    """Décision §9-10 (01-plan.md) : sans --include-hors-perimetre, un traité
    international ou un lot privé n'est ni traité ni même sauté — il est
    compté à part, jamais silencieusement absorbé dans 'traites'/'sautes'."""
    data_dir = tmp_path / "data"
    _seed_entry(data_dir, "sgg-jo", "sgg-jo/congo-jo-2026-20", "sources/sgg/JO/congo-jo-2026-20.pdf", CLEAN_TEXT)
    _seed_entry(
        data_dir, "sgg-traites", "sgg-traites/acte-ohada-x", "sources/sgg/traitées-internationaux/x.pdf",
        CLEAN_TEXT, type_source="traite_international",
    )
    _seed_entry(
        data_dir, "avocat-alban", "avocat-alban/convention-y", "sources/avocat_alban/y.pdf",
        CLEAN_TEXT, type_source="lot_prive",
    )

    summary = run_batch(data_dir, mineru_runner=_fake_mineru_fail)
    assert summary["traites"] == 1  # seul le JO
    assert summary["hors_perimetre"] == 2

    traites_manifest = Manifest(data_dir / "manifests" / "sgg-traites.jsonl")
    assert traites_manifest.get("sgg-traites/acte-ohada-x").statut == "telecharge"  # jamais touché
    assert not artefact_paths(data_dir, "sgg-traites/acte-ohada-x")["md"].is_file()

    # Le JO est déjà passé à 'parse' (traité ci-dessus) : plus rien d'éligible
    # en dry-run par défaut — les deux hors-périmètre restent exclus.
    dry = dry_run_report(data_dir)
    assert dry == []
    dry_avec_hors_perimetre = dry_run_report(data_dir, include_hors_perimetre=True)
    assert {r["id"] for r in dry_avec_hors_perimetre} == {
        "sgg-traites/acte-ohada-x",
        "avocat-alban/convention-y",
    }


def test_include_hors_perimetre_les_reintegre(tmp_path: Path):
    data_dir = tmp_path / "data"
    _seed_entry(
        data_dir, "sgg-traites", "sgg-traites/acte-ohada-z", "sources/sgg/traitées-internationaux/z.pdf",
        CLEAN_TEXT, type_source="traite_international",
    )

    summary = run_batch(data_dir, mineru_runner=_fake_mineru_fail, include_hors_perimetre=True)
    assert summary["traites"] == 1
    assert summary["hors_perimetre"] == 0


def test_dry_run_ne_touche_a_rien(tmp_path: Path):
    data_dir = tmp_path / "data"
    _seed_entry(data_dir, "sgg-jo", "sgg-jo/congo-jo-2026-18", "sources/sgg/JO/congo-jo-2026-18.pdf", CLEAN_TEXT)
    _seed_entry(data_dir, "sgg-jo", "sgg-jo/scan-2026-5", "sources/sgg/JO/scan-2026-5.pdf", text=None)

    report = dry_run_report(data_dir)

    assert {r["id"]: r["methode_prevue"] for r in report} == {
        "sgg-jo/congo-jo-2026-18": "native",
        "sgg-jo/scan-2026-5": "mineru",
    }
    assert not (data_dir / "pipeline").exists()
    manifest = Manifest(data_dir / "manifests" / "sgg-jo.jsonl")
    assert manifest.get("sgg-jo/congo-jo-2026-18").statut == "telecharge"  # inchangé
