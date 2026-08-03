#!/usr/bin/env python3
"""Remédiation 2026-08-03 phase 9 (suite) : retire (soft-delete) les deux
documents FLUX "à plat" — DECRET N° 90-042 (congo-jo-1990-02) et Avis n°345
(congo-jo-1959-23) — remplacés par leurs 79 actes correctement scindés,
créés et vérifiés sur dev (cf. scripts/split_jo_1990_02_et_1959_23.py).

Pourquoi retirer AVANT de pousser les 79 actes (plutôt qu'après, comme prévu
initialement) : `push-corpus` écarte un document dont le SHA-256 du PDF
source existe déjà en cible (`construire_plan`, "source déjà en production")
— exactement le cas ici, puisque les 79 actes partagent volontairement le
même PDF source que les 2 documents à plat. Tant que ces 2 documents à plat
restent en base (même soft-supprimés au sens `deleted_at`, TANT QUE la
requête de collision ne filtre QUE `deleted_at IS NULL` — vérifié dans
`charger_etat_cible`/`charger_documents_source`, c'est bien le cas), le
push des 79 actes serait skippé à 100% (dry-run vérifié : 0 document
poussable avant ce retrait). Aucune perte de disponibilité publique : les
deux documents à plat sont encore `curation_status='draft'`, jamais publiés
— vérifié avant ce script, revérifié comme garde-fou avant écriture.

Aucune écriture MinIO (le PDF/markdown des 2 documents à plat restent
inchangés sur MinIO — seul `legal_documents.deleted_at` est modifié).

Usage (dev — défaut, sert surtout à valider les requêtes, ces 2 ids
n'existent que dans le reingest prod du 2026-08-02) :
    python scripts/retire_flat_jo_documents.py                    # dry-run
    python scripts/retire_flat_jo_documents.py --execute          # écrit sur le dev local

Usage (prod) :
    python scripts/retire_flat_jo_documents.py --target prod                  # dry-run, profil PROD_RO_*
    python scripts/retire_flat_jo_documents.py --target prod --execute        # écrit en PROD, profil PROD_RW_DB_*

Garde-fous : mêmes profils/vérifications que les scripts précédents (dump
frais requis avant tout --execute --target prod, saisie « PRODUCTION »
exigée). Refuse si l'un des deux documents est déjà publié ou déjà supprimé.

Procédure d'annulation : `deleted_at` remis à NULL restaure l'état antérieur
à l'identique (aucune ligne physiquement supprimée, aucun article touché).
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy import text  # noqa: E402

from src.db.models import LegalDocument  # noqa: E402

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "5433")

DOCUMENTS_A_RETIRER = [
    ("ac10341a-bd9a-4b77-919b-ff4c92db85a9", "DECRET N° 90-042 (congo-jo-1990-02, à plat) — remplacé par 27 actes"),
    ("971d2c40-c72a-4c1c-ba5e-e096b565f9d6", "Avis n°345 (congo-jo-1959-23, à plat) — remplacé par 35 actes (+ Décret 59-178 déjà séparé)"),
]


def _guard_dev_only() -> None:
    if (DB_HOST, str(DB_PORT)) != ("127.0.0.1", "5433"):
        raise SystemExit(
            f"Refus : cible dev demandée mais l'environnement pointe {DB_HOST}:{DB_PORT}, "
            "pas 127.0.0.1:5433. Utiliser --target prod pour la production."
        )


def _session_dev():
    db_user = os.getenv("DB_USERNAME", "root")
    db_pass = os.getenv("DB_PASSWORD", "root")
    db_name = os.getenv("DB_DATABASE", "mibeko-db")
    url = f"postgresql://{db_user}:{db_pass}@{DB_HOST}:{DB_PORT}/{db_name}"
    engine = create_engine(url)
    return sessionmaker(bind=engine)(), None


def _session_prod_readonly():
    from src.db.prod_readonly import SQLSTATE_LECTURE_SEULE, assert_read_only, charger_cible, creer_engine

    cible = charger_cible()
    engine = creer_engine(cible)
    sqlstate = assert_read_only(engine)
    if sqlstate != SQLSTATE_LECTURE_SEULE:
        raise SystemExit(f"Refus : lecture seule non prouvée par SQLSTATE {SQLSTATE_LECTURE_SEULE} (obtenu : {sqlstate}).")
    print(f"Préflight PROD : lecture seule prouvée ({cible.resume()}).")
    return sessionmaker(bind=engine)(), engine


def _session_prod_ecriture(engine_ro):
    from src.promotion.push_corpus import CibleProdAmbigue, ConfigurationProdManquante, charger_cible_ecriture

    try:
        engine_rw = charger_cible_ecriture()
    except (ConfigurationProdManquante, CibleProdAmbigue) as exc:
        raise SystemExit(f"Refus : {exc}")

    with engine_rw.connect() as cnx_rw, engine_ro.connect() as cnx_ro:
        compte_sql = "select count(*) from legal_documents"
        if cnx_rw.execute(text(compte_sql)).scalar() != cnx_ro.execute(text(compte_sql)).scalar():
            raise SystemExit(
                "Refus : la cible RW (PROD_RW_DB_*) ne répond pas comme la cible RO "
                "(PROD_RO_DB_*) — les deux profils ne visent pas la même base."
            )
    return sessionmaker(bind=engine_rw)()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true", help="Écrit réellement (par défaut : dry-run, aucune écriture).")
    parser.add_argument("--target", choices=["dev", "prod"], default="dev", help="Cible (défaut : dev).")
    args = parser.parse_args()

    engine_ro = None
    if args.target == "dev":
        _guard_dev_only()
        db, _ = _session_dev()
    else:
        db, engine_ro = _session_prod_readonly()

    documents = []
    for doc_id, label in DOCUMENTS_A_RETIRER:
        doc = db.query(LegalDocument).filter(LegalDocument.id == doc_id).first()
        if doc is None:
            raise SystemExit(f"Refus : document introuvable {doc_id} ({label}).")
        if doc.deleted_at is not None:
            raise SystemExit(f"Refus : document {doc_id} déjà supprimé (deleted_at={doc.deleted_at}) — état inattendu.")
        if doc.curation_status == "published":
            raise SystemExit(
                f"Refus : document {doc_id} ({label}) est PUBLIÉ — hors garde-fou sans validation humaine explicite."
            )
        documents.append((doc, label))
        print(f"  {doc_id}  statut={doc.curation_status}  {label}")

    print(f"\nCible : {args.target}. {len(documents)} document(s) à retirer (soft-delete).")

    if not args.execute:
        print("\nDRY-RUN : aucune écriture. Relancer avec --execute pour appliquer.")
        return

    if args.target == "prod":
        print("\n  Ces 2 documents PROD (aucun publié) vont être marqués deleted_at (soft-delete).")
        saisie = input("Taper PRODUCTION pour confirmer : ").strip()
        if saisie != "PRODUCTION":
            print("Annulé.")
            sys.exit(1)
        db = _session_prod_ecriture(engine_ro)
        documents = []
        for doc_id, label in DOCUMENTS_A_RETIRER:
            doc = db.query(LegalDocument).filter(LegalDocument.id == doc_id).first()
            documents.append((doc, label))

    maintenant = datetime.datetime.utcnow()
    for doc, label in documents:
        doc.deleted_at = maintenant
    db.commit()
    print(f"\n--execute ({args.target}) : {len(documents)} document(s) retiré(s) (COMMIT).")


if __name__ == "__main__":
    main()
