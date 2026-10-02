#!/usr/bin/env python3
"""Construit la requête `replace-extraction` d'un brouillon à partir de sa structure
reconstruite en dev (mibeko-python#45).

Contexte. Le parseur corrigé (intitulés sur plusieurs lignes, articles masqués par un
caractère invisible, lignes jetées) donne une meilleure structure que celle des
brouillons de JO déjà poussés en production. Pour la leur appliquer sans changer
leur identifiant, la voie conforme à D-045 est l'API :

    GET  /api/v1/legal-documents/{id}/extraction-snapshot   (état mesuré + empreinte)
    POST /api/v1/legal-documents/{id}/replace-extraction    (simulation, puis application)

L'API conserve l'identifiant et la version active des articles réemployés, supprime
LOGIQUEMENT les retraits (`deleted_at`) et exige la confirmation chiffrée de ces
retraits. L'ancienne commande `mibeko:remplacer-articles-document`, elle, fait des
DELETE physiques : interdite en production (dashboard#227).

Ce script ne parle à aucune API et n'écrit rien : il lit le snapshot (fichier JSON
enregistré par l'humain ou par une copie locale) et la structure du dev, et produit le
fichier de requête, en mode simulation (`execute: false`).

Appariement.
- Articles : par numéro (unique par document). Un article de la production dont le
  numéro existe dans la structure reconstruite garde son `id` ; les autres numéros
  sont créés ; ceux de la production sans équivalent sont retirés.
- Divisions : alignées dans l'ordre du document sur le couple (type, numéro), car un
  « Chapitre 1 » se répète sous chaque titre. Une division alignée garde son `id`,
  une division sans équivalent dans la production est créée.
- Rien n'est deviné : contenus, repères de page, ordres viennent tels quels du dev.

Garde-fous : dev seulement (127.0.0.1:5433), SHA-256 du PDF source identique des deux
côtés, ordres uniques sur l'ensemble des divisions et articles, motif de 20 à 1 000
caractères. Le nombre de retraits annoncé par ce script doit être confirmé après la
simulation de l'API, qui fait foi.

Usage :
    python scripts/build_remplacement_extraction.py --document <uuid> \\
        --snapshot snapshot-response.json --motif "<au moins 20 caractères>" --out requete.json
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "5433")


class CibleInvalide(ValueError):
    """La cible ne peut pas être construite sans deviner : on s'arrête."""


def parent_depuis_chemin(tree_path: str) -> Optional[str]:
    """Identifiant du parent d'une division, lu dans son chemin ltree.

    Un segment est `n_` suivi de l'UUID dont les tirets sont des soulignés, comme
    `PublishedDocumentExtractionRepairService::parentIdFromPath` côté Laravel.
    """
    segments = [s for s in (tree_path or "").split(".") if s]
    if len(segments) < 2:
        return None
    brut = segments[-2]
    brut = brut[2:] if brut.startswith("n_") else brut
    return brut.replace("_", "-")


def _aligner_divisions(courantes: List[Dict[str, Any]], dev: List[Dict[str, Any]]) -> Dict[str, str]:
    """Associe l'id de division de dev à l'id de division courant, dans l'ordre."""
    cle = lambda n: (str(n["type"]), str(n["number"] if n["number"] is not None else ""))
    seq_cur = [cle(n) for n in courantes]
    seq_dev = [cle(n) for n in dev]
    appariement: Dict[str, str] = {}
    for bloc in difflib.SequenceMatcher(None, seq_cur, seq_dev, autojunk=False).get_matching_blocks():
        for k in range(bloc.size):
            appariement[dev[bloc.b + k]["id"]] = courantes[bloc.a + k]["id"]
    return appariement


def construire_cible(
    snapshot_target: Dict[str, Any],
    dev_nodes: List[Dict[str, Any]],
    dev_articles: List[Dict[str, Any]],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Cible `replace-extraction` et rapport prévisionnel.

    `dev_nodes` : {id, type, number, title, order, parent} (parent = id de dev ou None).
    `dev_articles` : {number, parent, order, content, source_locator} (parent = id de dev).
    """
    courantes = snapshot_target["nodes"]
    articles_courants = snapshot_target["articles"]

    appariement = _aligner_divisions(courantes, dev_nodes)
    cle_cible = {
        n["id"]: appariement.get(n["id"]) or f"nouveau-{n['id']}"
        for n in dev_nodes
    }

    nodes = []
    for n in dev_nodes:
        entree = {
            "key": cle_cible[n["id"]],
            "parent": cle_cible.get(n["parent"]) if n["parent"] else None,
            "type": n["type"],
            "number": n["number"],
            "title": n["title"],
            "order": int(n["order"]),
        }
        if n["id"] in appariement:
            entree["id"] = appariement[n["id"]]
        nodes.append(entree)

    par_numero: Dict[str, List[Dict[str, Any]]] = {}
    for a in articles_courants:
        par_numero.setdefault(a["number"], []).append(a)

    articles = []
    reutilises = 0
    contenus_modifies = 0
    reperes_modifies = 0
    for a in dev_articles:
        entree = {
            "number": a["number"],
            "parent": cle_cible.get(a["parent"]) if a["parent"] else None,
            "order": int(a["order"]),
            "content": a["content"] or "",
            "source_locator": a["source_locator"] or {},
        }
        existants = par_numero.get(a["number"], [])
        if len(existants) == 1:
            entree["id"] = existants[0]["id"]
            reutilises += 1
            contenus_modifies += 1 if existants[0]["content"] != entree["content"] else 0
            reperes_modifies += 1 if (existants[0].get("source_locator") or {}) != entree["source_locator"] else 0
        elif len(existants) > 1:
            raise CibleInvalide(f"numéro d'article en double dans la production : {a['number']!r}")
        articles.append(entree)

    nombres_dev = {a["number"] for a in dev_articles}
    retires = [a for a in articles_courants if a["number"] not in nombres_dev]
    ordres = [n["order"] for n in nodes] + [a["order"] for a in articles]
    doublons = sorted({o for o in ordres if ordres.count(o) > 1})
    if doublons:
        raise CibleInvalide(f"ordres non uniques sur les divisions et articles : {doublons[:10]}")
    if len({n["key"] for n in nodes}) != len(nodes):
        raise CibleInvalide("clés de division non uniques")

    cible = {
        "schema_version": 1,
        "document_id": snapshot_target["document_id"],
        "source_pdf": snapshot_target["source_pdf"],
        "nodes": nodes,
        "articles": articles,
    }
    ids_nodes_cibles = {n["id"] for n in nodes if "id" in n}
    rapport = {
        "articles_courants": len(articles_courants),
        "articles_cibles": len(articles),
        "articles_reutilises": reutilises,
        "articles_crees": len(articles) - reutilises,
        "articles_retires": len(retires),
        "numeros_retires": sorted(a["number"] for a in retires),
        "contenus_modifies": contenus_modifies,
        "reperes_modifies": reperes_modifies,
        "divisions_courantes": len(courantes),
        "divisions_cibles": len(nodes),
        "divisions_reutilisees": len(ids_nodes_cibles),
        "divisions_creees": len(nodes) - len(ids_nodes_cibles),
        "divisions_retirees": sum(1 for n in courantes if n["id"] not in ids_nodes_cibles),
    }
    return cible, rapport


def _session_dev():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    if (DB_HOST, str(DB_PORT)) != ("127.0.0.1", "5433"):
        raise SystemExit(f"Refus : le dev est attendu sur 127.0.0.1:5433, l'environnement pointe {DB_HOST}:{DB_PORT}.")
    url = "postgresql://{u}:{p}@{h}:{port}/{d}".format(
        u=os.getenv("DB_USERNAME", "root"), p=os.getenv("DB_PASSWORD", "root"),
        h=DB_HOST, port=DB_PORT, d=os.getenv("DB_DATABASE", "mibeko-db"),
    )
    return sessionmaker(bind=create_engine(url))()


def lire_structure_dev(db, document_id: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[str]]:
    from sqlalchemy import text

    noeuds = [
        {
            "id": str(r[0]), "type": r[1], "number": r[2], "title": r[3], "order": r[4],
            "parent": parent_depuis_chemin(str(r[5])),
        }
        for r in db.execute(
            text(
                "select id, type_unite, numero, titre, sort_order, tree_path::text from structure_nodes "
                "where document_id = :d and deleted_at is null order by sort_order, id"
            ),
            {"d": document_id},
        )
    ]
    articles = []
    for r in db.execute(
        text(
            "select a.numero_article, a.parent_node_id, a.ordre_affichage, v.contenu_texte, v.source_locator "
            "from articles a join article_versions v on v.article_id = a.id "
            "and upper_inf(v.validity_period) and v.deleted_at is null "
            "where a.document_id = :d and a.deleted_at is null order by a.ordre_affichage, a.id"
        ),
        {"d": document_id},
    ):
        locator = r[4]
        if isinstance(locator, str):
            locator = json.loads(locator) if locator else {}
        articles.append(
            {
                "number": r[0], "parent": str(r[1]) if r[1] else None, "order": r[2],
                "content": r[3], "source_locator": locator or {},
            }
        )
    sha = db.execute(
        text(
            "select checksum_sha256 from media_files where document_id = :d and file_category = 'SOURCE_PDF' "
            "order by created_at desc limit 1"
        ),
        {"d": document_id},
    ).scalar()
    return noeuds, articles, (sha or "").lower() or None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--document", required=True, help="UUID du document (identique en dev et dans le snapshot).")
    parser.add_argument("--snapshot", required=True, help="Réponse JSON de GET extraction-snapshot (ou son champ data).")
    parser.add_argument("--motif", required=True, help="Motif de l'opération, 20 à 1 000 caractères.")
    parser.add_argument("--out", required=True, help="Fichier de requête à écrire (execute: false).")
    args = parser.parse_args()

    try:
        uuid.UUID(args.document)
    except ValueError:
        raise SystemExit(f"Refus : « {args.document} » n'est pas un UUID.")
    motif = args.motif.strip()
    if not 20 <= len(motif) <= 1000:
        raise SystemExit(f"Refus : le motif doit faire de 20 à 1 000 caractères (reçu {len(motif)}).")

    brut = json.load(open(args.snapshot, encoding="utf-8"))
    donnees = brut.get("data", brut)
    cible_courante = donnees["target"]
    if cible_courante["document_id"] != args.document:
        raise SystemExit("Refus : le snapshot ne concerne pas ce document.")

    noeuds, articles, sha_dev = _lire(args.document)
    sha_snapshot = (cible_courante["source_pdf"].get("sha256") or "").lower()
    if not sha_dev or sha_dev != sha_snapshot:
        raise SystemExit(
            f"Refus : SHA-256 du PDF source différent (dev {sha_dev!r}, snapshot {sha_snapshot!r})."
        )
    if not articles:
        raise SystemExit("Refus : aucun article en dev pour ce document, rien à appliquer.")

    try:
        cible, rapport = construire_cible(cible_courante, noeuds, articles)
    except CibleInvalide as exc:
        raise SystemExit(f"Refus : {exc}")

    requete = {
        "execute": False,
        "expected_fingerprint": donnees["expected_fingerprint"],
        "motif": motif,
        "target": cible,
    }
    Path(args.out).write_text(json.dumps(requete, ensure_ascii=False), encoding="utf-8")
    print(f"Requête écrite : {args.out} (execute: false)")
    print(json.dumps({k: v for k, v in rapport.items() if k != "numeros_retires"}, ensure_ascii=False, indent=2))
    print(f"confirm_deletions attendu : {rapport['articles_retires']} (l'API le confirmera à la simulation)")
    print("numéros retirés :", ", ".join(rapport["numeros_retires"][:12]), "…" if len(rapport["numeros_retires"]) > 12 else "")


def _lire(document_id: str):
    db = _session_dev()
    try:
        return lire_structure_dev(db, document_id)
    finally:
        db.close()


if __name__ == "__main__":
    main()
