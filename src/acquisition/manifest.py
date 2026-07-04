"""Manifeste de provenance de l'usine à textes.

Un manifeste = un fichier JSONL sous data/manifests/ (une ligne = un fichier
acquis). C'est la source de vérité des étages 1-2 (hors base de données) :
provenance (URL, date, SHA-256) et état pipeline de chaque fichier.

Écriture atomique (fichier temporaire + os.replace) et tri stable par id pour
des diffs lisibles. Le fichier PDF lui-même, sous data/sources/, est immuable.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from pydantic import BaseModel, Field

# Statuts pipeline d'une entrée (machine à états, cf. docs/pipeline/01-plan.md).
STATUTS = (
    "telecharge",  # PDF présent dans data/sources/, provenance enregistrée
    "parse",       # artefacts data/pipeline/ produits (triage/MinerU)
    "structure",   # document créé en base (curation_status=draft)
    "erreur",
    "rejete",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ManifestEvent(BaseModel):
    quand: str
    quoi: str
    par: str
    detail: Optional[str] = None


class ManifestEntry(BaseModel):
    id: str                       # ex. "sgg-jo/congo-jo-2026-13"
    fichier: str                  # chemin relatif à data/ (ex. sources/sgg/JO/….pdf)
    sha256: str
    size_bytes: int
    type_source: str              # journal_officiel | code | loi | decret | …
    source_url: Optional[str] = None
    fetched_at: Optional[str] = None   # ISO 8601 UTC ; None si rétro-rempli
    jo_annee: Optional[int] = None
    jo_numero: Optional[str] = None
    jo_date: Optional[str] = None      # complétée plus tard (métadonnées du JO)
    statut: str = "telecharge"
    retroactif: bool = False           # True = fichier acquis avant l'usine
    evenements: list[ManifestEvent] = Field(default_factory=list)

    def add_event(self, quoi: str, par: str, detail: Optional[str] = None) -> None:
        self.evenements.append(
            ManifestEvent(quand=utc_now_iso(), quoi=quoi, par=par, detail=detail)
        )


class Manifest:
    """Un fichier JSONL de manifeste, chargé en mémoire, sauvé atomiquement."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.entries: dict[str, ManifestEntry] = {}
        if self.path.exists():
            with open(self.path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    entry = ManifestEntry.model_validate_json(line)
                    self.entries[entry.id] = entry

    def __len__(self) -> int:
        return len(self.entries)

    def __contains__(self, entry_id: str) -> bool:
        return entry_id in self.entries

    def get(self, entry_id: str) -> Optional[ManifestEntry]:
        return self.entries.get(entry_id)

    def by_sha256(self, sha: str) -> Optional[ManifestEntry]:
        for entry in self.entries.values():
            if entry.sha256 == sha:
                return entry
        return None

    def by_source_url(self, url: str) -> Optional[ManifestEntry]:
        for entry in self.entries.values():
            if entry.source_url == url:
                return entry
        return None

    def upsert(self, entry: ManifestEntry) -> None:
        self.entries[entry.id] = entry

    def save(self) -> None:
        """Écriture atomique, entrées triées par id (diffs stables)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            for entry_id in sorted(self.entries):
                payload = self.entries[entry_id].model_dump(exclude_none=True)
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        os.replace(tmp_path, self.path)

    def iter_entries(self) -> Iterator[ManifestEntry]:
        for entry_id in sorted(self.entries):
            yield self.entries[entry_id]


def known_checksums(manifests_dir: Path) -> dict[str, str]:
    """SHA-256 → id, agrégé sur tous les manifestes (anti re-téléchargement)."""
    checksums: dict[str, str] = {}
    if not manifests_dir.exists():
        return checksums
    for path in sorted(manifests_dir.glob("*.jsonl")):
        manifest = Manifest(path)
        for entry in manifest.iter_entries():
            checksums.setdefault(entry.sha256, entry.id)
    return checksums
