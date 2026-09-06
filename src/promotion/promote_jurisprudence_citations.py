"""Promeut vers la production les citations de jurisprudence CCJA, une fois
les décisions elles-mêmes poussées par `push-corpus` (mibeko-python#19).

RE-RÉSOUT contre la cible plutôt que de copier `cited_article_id` du dev : un
Acte uniforme peut porter un id différent en prod qu'en dev quand il a été
réingéré côté dev après le push initial du 30/07/2026 — constaté le
06/09/2026 sur ce lot précis (17 des 22 articles cités par ces 184 arrêts
n'ont PAS le même id en prod), même défaut documenté pour AUDCG le
16/08/2026 (`push_corpus` ne détecte pas les doublons à clés différentes).
Copier l'id du dev créerait soit une violation de contrainte FK (id absent
en prod), soit pire, un lien silencieusement faux si un autre article
portait cet id par coïncidence.

Le texte intégral est identique des deux côtés (poussé tel quel par
`push-corpus`, aucune retouche) : ré-extraire les citations et les résoudre
contre le corpus PROD donne les mêmes candidats logiques, avec les BONS id.

Additif et idempotent comme `push_corpus` : la contrainte unique
`(decision_id, reference_brute)` de `jurisprudence_citations` fait qu'une
ligne déjà posée ne peut pas être dupliquée par une relance — `INSERT ...
ON CONFLICT DO NOTHING`, pas de pré-filtrage applicatif à maintenir.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.structuration.jurisprudence import TYPE_CODE, extract_citations, resolve_au_article


@dataclass
class LignePlan:
    decision_id: str
    cited_article_id: str | None
    reference_brute: str


@dataclass
class Plan:
    lignes: list = field(default_factory=list)
    decisions_sans_texte: int = 0

    def resolues(self) -> int:
        return sum(1 for l in self.lignes if l.cited_article_id)


def construire_plan(session: Session) -> Plan:
    """Calcule les lignes à insérer, contre LA CIBLE connectée par `session`.

    Fonctionne à l'identique en dry-run (session liée au profil lecture
    seule) et en exécution (session liée à `PROD_RW_DB_*`) : c'est la
    connexion qui change, jamais la logique — cf. doctrine `push_corpus`.
    """
    decisions = session.execute(
        text(
            "select a.document_id, av.contenu_texte "
            "from legal_documents ld "
            "join articles a on a.document_id = ld.id and a.deleted_at is null "
            "join article_versions av on av.article_id = a.id "
            "where ld.type_code = :type_code and ld.deleted_at is null"
        ),
        {"type_code": TYPE_CODE},
    ).all()

    plan = Plan()
    for document_id, contenu_texte in decisions:
        if not contenu_texte:
            plan.decisions_sans_texte += 1
            continue
        for citation in extract_citations(contenu_texte):
            cited_article_id = None
            if citation.acte_libelle and citation.numero_article:
                resolved = resolve_au_article(session, citation.acte_libelle, citation.numero_article)
                cited_article_id = str(resolved) if resolved else None
            plan.lignes.append(
                LignePlan(
                    decision_id=str(document_id),
                    cited_article_id=cited_article_id,
                    reference_brute=citation.reference_brute,
                )
            )
    return plan


def executer(engine_cible, plan: Plan, rapport_chemin: str | None = None) -> dict:
    """Insère les lignes du plan dans `jurisprudence_citations`, en une seule
    transaction (additif, pas de document existant modifié). Retour arrière :
    les ids insérés sont dans le rapport ; `DELETE FROM jurisprudence_citations
    WHERE id = ANY(...)` les retire proprement (aucune autre table n'y fait
    référence).
    """
    inseres_ids: list[str] = []
    with engine_cible.begin() as cnx:
        for ligne in plan.lignes:
            resultat = cnx.execute(
                text(
                    "insert into jurisprudence_citations "
                    "(id, decision_id, cited_article_id, reference_brute, created_at, updated_at) "
                    "values (uuid_generate_v4(), :decision_id, :cited_article_id, :reference_brute, now(), now()) "
                    "on conflict (decision_id, reference_brute) do nothing "
                    "returning id"
                ),
                {
                    "decision_id": ligne.decision_id,
                    "cited_article_id": ligne.cited_article_id,
                    "reference_brute": ligne.reference_brute,
                },
            )
            row = resultat.first()
            if row is not None:
                inseres_ids.append(str(row[0]))

    rapport = {
        "horodatage": datetime.now().isoformat(timespec="seconds"),
        "lignes_du_plan": len(plan.lignes),
        "inserees": len(inseres_ids),
        "deja_presentes": len(plan.lignes) - len(inseres_ids),
        "ids_inseres": inseres_ids,
        "retour_arriere": "delete from jurisprudence_citations where id = any(ARRAY['"
        + "','".join(inseres_ids)
        + "']::uuid[]);" if inseres_ids else None,
    }
    if rapport_chemin:
        with open(rapport_chemin, "w", encoding="utf-8") as f:
            json.dump(rapport, f, ensure_ascii=False, indent=2)
    return rapport
