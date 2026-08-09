#!/usr/bin/env python3
"""Purge PHYSIQUE du document hors périmètre RDC — « Code_Penal_Congolais_01 ».

Cible unique et figée : ``db2bd2a1-ddba-4c96-b0e0-b0c7228f9b2b``, Code pénal de
la **République Démocratique du Congo** (Décret du 30 janvier 1940, source
``legal-tools.org``, aucune provenance tracée). Mibeko ne couvre que le
**Congo-Brazzaville** : ce document n'a jamais eu sa place dans le corpus. Il
avait été retiré en soft-delete le 03/08/2026 (`docs/decisions.md`,
`docs/_archive/vague-1-publication-2026-08-03.md`) ; la présente purge va
au-delà et efface les lignes et les objets pour de bon.

⚠️ ÉCART ASSUMÉ AUX RÈGLES DU PROJET. Le `CLAUDE.md` du monorepo classe en
« interdits absolus, même autorisés » le `DELETE` physique et la suppression
dans MinIO. Ce script fait les deux, sur autorisation explicite de
l'utilisateur (09/08/2026), et **uniquement** sur cet UUID. Il ne doit jamais
être généralisé : ne pas le transformer en outil de suppression paramétrable,
ne pas élargir son périmètre. Le ciblage par mot-clé serait d'ailleurs
catastrophique ici — « congolais » remonte des centaines de textes
Congo-Brazzaville parfaitement légitimes (« ordre du mérite congolais »,
« Compagnie Congolaise de Recyclage »…).

Compensation du caractère irréversible : avant toute suppression, le script
écrit une **sauvegarde complète** (lignes en JSON + objets MinIO téléchargés +
`rollback.sql` prêt à rejouer) et refuse d'exécuter si elle est incomplète.
`--restaurer` rejoue cette sauvegarde.

Rayon d'impact mesuré le 09/08/2026 (lecture seule) :

    table                        dev    prod
    legal_documents                1       1
    structure_nodes              191     191
    articles                     271     270
    article_versions             271     270
    curation_flags                23       1
    extraction_runs                1       1
    media_files                    2       2
    document_relations             0       0
    dossier_articles               0       0
    TOTAL                        760     736

    MinIO (dev et prod, bucket mibeko-documents), préfixe dédié :
        domino/legal-documents/stock/code_penal_congolais_01/
            source/pdf/code_penal_congolais_01.pdf              375 625 o
            extractions/markdown/code_penal_congolais_01.md     154 752 o

Usage (dev — défaut) :
    python scripts/purge_document_hors_perimetre_rdc.py                  # dry-run
    python scripts/purge_document_hors_perimetre_rdc.py --execute        # purge le dev local

Usage (prod — À LANCER DEPUIS LE TERMINAL HUMAIN, dump frais obligatoire) :
    python scripts/purge_document_hors_perimetre_rdc.py --target prod              # dry-run, profil PROD_RO_*
    python scripts/purge_document_hors_perimetre_rdc.py --target prod --execute    # profils PROD_RW_DB_* / PROD_RW_MINIO_*

Retour arrière :
    python scripts/purge_document_hors_perimetre_rdc.py --target <cible> --restaurer backups/purge-rdc-<cible>-<horodatage>
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

DOC_ID = "db2bd2a1-ddba-4c96-b0e0-b0c7228f9b2b"
PREFIXE_MINIO = "domino/legal-documents/stock/code_penal_congolais_01/"

# Empreintes attendues — si elles ne correspondent pas, la cible n'est pas
# celle qui a été auditée : on s'arrête plutôt que de supprimer à l'aveugle.
TITRE_ATTENDU = "Code Pénal — Code_Penal_Congolais_01"
SHA256_PDF_ATTENDU = "420706f8d6206ffda898f1c7217d4b99436043d7759df5173217087c86b1eafe"

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "5433")

# Ordre topologique du graphe de clés étrangères — parents avant enfants, donc
# ordre de restauration. Relevé sur la base, pas déduit à l'œil :
#   media_files, structure_nodes  → legal_documents
#   articles                      → legal_documents, structure_nodes
#   extraction_runs               → legal_documents, media_files
#   article_versions              → articles, extraction_runs, media_files, legal_documents
#   curation_flags                → articles, structure_nodes, legal_documents
#   document_relations            → articles, legal_documents
#   dossier_articles              → articles
# La suppression, elle, n'a pas besoin de cet ordre : tout est en ON DELETE
# CASCADE depuis legal_documents.
TABLES = [
    ("legal_documents", "SELECT * FROM legal_documents WHERE id = :d"),
    ("media_files", "SELECT * FROM media_files WHERE document_id = :d"),
    ("structure_nodes", "SELECT * FROM structure_nodes WHERE document_id = :d"),
    ("articles", "SELECT * FROM articles WHERE document_id = :d"),
    ("extraction_runs", "SELECT * FROM extraction_runs WHERE document_id = :d"),
    (
        "article_versions",
        "SELECT * FROM article_versions WHERE article_id IN "
        "(SELECT id FROM articles WHERE document_id = :d)",
    ),
    (
        "curation_flags",
        "SELECT * FROM curation_flags WHERE document_id = :d OR article_id IN "
        "(SELECT id FROM articles WHERE document_id = :d)",
    ),
    (
        "document_relations",
        "SELECT * FROM document_relations WHERE source_doc_id = :d OR target_doc_id = :d",
    ),
    (
        "dossier_articles",
        "SELECT * FROM dossier_articles WHERE article_id IN "
        "(SELECT id FROM articles WHERE document_id = :d)",
    ),
]


def _guard_dev_only() -> None:
    if (DB_HOST, str(DB_PORT)) != ("127.0.0.1", "5433"):
        raise SystemExit(
            f"Refus : cible dev demandée mais l'environnement pointe {DB_HOST}:{DB_PORT}, "
            "pas 127.0.0.1:5433. Utiliser --target prod pour la production."
        )


def _engine_dev():
    user = os.getenv("DB_USERNAME", "root")
    mdp = os.getenv("DB_PASSWORD", "root")
    base = os.getenv("DB_DATABASE", "mibeko-db")
    return create_engine(f"postgresql://{user}:{mdp}@{DB_HOST}:{DB_PORT}/{base}")


def _engine_prod_lecture():
    from src.db.prod_readonly import (
        SQLSTATE_LECTURE_SEULE,
        assert_read_only,
        charger_cible,
        creer_engine,
    )

    cible = charger_cible()
    engine = creer_engine(cible)
    sqlstate = assert_read_only(engine)
    if sqlstate != SQLSTATE_LECTURE_SEULE:
        raise SystemExit(
            f"Refus : lecture seule non prouvée par SQLSTATE {SQLSTATE_LECTURE_SEULE} "
            f"(obtenu : {sqlstate})."
        )
    print(f"Préflight PROD : lecture seule prouvée ({cible.resume()}).")
    return engine


def _engine_prod_ecriture(engine_ro):
    from src.promotion.push_corpus import (
        CibleProdAmbigue,
        ConfigurationProdManquante,
        charger_cible_ecriture,
    )

    try:
        engine_rw = charger_cible_ecriture()
    except (ConfigurationProdManquante, CibleProdAmbigue) as exc:
        raise SystemExit(f"Refus : {exc}")

    with engine_rw.connect() as rw, engine_ro.connect() as ro:
        sql = "SELECT count(*) FROM legal_documents"
        if rw.execute(text(sql)).scalar() != ro.execute(text(sql)).scalar():
            raise SystemExit(
                "Refus : la cible RW (PROD_RW_DB_*) ne répond pas comme la cible RO "
                "(PROD_RO_DB_*) — les deux profils ne visent pas la même base."
            )
    return engine_rw


def _clients_minio(cible: str, ecriture: bool):
    """Renvoie (client, bucket). En prod, lecture et écriture ont deux profils."""
    if cible == "dev":
        from src.promotion.push_corpus import creer_client_minio_source

        return creer_client_minio_source(), os.getenv("MINIO_BUCKET", "mibeko-documents")
    if ecriture:
        from src.promotion.push_corpus import creer_client_minio_ecriture

        return (
            creer_client_minio_ecriture(),
            os.getenv("PROD_RW_MINIO_BUCKET", "mibeko-documents"),
        )
    from src.db.prod_readonly import creer_client_minio_diagnostic

    return creer_client_minio_diagnostic()


def _verifier_identite(cnx) -> dict:
    ligne = cnx.execute(
        text(
            "SELECT titre_officiel, document_key, curation_status, deleted_at "
            "FROM legal_documents WHERE id = :d"
        ),
        {"d": DOC_ID},
    ).fetchone()
    if ligne is None:
        raise SystemExit(f"Document {DOC_ID} absent : rien à purger (déjà fait ?).")

    titre, cle, statut, supprime_le = ligne
    if titre != TITRE_ATTENDU:
        raise SystemExit(
            f"Refus : titre inattendu {titre!r} (attendu {TITRE_ATTENDU!r}). "
            "La cible n'est pas le document audité."
        )
    if statut == "published":
        raise SystemExit(
            "Refus : le document est publié. Le dépublier par l'API Laravel avant toute purge."
        )

    sha = cnx.execute(
        text(
            "SELECT checksum_sha256 FROM media_files "
            "WHERE document_id = :d AND file_category = 'SOURCE_PDF'"
        ),
        {"d": DOC_ID},
    ).scalar()
    if sha != SHA256_PDF_ATTENDU:
        raise SystemExit(
            f"Refus : SHA-256 du PDF source inattendu ({sha}). Attendu {SHA256_PDF_ATTENDU}."
        )

    # Garde-fou : dossier_echeances passerait en SET NULL sans être sauvegardé.
    orphelines = cnx.execute(
        text(
            "SELECT count(*) FROM dossier_echeances WHERE basis_article_id IN "
            "(SELECT id FROM articles WHERE document_id = :d)"
        ),
        {"d": DOC_ID},
    ).scalar_one()
    if orphelines:
        raise SystemExit(
            f"Refus : {orphelines} échéance(s) de dossier s'appuient sur un article de ce "
            "document et seraient silencieusement détachées (SET NULL). Hors périmètre — "
            "les traiter d'abord."
        )

    print(f"Identité confirmée : {titre!r}")
    print(f"    clé {cle} · statut {statut} · deleted_at {supprime_le or 'NON (vivant)'}")
    return {"titre": titre, "cle": cle, "statut": statut}


def _sauvegarder(cnx, client_minio, bucket: str, dossier: Path) -> dict:
    dossier.mkdir(parents=True, exist_ok=True)
    (dossier / "objets").mkdir(exist_ok=True)

    compteurs, morceaux_sql = {}, []
    for table, sql in TABLES:
        lignes = [
            r[0]
            for r in cnx.execute(
                text(sql.replace("SELECT *", "SELECT row_to_json(t)", 1).replace(
                    f"FROM {table}", f"FROM {table} t", 1
                )),
                {"d": DOC_ID},
            ).fetchall()
        ]
        compteurs[table] = len(lignes)
        (dossier / f"{table}.json").write_text(
            json.dumps(lignes, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        if lignes:
            charge = json.dumps(lignes, ensure_ascii=False, default=str).replace("'", "''")
            morceaux_sql.append(
                f"-- {table} : {len(lignes)} ligne(s)\n"
                f"INSERT INTO {table} SELECT * FROM "
                f"jsonb_populate_recordset(NULL::{table}, '{charge}'::jsonb);"
            )

    (dossier / "rollback.sql").write_text(
        "-- Retour arrière de la purge RDC — à rejouer dans une transaction.\n"
        "-- Les objets MinIO se restaurent par --restaurer (ce SQL ne couvre que la base).\n"
        "BEGIN;\n\n" + "\n\n".join(morceaux_sql) + "\n\nCOMMIT;\n",
        encoding="utf-8",
    )

    objets = []
    for obj in client_minio.list_objects(bucket, prefix=PREFIXE_MINIO, recursive=True):
        destination = dossier / "objets" / obj.object_name.removeprefix(PREFIXE_MINIO)
        destination.parent.mkdir(parents=True, exist_ok=True)
        client_minio.fget_object(bucket, obj.object_name, str(destination))
        taille = destination.stat().st_size
        if taille != obj.size:
            raise SystemExit(
                f"Refus : {obj.object_name} téléchargé incomplet ({taille}/{obj.size} octets)."
            )
        objets.append({"cle": obj.object_name, "taille": obj.size})

    (dossier / "manifeste.json").write_text(
        json.dumps(
            {
                "document_id": DOC_ID,
                "prefixe_minio": PREFIXE_MINIO,
                "bucket": bucket,
                "lignes": compteurs,
                "objets": objets,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {"lignes": compteurs, "objets": objets}


def _restaurer(engine, client_minio, bucket: str, dossier: Path) -> None:
    """Rejoue la sauvegarde depuis les JSON, dans l'ordre topologique de TABLES.

    Les JSON font foi, pas `rollback.sql` : une seule source d'ordre. Le JSON
    est passé en paramètre lié — ni échappement de quotes, ni horodatage
    « 18:22:59 » pris pour un paramètre nommé, ni `%` interprété.
    """
    manifeste = json.loads((dossier / "manifeste.json").read_text(encoding="utf-8"))

    with engine.begin() as cnx:
        deja = cnx.execute(
            text("SELECT count(*) FROM legal_documents WHERE id = :d"), {"d": DOC_ID}
        ).scalar_one()
        if deja:
            raise SystemExit("Refus : le document est déjà présent en base — rien à restaurer.")

        total = 0
        for table, _ in TABLES:
            lignes = json.loads((dossier / f"{table}.json").read_text(encoding="utf-8"))
            if not lignes:
                continue
            resultat = cnx.exec_driver_sql(
                f"INSERT INTO {table} SELECT * FROM "
                f"jsonb_populate_recordset(NULL::{table}, %s::jsonb)",
                (json.dumps(lignes, ensure_ascii=False),),
            )
            total += resultat.rowcount
            print(f"    {table:<24} {resultat.rowcount:>5} ligne(s)")

    for objet in manifeste["objets"]:
        source = dossier / "objets" / objet["cle"].removeprefix(PREFIXE_MINIO)
        client_minio.fput_object(bucket, objet["cle"], str(source))
        print(f"    MinIO restauré : {objet['cle']}")

    attendu = sum(manifeste["lignes"].values())
    if total != attendu:
        raise SystemExit(f"ALERTE : {total} lignes restaurées au lieu de {attendu} — incident.")
    print(f"\nRestauration terminée : {total} lignes et {len(manifeste['objets'])} objets remis en place.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--target", choices=["dev", "prod"], default="dev")
    parser.add_argument("--execute", action="store_true", help="Écrit réellement (défaut : dry-run).")
    parser.add_argument("--restaurer", metavar="DOSSIER", help="Rejoue une sauvegarde.")
    parser.add_argument("--sauvegarde", metavar="DOSSIER", help="Où écrire la sauvegarde.")
    args = parser.parse_args()

    engine_ro = _engine_dev() if args.target == "dev" else _engine_prod_lecture()
    if args.target == "dev":
        _guard_dev_only()

    if args.restaurer:
        engine = engine_ro if args.target == "dev" else _engine_prod_ecriture(engine_ro)
        client, bucket = _clients_minio(args.target, ecriture=True)
        _restaurer(engine, client, bucket, Path(args.restaurer))
        return

    with engine_ro.connect() as cnx:
        _verifier_identite(cnx)

        horodatage = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dossier = Path(
            args.sauvegarde
            or Path(__file__).resolve().parents[1]
            / "backups"
            / f"purge-rdc-{args.target}-{horodatage}"
        )
        client_ro, bucket_ro = _clients_minio(args.target, ecriture=False)
        print(f"\nSauvegarde préalable → {dossier}")
        bilan = _sauvegarder(cnx, client_ro, bucket_ro, dossier)

    total = sum(bilan["lignes"].values())
    print("\nRayon d'impact :")
    for table, n in bilan["lignes"].items():
        print(f"    {table:<24} {n:>5}")
    print(f"    {'TOTAL lignes':<24} {total:>5}")
    print(f"    {'objets MinIO':<24} {len(bilan['objets']):>5}")

    if not bilan["objets"]:
        print("\n  Note : aucun objet sous le préfixe MinIO (déjà purgé ?).")

    if not args.execute:
        print(
            f"\nDRY-RUN ({args.target}) : aucune suppression. La sauvegarde est écrite et "
            "réutilisable. Relancer avec --execute pour purger."
        )
        return

    if args.target == "prod":
        print(
            f"\n  PRODUCTION : {total} lignes et {len(bilan['objets'])} objets vont être "
            "DÉFINITIVEMENT supprimés."
        )
        print(f"  Retour arrière : --restaurer {dossier}")
        if input("  Taper PRODUCTION pour confirmer : ").strip() != "PRODUCTION":
            raise SystemExit("Annulé.")

    engine_rw = engine_ro if args.target == "dev" else _engine_prod_ecriture(engine_ro)
    client_rw, bucket_rw = _clients_minio(args.target, ecriture=True)

    for objet in bilan["objets"]:
        client_rw.remove_object(bucket_rw, objet["cle"])
        print(f"  MinIO supprimé : {objet['cle']}")

    with engine_rw.begin() as cnx:
        supprimes = cnx.execute(
            text("DELETE FROM legal_documents WHERE id = :d"), {"d": DOC_ID}
        ).rowcount
        if supprimes != 1:
            raise SystemExit(
                f"Refus : {supprimes} ligne(s) supprimée(s) dans legal_documents au lieu de 1 "
                "— transaction annulée."
            )

    with engine_rw.connect() as cnx:
        restant = sum(
            cnx.execute(
                text(sql.replace("SELECT *", "SELECT count(*)", 1)), {"d": DOC_ID}
            ).scalar_one()
            for _, sql in TABLES
        )
    if restant:
        raise SystemExit(f"ALERTE : {restant} ligne(s) subsistent après purge — incident.")

    print(
        f"\n--execute ({args.target}) : purge validée. {total} lignes et "
        f"{len(bilan['objets'])} objets supprimés, 0 résiduel."
    )
    print(f"Retour arrière possible depuis : {dossier}")


if __name__ == "__main__":
    main()
