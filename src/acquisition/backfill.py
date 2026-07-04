"""Rétro-remplissage du manifeste depuis data/sources/ existant.

Les ~480 PDF acquis manuellement avant l'usine reçoivent une entrée de
manifeste : SHA-256 + taille calculés, URL reconstruite quand c'est possible
(Journaux officiels : grammaire vérifiée en Phase 0), `retroactif=true` et
`fetched_at=null` (la date de récupération d'origine est inconnue).

Idempotent : les entrées existantes sont préservées (événements compris) ;
un SHA qui change sur un id connu — interdit, data/sources/ est immuable —
est signalé par un événement `sha_changed` sans écraser l'entrée.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

from .manifest import Manifest, ManifestEntry, sha256_file
from .sgg import build_jo_url, parse_jo_filename

# Sous-dossier de data/sources/ → (clé de manifeste, type_source).
SOURCE_MAP: list[tuple[str, str, str]] = [
    ("sgg/JO", "sgg-jo", "journal_officiel"),
    ("sgg/hors-series", "sgg-jo", "journal_officiel"),
    ("sgg/codes", "sgg-codes", "code"),
    ("sgg/Lois", "sgg-lois", "loi"),
    ("sgg/décret", "sgg-decrets", "decret"),
    ("sgg/arrete", "sgg-autres", "arrete"),
    ("sgg/circulaire", "sgg-autres", "circulaire"),
    ("sgg/constitution", "sgg-autres", "constitution"),
    ("sgg/textes-officiels", "sgg-autres", "texte_officiel"),
    ("sgg/ccm", "sgg-autres", "ccm"),
    ("sgg/traitées-internationaux", "sgg-traites", "traite_international"),
    ("avocat_alban", "avocat-alban", "lot_prive"),
    ("code-penal", "code-penal", "code"),
]


def _classify(relative_to_sources: Path) -> tuple[str, str]:
    # macOS stocke les noms de fichiers en NFD : normaliser pour que les
    # préfixes accentués (décret, traitées-internationaux) matchent.
    posix = unicodedata.normalize("NFC", relative_to_sources.as_posix())
    for prefix, manifest_key, type_source in SOURCE_MAP:
        if posix.startswith(prefix + "/"):
            return manifest_key, type_source
    return "divers", "inconnu"


def backfill(data_dir: Path, actor: str = "MibekoBot/backfill") -> dict:
    """Parcourt data/sources/ et complète les manifestes. Retourne un résumé."""
    sources = data_dir / "sources"
    manifests_dir = data_dir / "manifests"
    manifests: dict[str, Manifest] = {}
    summary = {"ajoutes": 0, "existants": 0, "doublons_sha": 0, "hors_grammaire_jo": []}
    seen_sha: dict[str, str] = {}

    pdf_paths = sorted(
        [p for p in sources.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"]
    )
    for pdf_path in pdf_paths:
        relative = pdf_path.relative_to(sources)
        manifest_key, type_source = _classify(relative)
        manifest = manifests.setdefault(
            manifest_key, Manifest(manifests_dir / f"{manifest_key}.jsonl")
        )
        entry_id = f"{manifest_key}/{pdf_path.stem}"

        sha = sha256_file(pdf_path)
        existing = manifest.get(entry_id)
        if existing:
            summary["existants"] += 1
            if existing.sha256 != sha:
                existing.add_event(
                    "sha_changed",
                    actor,
                    detail="data/sources/ est immuable : vérifier ce fichier",
                )
            seen_sha.setdefault(sha, entry_id)
            continue

        duplicate_of = seen_sha.get(sha)
        entry = ManifestEntry(
            id=entry_id,
            fichier=str(pdf_path.relative_to(data_dir)),
            sha256=sha,
            size_bytes=pdf_path.stat().st_size,
            type_source=type_source,
            retroactif=True,
            statut="telecharge",
        )
        if type_source == "journal_officiel":
            parsed = parse_jo_filename(pdf_path.name)
            if parsed:
                entry.source_url = build_jo_url(pdf_path.name)
                entry.jo_annee = parsed["annee"]
                entry.jo_numero = parsed["numero"]
            else:
                summary["hors_grammaire_jo"].append(pdf_path.name)
        entry.add_event("backfill", actor, detail="rétro-rempli depuis data/sources/")
        if duplicate_of:
            entry.add_event("doublon_checksum", actor, detail=f"même SHA-256 que {duplicate_of}")
            summary["doublons_sha"] += 1
        manifest.upsert(entry)
        seen_sha.setdefault(sha, entry_id)
        summary["ajoutes"] += 1

    for manifest in manifests.values():
        manifest.save()
    summary["manifestes"] = {
        key: len(manifest) for key, manifest in sorted(manifests.items())
    }
    return summary
