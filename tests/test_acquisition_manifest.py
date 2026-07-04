"""Tests du manifeste de provenance (JSONL, écriture atomique, idempotence)."""

from pathlib import Path

from src.acquisition.manifest import Manifest, ManifestEntry, known_checksums


def _entry(entry_id: str = "sgg-jo/congo-jo-2026-13", sha: str = "a" * 64) -> ManifestEntry:
    return ManifestEntry(
        id=entry_id,
        fichier="sources/sgg/JO/congo-jo-2026-13.pdf",
        sha256=sha,
        size_bytes=123,
        type_source="journal_officiel",
        source_url="https://www.sgg.cg/JO/2026/congo-jo-2026-13.pdf",
        jo_annee=2026,
        jo_numero="13",
    )


def test_roundtrip_sauvegarde_et_rechargement(tmp_path: Path):
    path = tmp_path / "sgg-jo.jsonl"
    manifest = Manifest(path)
    entry = _entry()
    entry.add_event("telecharge", "test")
    manifest.upsert(entry)
    manifest.save()

    reloaded = Manifest(path)
    assert len(reloaded) == 1
    got = reloaded.get("sgg-jo/congo-jo-2026-13")
    assert got is not None
    assert got.sha256 == "a" * 64
    assert got.jo_annee == 2026
    assert got.evenements[0].quoi == "telecharge"


def test_ecriture_atomique_sans_fichier_temporaire_residuel(tmp_path: Path):
    path = tmp_path / "m.jsonl"
    manifest = Manifest(path)
    manifest.upsert(_entry())
    manifest.save()
    assert path.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_tri_stable_par_id(tmp_path: Path):
    path = tmp_path / "m.jsonl"
    manifest = Manifest(path)
    manifest.upsert(_entry("sgg-jo/zzz", sha="b" * 64))
    manifest.upsert(_entry("sgg-jo/aaa", sha="c" * 64))
    manifest.save()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert '"sgg-jo/aaa"' in lines[0]
    assert '"sgg-jo/zzz"' in lines[1]


def test_recherche_par_sha_et_url(tmp_path: Path):
    manifest = Manifest(tmp_path / "m.jsonl")
    manifest.upsert(_entry())
    assert manifest.by_sha256("a" * 64) is not None
    assert manifest.by_sha256("f" * 64) is None
    assert manifest.by_source_url("https://www.sgg.cg/JO/2026/congo-jo-2026-13.pdf") is not None


def test_known_checksums_agrege_tous_les_manifestes(tmp_path: Path):
    m1 = Manifest(tmp_path / "sgg-jo.jsonl")
    m1.upsert(_entry())
    m1.save()
    m2 = Manifest(tmp_path / "sgg-codes.jsonl")
    m2.upsert(_entry("sgg-codes/congo-code-1975-travail", sha="d" * 64))
    m2.save()
    checksums = known_checksums(tmp_path)
    assert checksums["a" * 64] == "sgg-jo/congo-jo-2026-13"
    assert checksums["d" * 64] == "sgg-codes/congo-code-1975-travail"


def test_upsert_preserve_les_evenements_existants(tmp_path: Path):
    path = tmp_path / "m.jsonl"
    manifest = Manifest(path)
    entry = _entry()
    entry.add_event("telecharge", "test")
    manifest.upsert(entry)
    manifest.save()

    reloaded = Manifest(path)
    existing = reloaded.get(entry.id)
    existing.add_event("doublon_checksum", "test")
    reloaded.save()

    final = Manifest(path)
    assert [e.quoi for e in final.get(entry.id).evenements] == [
        "telecharge",
        "doublon_checksum",
    ]
