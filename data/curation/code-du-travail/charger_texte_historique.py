"""Charge un texte avec son historique dans un document existant, par l'API Laravel (dashboard#201).

Rejoue plan_chargement.json à l'identique sur n'importe quelle cible : la copie locale de la
production d'abord, la production ensuite (humain, dans son terminal). Tout passe par l'API :
audit, rôles et garde-fous s'appliquent ; aucune écriture SQL.

Étapes, dans l'ordre (chacune est journalisée, une reprise saute ce qui est déjà fait) :
  1. crée le(s) document(s) des lois modificatives (FLUX, brouillon) et leurs articles ;
  2. met à jour les métadonnées du document cible (titre, date de signature, statut) ;
  3. retire son contenu actuel — nœuds de tête (leurs articles suivent) et articles isolés,
     en suppression douce, réversible ;
  4. crée l'arborescence ;
  5. crée chaque article à la date d'effet de sa première version (et rattaché à sa loi s'il
     est né d'une loi modificative) — nécessite `POST /articles` avec `start_date` (dashboard#201) ;
  6. ajoute les amendements (`POST /articles/{id}/versions`, date d'effet et loi explicites) ;
  7. résout les signalements qui visaient un article retiré (sans objet : son texte n'existe plus).

Simulation par défaut : n'émet aucune écriture. Le jeton ne vient que du shell :

    export MIBEKO_API_TOKEN='…'        # jamais dans un fichier
    python3 charger_texte_historique.py --base-url http://127.0.0.1:8010/api/v1 --document <uuid>
    python3 charger_texte_historique.py --base-url … --document <uuid> --execute
    unset MIBEKO_API_TOKEN

Après coup : `php artisan mibeko:process-rag` sur la cible, pour que l'assistant voie le texte.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

ICI = Path(__file__).parent
TENTATIVES_MAX = 4


class Api:
    def __init__(self, base_url: str, jeton: str, rythme: int):
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=60,
                                   headers={"Authorization": f"Bearer {jeton}", "Accept": "application/json"})
        self.intervalle = 60 / rythme if rythme > 0 else 0
        self.dernier = 0.0

    def appel(self, methode: str, chemin: str, corps: dict | None = None) -> dict:
        for tentative in range(1, TENTATIVES_MAX + 1):
            attente = self.intervalle - (time.monotonic() - self.dernier)
            if attente > 0:
                time.sleep(attente)
            self.dernier = time.monotonic()
            reponse = self.client.request(methode, chemin, json=corps)
            if reponse.status_code == 429 or reponse.status_code >= 500:
                pause = float(reponse.headers.get("Retry-After", 5 * 2 ** (tentative - 1)))
                print(f"    {reponse.status_code} sur {methode} {chemin} — nouvel essai dans {pause:.0f} s", flush=True)
                time.sleep(min(pause, 60))
                continue
            if reponse.status_code >= 400:
                raise RuntimeError(f"{methode} {chemin} → {reponse.status_code} : {reponse.text[:500]}")
            return reponse.json()
        raise RuntimeError(f"{methode} {chemin} : échec après {TENTATIVES_MAX} tentatives")


def sonder_api(api: "Api") -> None:
    """Vérifie, sans rien écrire, que le jeton passe et que l'API sait dater une création.

    Une API antérieure à dashboard#209 ignore `start_date` sans erreur (Laravel écarte les
    champs qu'il ne valide pas) et date la version du jour : incident du 27/09/2026 en
    production, 373 articles datés du jour et aucun amendement possible ensuite. La sonde
    envoie un `POST /articles` invalide à dessein (ni document, ni texte) : la validation
    le rejette avant toute écriture, et seule une API qui connaît `start_date` se plaint
    aussi de la date non conforme.
    """
    reponse = api.client.post("/articles", json={"start_date": "sonde-sans-date"})
    if reponse.status_code in (401, 403):
        raise RuntimeError(f"sonde : jeton refusé ({reponse.status_code}). Rôle éditeur ou admin requis.")
    if reponse.status_code != 422:
        raise RuntimeError(f"sonde : réponse inattendue {reponse.status_code} : {reponse.text[:300]}")
    if "start_date" not in (reponse.json().get("errors") or {}):
        raise RuntimeError("sonde : l'API ignore `start_date` — la PR dashboard#209 n'est pas déployée sur cette cible. "
                           "Aucune écriture n'a été faite.")


def creer_article(api: "Api", corps: dict) -> str:
    """Crée un article et vérifie que l'API a bien daté sa première version.

    Filet derrière `sonder_api` : la réponse porte `validity_start` ; au moindre écart,
    l'article tout juste créé est retiré (suppression douce), pour qu'une relance ne bute
    pas sur « Cet article existe déjà » — ce qui est arrivé le 27/09 à 21:46 UTC, quand ce
    contrôle s'arrêtait sans défaire sa propre écriture.
    """
    donnees = api.appel("POST", "/articles", corps)["data"]
    attendue, obtenue = corps.get("start_date"), donnees.get("validity_start")
    if attendue and obtenue != attendue:
        api.appel("DELETE", f"/articles/{donnees['id']}")
        raise RuntimeError(
            f"article {corps['numero_article']} : date d'effet demandée {attendue}, enregistrée {obtenue} ; "
            "article retiré aussitôt. Arrêt avant d'aller plus loin."
        )
    return donnees["id"]


class Journal:
    """Correspondance clé du plan → identifiant créé, écrite après CHAQUE appel réussi."""

    def __init__(self, chemin: Path):
        self.chemin = chemin
        self.donnees = json.loads(chemin.read_text(encoding="utf-8")) if chemin.exists() else {}

    def get(self, cle: str):
        return self.donnees.get(cle)

    def poser(self, cle: str, valeur) -> None:
        self.donnees[cle] = valeur
        self.chemin.write_text(json.dumps(self.donnees, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--plan", default=str(ICI / "plan_chargement.json"))
    p.add_argument("--base-url", required=True, help="racine de l'API, ex. http://127.0.0.1:8010/api/v1")
    p.add_argument("--document", required=True, help="identifiant du document cible (le Code, STOCK, brouillon)")
    p.add_argument("--journal", help="fichier de reprise (défaut : journal-<hôte>-<document>.json à côté du plan)")
    p.add_argument("--rythme", type=int, default=40, help="appels par minute (quota API : 60)")
    p.add_argument("--execute", action="store_true", help="écrit réellement ; sinon simulation")
    p.add_argument("--limit", type=int, help="lot pilote : s'arrête après N articles créés (étapes 1 à 4 complètes) ; "
                                             "relancer sans --limit reprend là où il s'est arrêté")
    args = p.parse_args()

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    nb_versions = sum(len(a["versions"]) - 1 for a in plan["articles"])
    print(f"Plan : {len(plan['lois'])} loi(s) modificative(s), {len(plan['noeuds'])} nœuds, "
          f"{len(plan['articles'])} articles, {nb_versions} amendements — cible {args.base_url}, document {args.document}")
    if not args.execute:
        print("SIMULATION — aucun appel émis. Ajouter --execute pour écrire.")
        print(f"Appels estimés : ~{3 + sum(1 + len(l['articles']) for l in plan['lois'].values()) + len(plan['noeuds']) + len(plan['articles']) + nb_versions}"
              f" (+ le retrait du contenu actuel), soit ~{(len(plan['noeuds']) + len(plan['articles']) + nb_versions) / args.rythme:.0f} min au rythme de {args.rythme}/min.")
        return 0

    jeton = os.getenv("MIBEKO_API_TOKEN", "")
    if not jeton:
        print("MIBEKO_API_TOKEN absent du shell. À exporter à la main, jamais dans un fichier.", file=sys.stderr)
        return 1

    hote = urlparse(args.base_url).netloc.replace(":", "_")
    journal = Journal(Path(args.journal) if args.journal else ICI / f"journal-{hote}-{args.document[:8]}.json")
    api = Api(args.base_url, jeton, args.rythme)
    sonder_api(api)
    print("  sonde : jeton accepté, l'API date les créations")

    # 1. Lois modificatives
    lois = {}
    for code_loi, loi in plan["lois"].items():
        cle = f"loi:{code_loi}"
        if not journal.get(cle):
            corps = {k: loi[k] for k in ("titre_officiel", "type_code", "statut", "date_signature", "date_entree_vigueur")}
            journal.poser(cle, api.appel("POST", "/legal-documents", corps)["data"]["id"])
            print(f"  loi {code_loi} créée : {journal.get(cle)}")
        lois[code_loi] = journal.get(cle)
        for rang, article in enumerate(loi["articles"]):
            cle_art = f"{cle}:art:{article['numero']}"
            if not journal.get(cle_art):
                corps = {"document_id": lois[code_loi], "numero_article": article["numero"], "content": article["texte"],
                         "ordre_affichage": rang, "start_date": loi["date_signature"]}
                journal.poser(cle_art, creer_article(api, corps))

    # 2. Métadonnées du document cible
    if not journal.get("code:patch"):
        api.appel("PATCH", f"/legal-documents/{args.document}", plan["code"]["patch"])
        journal.poser("code:patch", True)
        print("  métadonnées du document mises à jour")

    # 3. Retrait du contenu actuel (une seule fois : ensuite, le document contient le nouveau)
    if not journal.get("code:retrait"):
        arbre = api.appel("GET", f"/legal-documents/{args.document}/tree")["data"]
        tete = [e for e in arbre if not e.get("parent_id")]
        for element in tete:
            chemin = f"/articles/{element['id']}" if element.get("type") == "ARTICLE" else f"/structure-nodes/{element['id']}"
            api.appel("DELETE", chemin)
        journal.poser("code:retrait", {"elements_de_tete_retires": len(tete)})
        print(f"  contenu actuel retiré : {len(tete)} éléments de tête (suppression douce)")

    # 4. Arborescence
    for noeud in plan["noeuds"]:
        if journal.get(noeud["cle"]):
            continue
        corps = {"document_id": args.document, "type_unite": noeud["type_unite"], "numero": noeud["numero"],
                 "titre": noeud["titre"], "parent_id": journal.get(noeud["parent"]) if noeud["parent"] else None,
                 "sort_order": noeud["ordre"]}
        journal.poser(noeud["cle"], api.appel("POST", "/structure-nodes", corps)["data"]["id"])
    print(f"  arborescence : {len(plan['noeuds'])} nœuds")

    # 5. Articles, à la date d'effet de leur première version
    crees_ce_passage = 0
    for rang, article in enumerate(plan["articles"], 1):
        if journal.get(article["cle"]):
            continue
        if args.limit is not None and crees_ce_passage >= args.limit:
            print(f"Lot pilote : arrêt après {args.limit} article(s). Vérifier, puis relancer sans --limit pour reprendre.")
            return 0
        crees_ce_passage += 1
        premiere = article["versions"][0]
        corps = {"document_id": args.document, "parent_node_id": journal.get(article["noeud"]),
                 "numero_article": article["numero"], "content": premiere["texte"],
                 "ordre_affichage": article["ordre"], "start_date": premiere["debut"]}
        if premiere["loi"]:
            corps["modifie_par_document_id"] = lois[premiere["loi"]]
        journal.poser(article["cle"], creer_article(api, corps))
        if rang % 50 == 0:
            print(f"  articles : {rang}/{len(plan['articles'])}", flush=True)
    print(f"  articles : {len(plan['articles'])} créés")

    # 6. Amendements
    faits = 0
    for article in plan["articles"]:
        for rang_version, version in enumerate(article["versions"][1:], 2):
            cle = f"{article['cle']}:v{rang_version}"
            if journal.get(cle):
                continue
            corps = {"content": version["texte"], "start_date": version["debut"],
                     "modifie_par_document_id": lois[version["loi"]]}
            api.appel("POST", f"/articles/{journal.get(article['cle'])}/versions", corps)
            journal.poser(cle, True)
            faits += 1
    print(f"  amendements : {faits} appliqués (sur {nb_versions})")

    # 7. Signalements devenus sans objet : ceux qui visent un article retiré à l'étape 3. Le
    #    garde-fou de publication les compte encore (il ne regarde pas l'état de l'article) ;
    #    les résoudre par l'API laisse une trace (qui, quand), là où `force` contournerait.
    #    Les signalements du document lui-même ou des nouveaux articles restent intacts.
    nouveaux = {journal.get(a["cle"]) for a in plan["articles"]}
    ouverts = api.appel("GET", f"/legal-documents/{args.document}/curation-flags?open_only=1")["data"]
    sans_objet = [f for f in ouverts if f.get("article_id") and f["article_id"] not in nouveaux]
    for flag in sans_objet:
        if not journal.get(f"flag:{flag['id']}"):
            api.appel("PATCH", f"/curation-flags/{flag['id']}", {"resolved": True})
            journal.poser(f"flag:{flag['id']}", flag["type_probleme"])
    print(f"  signalements sans objet résolus : {len(sans_objet)} ; encore ouverts : {len(ouverts) - len(sans_objet)}")

    # 8. Provenance : `metadata` n'a pas de canal API. On écrit le fichier que
    #    `php artisan mibeko:corriger-provenance-documents --mapping=…` applique, avec les
    #    identifiants de CETTE cible (la loi modificative y a un identifiant propre).
    provenance = [{"id": args.document, **plan["code"]["provenance"]}]
    provenance += [{"id": lois[code_loi], **loi["provenance"]} for code_loi, loi in plan["lois"].items()]
    fichier = journal.chemin.with_name(journal.chemin.name.replace("journal-", "provenance-"))
    fichier.write_text(json.dumps(provenance, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  provenance à appliquer : {fichier}")
    print(f"Terminé. Journal : {journal.chemin}. En attente : {', '.join(plan.get('en_attente', {}))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
