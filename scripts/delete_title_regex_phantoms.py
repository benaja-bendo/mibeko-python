#!/usr/bin/env python3
"""Remédiation 2026-08-02 phase 5, suite : supprime (soft-delete) les 33
documents fantômes confirmés créés par le bug `title_regex` (corrigé,
commit dd4ca38) — une clause de clôture du JO congolais (« … pourra faire
l'objet d'une suspension… », « … sera publié et communiqué partout où besoin
sera. ») coupée par le rendu markdown et prise à tort pour le début d'un
nouvel acte.

Périmètre déterminé par une recherche systématique (lecture seule, 2026-08-02
soir) : 145 documents dont le titre commence par un mot-clé d'acte en
MINUSCULES (signature du bug — un vrai titre est toujours en MAJUSCULES dans
ce corpus), puis filtré au contenu réellement vérifié :
- 33 ici : ≤2 articles, <500 caractères de contenu total, CHAQUE contenu lu
  individuellement et confirmé comme du bruit pur (bandeau de ministère,
  fragment de signature, phrase coupée au milieu, document vide) — jamais un
  acte juridique.
- 112 EXCLUS délibérément : contenu substantiel (jusqu'à 89 articles,
  91 813 caractères) — ce sont de VRAIS textes, pas des fantômes, juste
  affectés par un problème d'extraction de titre SANS RAPPORT avec ce bug ;
  ne pas les toucher ici.

Soft-delete cascadé vers les articles, exactement comme le hook Laravel
`LegalDocument::deleting` (`app/Models/LegalDocument.php`) : seuls
`legal_documents.deleted_at` et `articles.deleted_at` sont posés — ni
`curation_flags`, ni `media_files`, ni `structure_nodes` (le modèle Laravel
ne les touche pas non plus). Le markdown source, lui, n'est jamais modifié :
réversible en remettant `deleted_at = NULL` sur les 33 documents et leurs
articles.

Usage (dev — défaut) :
    python scripts/delete_title_regex_phantoms.py                    # dry-run
    python scripts/delete_title_regex_phantoms.py --execute           # écrit sur le dev local

Usage (prod) :
    python scripts/delete_title_regex_phantoms.py --target prod                  # dry-run, profil PROD_RO_*
    python scripts/delete_title_regex_phantoms.py --target prod --execute        # écrit en PROD, profil PROD_RW_DB_*

Garde-fous : mêmes profils/vérifications que les scripts précédents (dump
frais requis avant tout --execute --target prod, saisie « PRODUCTION »
exigée, aucune exception, refus si l'un des documents est publié).
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.db.models import Article, LegalDocument  # noqa: E402

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "5433")

# Périmètre figé — 33 documents, un par ligne, vérifiés individuellement
# (titre, id, taille de contenu en octets pour traçabilité de la revue).
PERIMETRE_IDS = [
    "6581b065-599a-4c99-9292-d6a715bc7779",  # décret en Conseil des ministres. (72 car.)
    "a684cdf9-f25a-40d1-adb6-1efb64a4d2d2",  # communiqué partout où besoin sera. (313 car.)
    "220dbec7-083d-4752-8ae1-a9b26775780a",  # communiqué partout où besoin sera. (245 car.)
    "cece8877-4613-4931-a149-a1d65bb99b8b",  # loi n° 4-2005 du 11 avril 2005 portant code minier, (50 car.)
    "8efa1387-ba9c-43a0-af0f-b413df6809e9",  # décret n° 59-29 du 30 janvier 1959 fixant les modalités (483 car.)
    "205dae06-1480-4bf5-b876-561f31adc8b3",  # communiqué partout où besoin sera. (64 car.)
    "87a008b7-76ae-4ede-af7f-b9ff1cb4ebe4",  # communiqué partout où besoin sera. (64 car.)
    "c5ebb199-51ec-4b6e-89ac-3104d2bc36de",  # communiqué partout où besoin sera. (64 car.)
    "10938259-1208-4c09-9e94-e703596fd18a",  # communiqué partout où besoin sera. (64 car.)
    "1eda138e-71e8-4f60-86e9-197c32cbb2b5",  # communiqué partout où besoin sera. (122 car.)
    "65e9df25-6f41-4833-be06-b872b5ec117e",  # décret n° 2004-11 du 3 février 2004. (252 car.)
    "1433baa0-2d45-4194-9c16-0ba6de58317a",  # communiqué partout où besoin sera. (49 car.)
    "71d22aef-b975-48ae-8944-1e0ccdb5594c",  # communiqué partout où besoin sera. (58 car.)
    "df202701-0424-4ba6-81bf-7ab73eb665ae",  # décret n° 2024-2070 définissant les modalités (330 car.)
    "c108a55e-65e7-472f-80e3-6cbfb13b07f8",  # loi de l'Emprunteur n° 022-92, en date du 20 octobre (145 car.)
    "61a230ee-850b-42f1-a53e-aadd6c1a14bb",  # communiqué partout où besoin sera. (58 car.)
    "31f0a2c8-7acc-494c-b1b2-0486c38e4f71",  # communiqué partout où besoin sera. (60 car.)
    "fe71959a-a00c-45b7-8159-3babf0045111",  # décret en Conseil des ministres. (0 article, vide)
    "89a01744-1925-4d2c-886d-cc7f2d9482da",  # loi n° 4-2005 du 11 avril 2005 portant code minier, (127 car.)
    "b47fa010-faf7-4df5-a2cd-efe9c131441b",  # avis juridiques et tout autre document s'y rattachant. (73 car.)
    "465d2871-2001-461a-9baa-01403ba77ab7",  # décision conjointe de LA BANQUE et de L'EMPRUNTEUR (222 car.)
    "9e6d2c45-f498-4f03-ba4c-62d27686d959",  # communiqué partout où besoin sera. (63 car.)
    "9a72418f-c215-45e7-93a6-486b603fafa7",  # communiqué partout où besoin sera. (63 car.)
    "9c428e86-596f-4527-b818-60ad0f41de1e",  # communiqué partout où besoin sera. (63 car.)
    "c49a3de2-c9d2-4e0d-9b24-c56068af41bf",  # communiqué partout où besoin sera. (63 car.)
    "e7fac885-c573-49a2-b87d-d2f6bda7ab3e",  # communiqué partout où besoin sera. (63 car.)
    "4b1de7b8-64d2-4ba8-b52b-45c94533b7e0",  # communiqué partout où besoin sera. (72 car.)
    "9e5021fd-ec3b-474d-8aa1-c638be21aada",  # communiqué partout où besoin sera. (63 car.)
    "5765cac4-8739-404c-8946-e09bbc3aacb8",  # communiqué partout où besoin sera. (63 car.)
    "a698d2c9-bf1b-41c7-84ce-79304851175d",  # communiqué partout où besoin sera. (67 car.)
    "df406d5a-22dd-491f-9e60-4fee5fd598d7",  # communiqué partout où besoin sera. (77 car.)
    "0a442ae3-095e-4c95-9d0f-d9e250d9a53c",  # décret n° 2007-293 du 31 mai 2007 susvisé. (149 car.)
    "ae48731e-4992-43f5-8e24-7cfaccb998b4",  # décret pour se conformer à ses dispositions. (497 car.)
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

    documents = (
        db.query(LegalDocument)
        .filter(LegalDocument.id.in_(PERIMETRE_IDS), LegalDocument.deleted_at.is_(None))
        .all()
    )
    print(f"Cible : {args.target}. Périmètre : {len(documents)}/{len(PERIMETRE_IDS)} documents trouvés (non déjà supprimés).")
    if len(documents) != len(PERIMETRE_IDS):
        trouves = {str(d.id) for d in documents}
        manquants = [i for i in PERIMETRE_IDS if i not in trouves]
        print(f"  (déjà supprimés ou introuvables, ignorés : {manquants})")

    publies = [d for d in documents if d.curation_status == "published"]
    if publies:
        print("ARRÊT — un ou plusieurs documents du périmètre sont publiés, hors garde-fou sans validation humaine :")
        for d in publies:
            print(f"  - {d.id} {d.titre_officiel!r}")
        sys.exit(1)

    if args.execute and args.target == "prod":
        print(f"\n  {len(documents)} documents PROD vont être marqués supprimés (soft-delete), avec leurs articles.")
        saisie = input("Taper PRODUCTION pour confirmer : ").strip()
        if saisie != "PRODUCTION":
            print("Annulé.")
            sys.exit(1)
        db = _session_prod_ecriture(engine_ro)
        documents = (
            db.query(LegalDocument)
            .filter(LegalDocument.id.in_(PERIMETRE_IDS), LegalDocument.deleted_at.is_(None))
            .all()
        )

    now = datetime.datetime.utcnow()
    resultats = []
    for document in documents:
        nb_articles = (
            db.query(Article).filter(Article.document_id == document.id, Article.deleted_at.is_(None)).count()
        )
        resultats.append({"document_id": str(document.id), "titre": document.titre_officiel, "nb_articles": nb_articles})
        if args.execute:
            document.deleted_at = now
            db.query(Article).filter(Article.document_id == document.id, Article.deleted_at.is_(None)).update(
                {Article.deleted_at: now}, synchronize_session=False
            )

    if args.execute:
        db.commit()
        print(f"\n--execute ({args.target}) : modifications validées (COMMIT).")
    else:
        db.rollback()
        print(f"\nDRY-RUN ({args.target}) : aucune écriture (ROLLBACK). Relancer avec --execute pour appliquer.")

    for r in resultats:
        print(r)

    print(f"\nRésumé : {len(resultats)} documents {'supprimés' if args.execute else 'à supprimer'}, "
          f"{sum(r['nb_articles'] for r in resultats)} articles associés.")


if __name__ == "__main__":
    main()
