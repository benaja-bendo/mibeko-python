#!/usr/bin/env python3
"""Remédiation 2026-08-02 phase 4 : reclassifie les `CurationFlag`
`article_doublon` déjà posés sur des articles qui appartiennent en réalité à
une série secondaire INCRUSTÉE (annexe, traité ratifié, cahier des charges)
confirmée par `find_embedded_series_runs` (src/api/main.py) — au lieu d'une
vraie collision non résolue.

Contexte : `unique_article_number` (dans `ingest_hierarchy`) flague TOUJOURS
individuellement chaque renommage `_doublon_N`, jamais en silence (audit
2026-08-02 phase 2 — cf. `test_zero_renommage_sans_flag`). Ce garde-fou reste
intact ici : AUCUN flag n'est supprimé sans être remplacé, la bijection
« un renommage = un flag » est préservée à l'identique. Seules la
DESCRIPTION et la SEVERITY changent quand la collision fait partie d'une
série confirmée (`severity='warning'` : n'empêche plus la publication côté
Laravel — seul `severity='blocking'` bloque, cf. `CurationFlag.php`).

Aucun re-parsing, aucune re-ingestion, aucun renommage d'article : les
numéros déjà en base sont relus, le numéro D'ORIGINE retrouvé via
`derive_numero_origine`, la séquence ordinale reconstruite via
`ordinal_from_raw_number` — mêmes fonctions, testées, que `ingest_hierarchy`.

Périmètre (dry-run et --execute) : documents créés par le reingest du
2026-08-02 (`metadata->>'routage_jo_corrige' = 'true'`) portant au moins un
`CurationFlag` `article_doublon` non résolu — 65 documents, 366 flags au
2026-08-02 19h45. Volontairement HORS périmètre : les flags `article_doublon`
des codes STOCK anciens (Code civil, Code Pénal, Règlement CEMAC…), posés par
une version antérieure du code sur un périmètre différent — sujet distinct,
à traiter séparément (cf. mémoire [[code_penal_reliability]]).

Usage (dev — défaut) :
    python scripts/reclassify_embedded_series_flags.py                    # dry-run
    python scripts/reclassify_embedded_series_flags.py --execute          # écrit sur le dev local

Usage (prod) :
    python scripts/reclassify_embedded_series_flags.py --target prod                  # dry-run, profil PROD_RO_*
    python scripts/reclassify_embedded_series_flags.py --target prod --execute        # écrit en PROD, profil PROD_RW_*

Garde-fous : identiques à `scripts/reingest_flat_journals.py` (mêmes profils
PROD_RO_*/PROD_RW_*, mêmes vérifications de cible, dump frais requis avant
tout --execute --target prod, saisie « PRODUCTION » exigée, aucune exception).

Procédure d'annulation : ce script ne fait QUE des DELETE + INSERT sur
`curation_flags` (`source='heuristic', resolved=false`) pour les documents du
périmètre — jamais sur `articles`/`legal_documents`. Un dump pris juste avant
restaure l'état antérieur ; à défaut, les flags supprimés sont réimprimés en
intégralité par le dry-run (à conserver) avant toute exécution.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.api.main import (  # noqa: E402
    analyze_article_sequence,
    derive_numero_origine,
    find_embedded_series_runs,
    ordinal_from_raw_number,
)
from src.db.models import Article, CurationFlag, LegalDocument  # noqa: E402

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "5433")

DESCRIPTION_COLLISION_REELLE = (
    "Numéro d'article « {origine} » en collision avec un autre article du même "
    "document — renommé en « {final} » pour respecter la contrainte d'unicité. "
    "Nécessite une revue humaine (fusion, re-numérotation, ou confirmation qu'il "
    "s'agit bien de deux articles distincts)."
)
DESCRIPTION_SERIE_INCRUSTEE = (
    "Numéro d'article « {origine} » renommé en « {final} » : appartient "
    "probablement à une série secondaire (annexe, traité ratifié, cahier des "
    "charges) qui redémarre sa propre numérotation — pas nécessairement une "
    "erreur, à confirmer."
)


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
    """Session PROD en lecture seule, lecture seule PROUVÉE avant tout usage."""
    from src.db.prod_readonly import SQLSTATE_LECTURE_SEULE, assert_read_only, charger_cible, creer_engine

    cible = charger_cible()
    engine = creer_engine(cible)
    sqlstate = assert_read_only(engine)
    if sqlstate != SQLSTATE_LECTURE_SEULE:
        raise SystemExit(f"Refus : lecture seule non prouvée par SQLSTATE {SQLSTATE_LECTURE_SEULE} (obtenu : {sqlstate}).")
    print(f"Préflight PROD : lecture seule prouvée ({cible.resume()}).")
    return sessionmaker(bind=engine)(), engine


def _session_prod_ecriture(engine_ro):
    """Session PROD en écriture — n'est appelée QUE juste avant un --execute
    confirmé. Vérifie que la cible RW répond comme la cible RO (même base),
    même garde que `push_corpus.py`/`reingest_flat_journals.py`."""
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


def find_perimeter(db) -> list:
    """Documents du reingest 2026-08-02 portant au moins un `article_doublon`
    non résolu — cf. docstring, périmètre volontairement restreint."""
    ids_avec_flag = (
        db.query(CurationFlag.document_id)
        .filter(CurationFlag.resolved.is_(False), CurationFlag.type_probleme == "article_doublon")
        .distinct()
    )
    return (
        db.query(LegalDocument)
        .filter(
            LegalDocument.deleted_at.is_(None),
            LegalDocument.metadata_["routage_jo_corrige"].astext == "true",
            LegalDocument.id.in_(ids_avec_flag),
        )
        .order_by(LegalDocument.created_at)
        .all()
    )


def build_reclassification(db, document_id) -> Tuple[List[Dict[str, Any]], int]:
    """Reconstruit fidèlement les deux couches de détection (collision de
    chaîne + séquence ordinale) à partir des articles déjà en base, SANS rien
    renommer. Renvoie (nouveaux flags à créer, nb de flags actuellement en
    base pour ce document — pour le rapport dry-run)."""
    articles = (
        db.query(Article)
        .filter(Article.document_id == document_id, Article.deleted_at.is_(None))
        .order_by(Article.ordre_affichage)
        .all()
    )

    seen_bases: Dict[str, int] = {}
    rename_collisions: List[Dict[str, Any]] = []
    article_sequence: List[Tuple[Optional[int], Any]] = []

    for article in articles:
        origine = derive_numero_origine(article.numero_article) or article.numero_article
        if origine in seen_bases:
            seen_bases[origine] += 1
            rename_collisions.append({
                "article_id": article.id,
                "numero_origine": origine,
                "numero_final": article.numero_article,
            })
        else:
            seen_bases[origine] = 0
        ordinal = ordinal_from_raw_number(origine)
        if ordinal is not None:
            article_sequence.append((ordinal, article.id))

    ordinals = [n for n, _ in article_sequence]
    embedded_ids = {
        article_sequence[i][1]
        for start, end in find_embedded_series_runs(ordinals)
        for i in range(start, end + 1)
    }

    nouveaux_flags: List[Dict[str, Any]] = []
    for collision in rename_collisions:
        if collision["article_id"] in embedded_ids:
            severity, template = "warning", DESCRIPTION_SERIE_INCRUSTEE
        else:
            severity, template = "blocking", DESCRIPTION_COLLISION_REELLE
        nouveaux_flags.append({
            "article_id": collision["article_id"],
            "type_probleme": "article_doublon",
            "severity": severity,
            "description": template.format(origine=collision["numero_origine"], final=collision["numero_final"]),
        })

    already_flagged = {c["article_id"] for c in rename_collisions}
    for anomaly in analyze_article_sequence(article_sequence, already_flagged=already_flagged):
        nouveaux_flags.append({
            "article_id": anomaly["article_id"],
            "type_probleme": anomaly["type_probleme"],
            "severity": anomaly.get("severity", "blocking"),
            "description": anomaly["description"],
        })

    flags_actuels = (
        db.query(CurationFlag)
        .filter(
            CurationFlag.document_id == document_id,
            CurationFlag.source == "heuristic",
            CurationFlag.resolved.is_(False),
        )
        .count()
    )
    return nouveaux_flags, flags_actuels


def reclassify_one(db, document: LegalDocument, execute: bool) -> Dict[str, Any]:
    nouveaux_flags, nb_actuels = build_reclassification(db, document.id)
    nb_warning = sum(1 for f in nouveaux_flags if f["severity"] == "warning")
    nb_blocking = len(nouveaux_flags) - nb_warning

    resultat = {
        "document_id": str(document.id),
        "titre": document.titre_officiel,
        "flags_avant": nb_actuels,
        "flags_apres": len(nouveaux_flags),
        "dont_warning": nb_warning,
        "dont_blocking": nb_blocking,
    }
    if not execute:
        return resultat

    db.query(CurationFlag).filter(
        CurationFlag.document_id == document.id,
        CurationFlag.source == "heuristic",
        CurationFlag.resolved.is_(False),
    ).delete(synchronize_session=False)
    for flag in nouveaux_flags:
        db.add(CurationFlag(
            document_id=document.id,
            article_id=flag["article_id"],
            source="heuristic",
            type_probleme=flag["type_probleme"],
            severity=flag["severity"],
            description=flag["description"],
        ))
    return resultat


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

    perimetre = find_perimeter(db)
    print(f"Cible : {args.target}. Périmètre : {len(perimetre)} documents (reingest 2026-08-02, >=1 flag article_doublon non résolu).")

    if args.execute and args.target == "prod":
        print(f"\n  {len(perimetre)} documents PROD vont voir leurs flags heuristiques reclassifiés (DELETE + INSERT, curation_flags).")
        saisie = input("Taper PRODUCTION pour confirmer : ").strip()
        if saisie != "PRODUCTION":
            print("Annulé.")
            sys.exit(1)
        db = _session_prod_ecriture(engine_ro)
        perimetre = find_perimeter(db)

    resultats = [reclassify_one(db, document, execute=args.execute) for document in perimetre]

    if args.execute:
        db.commit()
        print(f"\n--execute ({args.target}) : modifications validées (COMMIT).")
    else:
        db.rollback()
        print(f"\nDRY-RUN ({args.target}) : aucune écriture (ROLLBACK). Relancer avec --execute pour appliquer.")

    for r in resultats:
        print(r)

    total_avant = sum(r["flags_avant"] for r in resultats)
    total_apres = sum(r["flags_apres"] for r in resultats)
    total_warning = sum(r["dont_warning"] for r in resultats)
    total_blocking = sum(r["dont_blocking"] for r in resultats)
    print(
        f"\nRésumé : {total_avant} flags article_doublon avant (tous blocking) -> "
        f"{total_apres} flags après ({total_blocking} blocking, {total_warning} warning)."
    )


if __name__ == "__main__":
    main()
