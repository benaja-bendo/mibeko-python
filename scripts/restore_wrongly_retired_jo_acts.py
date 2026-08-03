#!/usr/bin/env python3
"""Remédiation 2026-08-03 phase 10 : annule le retrait à tort de 2 actes
individuels de congo-jo-1990-02 et congo-jo-1959-23, appliqué par
`scripts/retire_flat_jo_documents.py` en croyant qu'il s'agissait des deux
gros documents JO "à plat" restant à scinder.

Ce qui s'est réellement passé (découvert en creusant pourquoi le dry-run de
`push-corpus` affichait 0 document à pousser après le retrait) : ces deux JO
avaient DÉJÀ été scindés et poussés en production le 2026-08-02, dans le
cadre du chantier plus large "52 JO → 1168 actes" (`reingest_flat_journals.py`,
cf. mémoire de session « Audit + corrections ingestion 02/08 ») — 29 actes
vivants pour 1990-02, 49 pour 1959-23, tous créés entre 17:41 et 17:43 le
2026-08-02. Les vrais "placeholders à plat" de CE chantier (titrés
génériquement "Journal officiel n° 2-1990" / "n° 23-1959") avaient déjà été
retirés, correctement, à cette même date.

Les deux ids ci-dessous ne sont PAS des placeholders : ce sont deux actes
individuels, légitimes, faisant partie de ce même lot du 02/08 :
  - DECRET N° 90-042 du 17 Février 1990 (nomination Mr. AWAH Cabral)
  - Avis n°345 de l'Office des Changes (603)
Notre script de retrait de ce matin (2026-08-03 09:56:42) les a soft-supprimés
par erreur, en réutilisant par erreur d'identification des ids collectés plus
tôt dans la session (avant que le chantier "52 JO" ne soit connu de cette
session). Aucun impact public : les deux étaient `curation_status='draft'`,
jamais publiés.

Correctif : remettre `deleted_at` à NULL pour ces deux ids exactement — annule
le retrait à l'identique, aucune autre ligne touchée. Conséquence pour la
mission "Décret 90-042/Avis 345" de cette session : elle est caduque, les
deux JO sont déjà correctement scindés et en production ; les 79 documents
construits en dev par `scripts/split_jo_1990_02_et_1959_23.py` sont
redondants et n'ont pas besoin d'être poussés.

Usage (dev — sert surtout à valider les requêtes ; ces ids n'existent qu'en
prod, donc dev refusera "document introuvable", ce qui est le comportement
attendu) :
    python scripts/restore_wrongly_retired_jo_acts.py                  # dry-run
    python scripts/restore_wrongly_retired_jo_acts.py --execute        # écrit sur le dev local

Usage (prod) :
    python scripts/restore_wrongly_retired_jo_acts.py --target prod                  # dry-run, profil PROD_RO_*
    python scripts/restore_wrongly_retired_jo_acts.py --target prod --execute        # écrit en PROD, profil PROD_RW_DB_*

Garde-fous : mêmes profils/vérifications que `retire_flat_jo_documents.py`.
Refuse si l'un des deux documents n'est PAS actuellement supprimé (deleted_at
NULL) — condition inverse du script de retrait, pour ne restaurer que ce qui
a été retiré par erreur, et échouer bruyamment si l'état a encore changé.

Procédure d'annulation (si jamais CE script devait lui-même être annulé) :
relancer `retire_flat_jo_documents.py --execute` sur ces mêmes deux ids.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.db.models import LegalDocument  # noqa: E402

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "5433")

DOCUMENTS_A_RESTAURER = [
    ("ac10341a-bd9a-4b77-919b-ff4c92db85a9", "DECRET N° 90-042 (congo-jo-1990-02) — retiré à tort ce matin"),
    ("971d2c40-c72a-4c1c-ba5e-e096b565f9d6", "Avis n°345 (congo-jo-1959-23) — retiré à tort ce matin"),
]


def _guard_dev_only() -> None:
    if (DB_HOST, str(DB_PORT)) != ("127.0.0.1", "5433"):
        raise SystemExit(
            f"Refus : cible dev demandée mais l'environnement pointe {DB_HOST}:{DB_PORT}, "
            "pas 127.0.0.1:5433. Utiliser --target prod pour la production."
        )


def _session_dev():
    from sqlalchemy import create_engine

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
    for doc_id, label in DOCUMENTS_A_RESTAURER:
        doc = db.query(LegalDocument).filter(LegalDocument.id == doc_id).first()
        if doc is None:
            raise SystemExit(f"Refus : document introuvable {doc_id} ({label}).")
        if doc.deleted_at is None:
            raise SystemExit(
                f"Refus : document {doc_id} ({label}) n'est PAS supprimé (deleted_at NULL) — "
                "état inattendu, rien à restaurer. Vérifier avant de forcer quoi que ce soit."
            )
        if doc.curation_status == "published":
            raise SystemExit(
                f"Refus : document {doc_id} ({label}) est PUBLIÉ — état incompatible avec un "
                "retrait par erreur récent, à examiner manuellement avant de continuer."
            )
        documents.append((doc, label))
        print(f"  {doc_id}  statut={doc.curation_status}  deleted_at={doc.deleted_at}  {label}")

    print(f"\nCible : {args.target}. {len(documents)} document(s) à restaurer (deleted_at -> NULL).")

    if not args.execute:
        print("\nDRY-RUN : aucune écriture. Relancer avec --execute pour appliquer.")
        return

    if args.target == "prod":
        print("\n  Ces 2 documents PROD vont être restaurés (deleted_at -> NULL, annulation du retrait de ce matin).")
        saisie = input("Taper PRODUCTION pour confirmer : ").strip()
        if saisie != "PRODUCTION":
            print("Annulé.")
            sys.exit(1)
        db = _session_prod_ecriture(engine_ro)
        documents = []
        for doc_id, label in DOCUMENTS_A_RESTAURER:
            doc = db.query(LegalDocument).filter(LegalDocument.id == doc_id).first()
            documents.append((doc, label))

    for doc, label in documents:
        doc.deleted_at = None
    db.commit()
    print(f"\n--execute ({args.target}) : {len(documents)} document(s) restauré(s) (COMMIT).")


if __name__ == "__main__":
    main()
