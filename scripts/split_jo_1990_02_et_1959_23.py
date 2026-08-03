#!/usr/bin/env python3
"""Remédiation 2026-08-03 phase 9 : scinde les deux Journaux Officiels ingérés
« à plat » (jamais découpés en actes) le 2026-08-02 lors du reingest —
DECRET N° 90-042 (congo-jo-1990-02, id prod ac10341a-bd9a-4b77-919b-
ff4c92db85a9) et Avis n°345 (congo-jo-1959-23, id prod 971d2c40-c72a-4c1c-
ba5e-e096b565f9d6) — en leurs actes réels.

Diagnostic (lecture seule, workflow à 2 agents, 2026-08-03) : ces deux
documents FLUX sont en réalité des Journaux Officiels ENTIERS, jamais passés
par `split_official_journal_markdown` — le title_regex existant échoue sur ce
corpus (titres tantôt tout en majuscules, tantôt en casse mixte, jamais
détectés par l'heuristique automatique `suggest-boundaries-md`). Les bornes
RÉELLES ont été vérifiées ligne par ligne (titre + formule de clôture citée
textuellement pour chaque acte, pas une simple correspondance de motif) :
- congo-jo-1990-02.md : 27 actes (2 lois, 1 ordonnance, 24 décrets) + 10
  zones « Actes en abrégé » non décomposées (notices administratives
  courtes groupées par ministère, conformément à la convention déjà en
  vigueur pour ce type de section dans le corpus).
- congo-jo-1959-23.md : 36 actes recensés, dont le Décret n°59-178 (déjà
  son propre document_id, corrigé séparément ce soir pour sa page pivotée —
  EXCLU de cette ingestion, jamais recréé) — 35 nouveaux actes à créer (4
  conventions, 1 loi constitutionnelle, 24 décrets, 6 avis de l'Office des
  Changes) + plusieurs zones assimilées à des « Actes en abrégé » non
  décomposées.

Mécanisme : réutilise TEL QUEL le cœur partagé et déjà éprouvé
`split_and_persist_journal_acts` (`src/structuration/journals.py`, cœur
commun avec `scripts/reingest_flat_journals.py` et le pipeline live) —
aucune nouvelle logique d'insertion DB. Seule la fonction de DÉTECTION des
bornes (`split_official_journal_markdown`, dont le title_regex échoue sur ce
corpus) est substituée par une liste préconstruite et vérifiée manuellement
— substitution locale au module `journals` (le monkeypatch ne s'applique
qu'à l'exécution de CE script, jamais à la fonction partagée elle-même).

Cible : DEV UNIQUEMENT (pas d'option --target prod : ce script crée du
contenu à vérifier dans l'éditeur avant tout envoi en prod, qui se fera par
un script de push scopé séparé une fois la revue faite). Les PDF/markdown
sources sont téléversés sur le MinIO DEV (aucune contrainte d'écriture ici,
contrairement à la PROD) ; un jeu d'objets par Journal officiel, référencé
par les N actes qui en sont issus (même convention que le reste du corpus :
plusieurs documents peuvent partager les mêmes `media_files`).

Usage :
    python scripts/split_jo_1990_02_et_1959_23.py                 # dry-run (aucune écriture, ni DB ni MinIO)
    python scripts/split_jo_1990_02_et_1959_23.py --execute        # écrit sur le DEV local (DB + MinIO dev)

Procédure d'annulation : purement DEV, aucun impact prod. Pour repartir de
zéro : soft-delete des documents créés (leurs id sont listés dans la sortie)
ou reset de la base dev.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.acquisition.manifest import sha256_file  # noqa: E402
from src.api.main import build_object_key  # noqa: E402
import src.structuration.journals as journals_module  # noqa: E402
from src.services.minio_service import minio_service  # noqa: E402

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "5433")

SCRATCH = "/private/tmp/claude-501/-Users-benji-mac-Desktop-Mibeko-mibeko/59d71c11-d3a2-4b14-86bc-cebc4291d902/scratchpad/decret90042-avis345"

# Provenance reprise telle quelle des documents "à plat" existants (mêmes
# sha256/source_url/document_source_id que legal_documents.metadata en prod) —
# jamais réinventée, juste reconduite pour la traçabilité.
JO_1990_02 = {
    "basename": "congo-jo-1990-02",
    "pdf_local": f"{SCRATCH}/congo-jo-1990-02.pdf",
    "md_local": f"{SCRATCH}/congo-jo-1990-02.md",
    "boundaries": f"{SCRATCH}/jo-1990-02.boundaries.validees.json",
    "outdir": f"{SCRATCH}/actes-1990-02",
    "provenance": {
        "source_url": "https://www.sgg.cg/JO/1990/congo-jo-1990-02.pdf",
        "sha256": "bf1319ba3777bcd4e9e78f5cdf9086b68b1f74b0be8bcc02bcb33165c21dee0a",
        "routage_jo_corrige": True,
        "reingestion_split_2026_08_03": True,
    },
    "exclure_index": [],  # aucun acte déjà traité dans ce JO
}

JO_1959_23 = {
    "basename": "congo-jo-1959-23",
    "pdf_local": f"{SCRATCH}/congo-jo-1959-23.pdf",
    "md_local": f"{SCRATCH}/congo-jo-1959-23.md",
    "boundaries": f"{SCRATCH}/jo-1959-23.boundaries.validees.json",
    "outdir": f"{SCRATCH}/actes-1959-23",
    "provenance": {
        "source_url": "https://www.sgg.cg/JO/1959/congo-jo-1959-23.pdf",
        "sha256": "dc7a188f19ad3017a5b887823bad2eb0fd63a84449689a81e02510620e0dc2a2",
        "routage_jo_corrige": True,
        "reingestion_split_2026_08_03": True,
    },
    "exclure_index": [22],  # index 0-based (après ajout des 7 bornes de zones "actes en abrégé" et retri) : Décret n°59-178, déjà son propre document (page pivotée corrigée)
}


def _guard_dev_only() -> None:
    if (DB_HOST, str(DB_PORT)) != ("127.0.0.1", "5433"):
        raise SystemExit(
            f"Refus : ce script ne cible QUE le dev, or l'environnement pointe {DB_HOST}:{DB_PORT}, "
            "pas 127.0.0.1:5433."
        )


def _session_dev():
    db_user = os.getenv("DB_USERNAME", "root")
    db_pass = os.getenv("DB_PASSWORD", "root")
    db_name = os.getenv("DB_DATABASE", "mibeko-db")
    url = f"postgresql://{db_user}:{db_pass}@{DB_HOST}:{DB_PORT}/{db_name}"
    engine = create_engine(url)
    return sessionmaker(bind=engine)()


def lire_actes(jo: Dict[str, Any]) -> List[Dict[str, str]]:
    """Relit les fichiers déjà découpés par `split-compilation-md` : la 1re
    ligne de chaque fichier est le titre brut réel (verbatim OCR, jamais
    nettoyé — même convention que `split_official_journal_markdown`, qui ne
    remet jamais la ligne de titre dans le contenu), le reste est le
    contenu de l'acte."""
    with open(jo["boundaries"]) as f:
        boundaries = json.load(f)
    fichiers = sorted(os.listdir(jo["outdir"]))
    actes = []
    for i, boundary in enumerate(boundaries):
        if i in jo["exclure_index"]:
            if "DEJA TRAITE" not in boundary["title"].upper():
                raise SystemExit(
                    f"Refus : l'index exclu {i} ne correspond plus à l'acte déjà traité "
                    f"(titre actuel : {boundary['title']!r}) — les bornes ont dû être régénérées "
                    "sans mettre à jour exclure_index, vérifier avant de continuer."
                )
            continue
        prefix = f"acte_{i + 1:02d}_"
        matches = [f for f in fichiers if f.startswith(prefix)]
        if len(matches) != 1:
            raise SystemExit(f"Refus : correspondance ambiguë ou introuvable pour {prefix} dans {jo['outdir']} : {matches}")
        with open(os.path.join(jo["outdir"], matches[0])) as f:
            lignes = f.read().split("\n")
        # Même nettoyage que `split_official_journal_markdown` (main.py) avant
        # stockage en `titre_officiel` : retire les décorations markdown de
        # titre ("#", ">", gras/italique) — jamais un "#" littéral en tête de
        # titre ailleurs dans le corpus.
        titre = re.sub(r"^[#>\s]*[*_]{0,3}\s*", "", lignes[0])
        titre = re.sub(r"\s*[*_]{1,3}$", "", titre).strip()
        contenu = "\n".join(lignes[1:])
        from src.api.main import detect_texte_type
        actes.append({"titre": titre, "contenu": contenu, "type": detect_texte_type(titre)})
    return actes


def televerser_source(db, jo: Dict[str, Any], execute: bool) -> Dict[str, Any]:
    """Téléverse le PDF + markdown source du Journal officiel sur MinIO DEV
    (une seule fois, partagé par tous les actes qui en sont issus — même
    convention que `_structure_official_journal_entry`). Aucun impact prod :
    le MinIO ciblé est celui du dev (`minio_service`, configuré par .env)."""
    import uuid

    storage_scope = uuid.uuid5(uuid.NAMESPACE_URL, f"mibeko-jo-source-dev:{jo['basename']}")
    pdf_path = Path(jo["pdf_local"])
    md_path = Path(jo["md_local"])

    if not execute:
        return {
            "pdf_media": {"object_key": "(dry-run, non calculé)", "file_path": "", "original_filename": pdf_path.name, "size_bytes": pdf_path.stat().st_size, "checksum_sha256": sha256_file(pdf_path)},
            "md_media": {"object_key": "(dry-run, non calculé)", "file_path": "", "original_filename": md_path.name, "size_bytes": md_path.stat().st_size, "checksum_sha256": sha256_file(md_path)},
        }

    pdf_object_key = build_object_key("FLUX", None, storage_scope, "source/pdf", pdf_path.name)
    pdf_s3_path = minio_service.upload_file(pdf_object_key, str(pdf_path), "application/pdf")
    if not pdf_s3_path:
        raise SystemExit(f"Refus : échec de téléversement MinIO dev pour {pdf_path}")

    md_object_key = build_object_key("FLUX", None, storage_scope, "extractions/markdown", md_path.name)
    md_s3_path = minio_service.upload_file(md_object_key, str(md_path), "text/markdown")
    if not md_s3_path:
        raise SystemExit(f"Refus : échec de téléversement MinIO dev pour {md_path}")

    return {
        "pdf_media": {"object_key": pdf_object_key, "file_path": pdf_s3_path, "original_filename": pdf_path.name, "size_bytes": pdf_path.stat().st_size, "checksum_sha256": sha256_file(pdf_path)},
        "md_media": {"object_key": md_object_key, "file_path": md_s3_path, "original_filename": md_path.name, "size_bytes": md_path.stat().st_size, "checksum_sha256": sha256_file(md_path)},
    }


def traiter_jo(db, jo: Dict[str, Any], execute: bool) -> Dict[str, Any]:
    actes = lire_actes(jo)
    print(f"  {len(actes)} actes à créer pour {jo['basename']} (après exclusion de {len(jo['exclure_index'])} déjà traité(s)).")

    medias = televerser_source(db, jo, execute)

    if not execute:
        return {"jo": jo["basename"], "statut": "dry-run", "nb_actes": len(actes), "titres": [a["titre"][:70] for a in actes]}

    with open(jo["md_local"]) as f:
        markdown_text_complet = f.read()

    # Substitution locale (portée = cette exécution du script uniquement) :
    # `split_and_persist_journal_acts` appelle `split_official_journal_markdown`
    # en interne — son title_regex échoue sur ce corpus. On lui fait renvoyer
    # notre liste vérifiée manuellement à la place, sans toucher au module
    # partagé ni à la fonction elle-même.
    original_split_fn = journals_module.split_official_journal_markdown
    journals_module.split_official_journal_markdown = lambda _texte: actes
    try:
        created = journals_module.split_and_persist_journal_acts(
            db,
            markdown_text=markdown_text_complet,
            basename=jo["basename"],
            official_journal_id=None,
            date_publication=None,
            curation_status="draft",
            pdf_media=medias["pdf_media"],
            md_media=medias["md_media"],
            json_media=None,
            provenance=jo["provenance"],
        )
    finally:
        journals_module.split_official_journal_markdown = original_split_fn

    return {
        "jo": jo["basename"],
        "statut": "cree",
        "nb_actes": len(created),
        "document_ids": [str(d.id) for d in created],
        "titres": [d.titre_officiel[:70] for d in created],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true", help="Écrit réellement sur le DEV (par défaut : dry-run).")
    args = parser.parse_args()

    _guard_dev_only()
    db = _session_dev()

    print("Cible : DEV uniquement.\n")
    resultats = []
    for jo in (JO_1990_02, JO_1959_23):
        print(f"--- {jo['basename']} ---")
        resultats.append(traiter_jo(db, jo, execute=args.execute))

    if args.execute:
        db.commit()
        print("\n--execute (dev) : modifications validées (COMMIT).")
    else:
        db.rollback()
        print("\nDRY-RUN : aucune écriture (ni DB ni MinIO). Relancer avec --execute pour appliquer sur le dev.")

    print()
    total = 0
    for r in resultats:
        print(f"{r['jo']} : {r['statut']}, {r['nb_actes']} acte(s)")
        for t in r["titres"]:
            print(f"    - {t}")
        total += r["nb_actes"]
    print(f"\nTotal : {total} actes.")


if __name__ == "__main__":
    main()
