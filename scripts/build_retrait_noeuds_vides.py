#!/usr/bin/env python3
"""Construit la requête `replace-extraction` qui retire les divisions de sommaire
vides d'un document publié (mibeko-dashboard#221).

Contexte. Le JO n° 1-2011 spécial, publié, compte 86 lignes de table des matières
lues comme des divisions (18 « Livre » au lieu de 9) : le sommaire n'a pas de
rubrique « SOMMAIRE », le parseur ne le retire donc pas, et une réingestion
reproduit le défaut. Les articles sont corrects, ce sont des divisions sans
article qui polluent l'arbre. Le reparseur ne peut pas les corriger (mibeko-python#40),
et l'API sait faire l'opération : `replace-extraction` (D-045) retire LOGIQUEMENT
les divisions absentes de la cible et re-chemine un nœud dont le parent change.
Le runbook (production.md § 6, Temps 2, point 6) impose l'API dès qu'elle sait
faire l'opération.

    GET  /api/v1/legal-documents/{id}/extraction-snapshot   (état mesuré + empreinte)
    POST /api/v1/legal-documents/{id}/replace-extraction    (simulation, puis application)

Ce script ne parle à aucune API, n'écrit rien en base et ne lit aucune base : il
lit le snapshot (fichier JSON enregistré par l'humain ou par une copie locale) et
produit le fichier de requête, en mode simulation (`execute: false`).

Ce qu'il fait de la cible courante, et rien d'autre :
- retire les divisions dont le SOUS-ARBRE ne porte aucun article (le critère du
  diagnostic du 01/10) ;
- dissout, sur désignation (`--dissoudre`), une division non vide qui n'a aucun
  article direct : ses descendants remontent d'un niveau, puis elle est retirée. Un
  nœud non vide se désigne, il ne se détecte pas : on ne retire jamais une division
  à cause de la typographie de son titre ;
- laisse articles, contenus, ordres et repères de page strictement identiques.

Garde-fous : refus si tous les nœuds sont vides (ce serait une reconstruction), si
un nœud désigné n'existe pas ou porte des articles directs, si l'arbre est
incohérent (clé en double, parent inconnu), si rien n'est à retirer.

Retour arrière : le snapshot d'avant sert de cible (`--retour-depuis`), avec
l'empreinte relevée APRÈS l'application. L'opération se rejoue par l'API ; le dump
frais reste le vrai point de retour.

Usage :
    python scripts/build_retrait_noeuds_vides.py --document <uuid> \\
        --snapshot snapshot-prod.json --dissoudre <uuid-du-noeud> \\
        --motif "<au moins 20 caractères>" --out requete.json

    python scripts/build_retrait_noeuds_vides.py --document <uuid> \\
        --snapshot snapshot-prod.json --retour-depuis snapshot-apres.json \\
        --motif "<au moins 20 caractères>" --out requete-retour.json
"""

from __future__ import annotations

import argparse
import json
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set, Tuple


class CibleInvalide(ValueError):
    """La cible ne peut pas être construite sans deviner : on s'arrête."""


def _indexer(cible: Dict[str, Any]) -> Tuple[Dict[str, dict], Dict[Any, List[str]], Counter]:
    noeuds: List[dict] = cible["nodes"]
    articles: List[dict] = cible["articles"]

    par_cle = {n["key"]: n for n in noeuds}
    if len(par_cle) != len(noeuds):
        raise CibleInvalide("clés de division non uniques dans le snapshot")

    for n in noeuds:
        parent = n.get("parent")
        if parent is not None and parent not in par_cle:
            raise CibleInvalide(f"le parent {parent!r} de la division {n['key']!r} n'existe pas dans le snapshot")

    directs: Counter = Counter()
    for a in articles:
        parent = a.get("parent")
        if parent is not None and parent not in par_cle:
            raise CibleInvalide(f"le parent {parent!r} de l'article {a.get('number')!r} n'existe pas dans le snapshot")
        directs[parent] += 1

    enfants: Dict[Any, List[str]] = {}
    for n in noeuds:
        enfants.setdefault(n.get("parent"), []).append(n["key"])
    return par_cle, enfants, directs


def _articles_du_sous_arbre(par_cle: Dict[str, dict], enfants: Dict[Any, List[str]], directs: Counter) -> Dict[str, int]:
    """Nombre d'articles de chaque sous-arbre, sans récursion (arbre de profondeur quelconque)."""
    total: Dict[str, int] = {}
    pile: List[Tuple[str, bool]] = [(k, False) for k in enfants.get(None, [])]
    while pile:
        cle, visite = pile.pop()
        if visite:
            total[cle] = directs[cle] + sum(total[e] for e in enfants.get(cle, []))
            continue
        pile.append((cle, True))
        pile.extend((e, False) for e in enfants.get(cle, []))
    return total


def _resoudre_designes(par_cle: Dict[str, dict], dissoudre: Iterable[str]) -> Set[str]:
    par_id = {n["id"]: n["key"] for n in par_cle.values() if n.get("id")}
    designes: Set[str] = set()
    for ref in dissoudre:
        if ref in par_cle:
            designes.add(ref)
        elif ref in par_id:
            designes.add(par_id[ref])
        else:
            raise CibleInvalide(f"le nœud à dissoudre {ref!r} n'existe pas dans le snapshot")
    return designes


def retirer_noeuds_vides(cible: Dict[str, Any], dissoudre: Iterable[str] = ()) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Retire les divisions sans article et dissout celles qu'on désigne.

    Pure : ne touche pas à `cible`. Rend la nouvelle cible (mêmes articles, mêmes
    ordres) et un rapport chiffré.
    """
    par_cle, enfants, directs = _indexer(cible)
    sous_arbre = _articles_du_sous_arbre(par_cle, enfants, directs)

    vides = {k for k in par_cle if sous_arbre[k] == 0}
    if par_cle and len(vides) == len(par_cle):
        raise CibleInvalide("toutes les divisions sont vides : ce serait une reconstruction, pas un nettoyage")

    designes = _resoudre_designes(par_cle, dissoudre)
    for cle in sorted(designes):
        if directs[cle] > 0:
            raise CibleInvalide(
                f"le nœud {cle!r} porte {directs[cle]} article(s) directement : le dissoudre exigerait de les re-parenter"
            )

    a_retirer = vides | designes
    if not a_retirer:
        raise CibleInvalide("aucune division vide et aucun nœud désigné : rien à retirer")

    def parent_conserve(cle: str):
        """Premier ancêtre qui reste, ou None (racine)."""
        parent = par_cle[cle].get("parent")
        while parent is not None and parent in a_retirer:
            parent = par_cle[parent].get("parent")
        return parent

    nouveaux: List[dict] = []
    remontes: List[str] = []
    for noeud in cible["nodes"]:
        if noeud["key"] in a_retirer:
            continue
        copie = dict(noeud)
        nouveau_parent = parent_conserve(noeud["key"])
        if nouveau_parent != noeud.get("parent"):
            copie["parent"] = nouveau_parent
            remontes.append(noeud["key"])
        nouveaux.append(copie)

    cle_restantes = {n["key"] for n in nouveaux}
    for article in cible["articles"]:
        parent = article.get("parent")
        if parent is not None and parent not in cle_restantes:
            raise CibleInvalide(f"l'article {article.get('number')!r} perdrait son parent {parent!r}")

    nouvelle_cible = dict(cible)
    nouvelle_cible["nodes"] = nouveaux

    par_type_avant = Counter(n.get("type") for n in cible["nodes"])
    par_type_apres = Counter(n.get("type") for n in nouveaux)
    rapport = {
        "noeuds_avant": len(cible["nodes"]),
        "noeuds_apres": len(nouveaux),
        "noeuds_retires": len(a_retirer),
        "dont_vides": len(vides),
        "dont_dissous_non_vides": len(designes - vides),
        "noeuds_remontes": len(remontes),
        "articles": len(cible["articles"]),
        "articles_retires": 0,
        "types_avant": dict(par_type_avant),
        "types_apres": dict(par_type_apres),
    }
    return nouvelle_cible, rapport


def construire_requete(
    donnees: Dict[str, Any],
    document_id: str,
    motif: str,
    dissoudre: Iterable[str] = (),
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Requête de simulation à partir de la réponse de `extraction-snapshot` (champ `data`)."""
    cible_courante = donnees["target"]
    if cible_courante["document_id"] != document_id:
        raise CibleInvalide("le snapshot ne concerne pas ce document")
    _verifier_motif(motif)

    cible, rapport = retirer_noeuds_vides(cible_courante, dissoudre)
    requete = {
        "execute": False,
        "expected_fingerprint": donnees["expected_fingerprint"],
        "motif": motif.strip(),
        "target": cible,
    }
    return requete, rapport


def construire_requete_retour(
    avant: Dict[str, Any],
    apres: Dict[str, Any],
    document_id: str,
    motif: str,
) -> Dict[str, Any]:
    """Requête de retour arrière : la cible est le snapshot d'avant, l'empreinte celle d'après."""
    if avant["target"]["document_id"] != document_id or apres["target"]["document_id"] != document_id:
        raise CibleInvalide("un des snapshots ne concerne pas ce document")
    _verifier_motif(motif)
    if avant["expected_fingerprint"] == apres["expected_fingerprint"]:
        raise CibleInvalide("les deux snapshots ont la même empreinte : l'opération n'a rien changé, rien à défaire")
    return {
        "execute": False,
        "expected_fingerprint": apres["expected_fingerprint"],
        "motif": motif.strip(),
        "target": avant["target"],
    }


def _verifier_motif(motif: str) -> None:
    if not 20 <= len(motif.strip()) <= 1000:
        raise CibleInvalide(f"le motif doit faire de 20 à 1 000 caractères (reçu {len(motif.strip())})")


def _lire_donnees(chemin: str) -> Dict[str, Any]:
    brut = json.loads(Path(chemin).read_text(encoding="utf-8"))
    return brut.get("data", brut)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--document", required=True, help="UUID du document.")
    parser.add_argument("--snapshot", required=True, help="Réponse JSON de GET extraction-snapshot (ou son champ data).")
    parser.add_argument("--dissoudre", action="append", default=[], help="Id ou clé d'un nœud non vide, sans article direct, à dissoudre (répétable).")
    parser.add_argument("--retour-depuis", help="Snapshot relevé APRÈS l'application : construit la requête de retour arrière.")
    parser.add_argument("--motif", required=True, help="Motif de l'opération, 20 à 1 000 caractères.")
    parser.add_argument("--out", required=True, help="Fichier de requête à écrire (execute: false).")
    args = parser.parse_args()

    try:
        uuid.UUID(args.document)
    except ValueError:
        raise SystemExit(f"Refus : « {args.document} » n'est pas un UUID.")

    try:
        if args.retour_depuis:
            requete = construire_requete_retour(_lire_donnees(args.snapshot), _lire_donnees(args.retour_depuis), args.document, args.motif)
            rapport = {"mode": "retour arrière", "noeuds_cible": len(requete["target"]["nodes"]), "articles": len(requete["target"]["articles"])}
        else:
            requete, rapport = construire_requete(_lire_donnees(args.snapshot), args.document, args.motif, args.dissoudre)
    except CibleInvalide as exc:
        raise SystemExit(f"Refus : {exc}")

    Path(args.out).write_text(json.dumps(requete, ensure_ascii=False), encoding="utf-8")
    print(f"Requête écrite : {args.out} (execute: false)")
    print(json.dumps(rapport, ensure_ascii=False, indent=2))
    print("confirm_deletions attendu : 0 (aucun article retiré ; l'API le confirmera à la simulation)")


if __name__ == "__main__":
    main()
