#!/usr/bin/env python3
"""Détection des documents dupliqués (même acte ingéré deux fois).

Contexte (audit de remédiation 2026-08-02/03, docs/audit-ingestion-2026-08-02.md) :
une requête pg_trgm brute (similarity(titre, titre) > seuil, même date_signature)
remonte des milliers de paires candidates, dont l'immense majorité sont des FAUX
POSITIFS légitimes — plusieurs actes non numérotés du même jour partagent
souvent un titre générique (« DECISION DU 9 FEVRIER 1959 ») sans être des
doublons. La similarité de titre seule ne discrimine pas.

Méthodologie retenue ici, en deux passes indépendantes et complémentaires :

  PASSE 1 — identité ADMINISTRATIVE de l'acte.
    Le numéro d'acte est extrait explicitement du titre (regex sur
    « n° »/« N° »/« No »/« N* » — ce dernier étant une corruption OCR
    fréquente du signe degré) puis normalisé (zéros de tête retirés, séparateurs
    « - »/« / » uniformisés). Les documents vivants sont regroupés par
    (type d'acte détecté en tête de titre, numéro normalisé, date_signature).
    Un groupe de taille >= 2 est un candidat FORT : deux actes du même type,
    portant le même numéro, signés le même jour, ne peuvent légitimement
    coexister — c'est une identité administrative, pas une similarité de texte.
    Les titres génériques sans numéro extractible (le cas des faux positifs
    pg_trgm) sortent naturellement de cette passe : ils ne sont simplement pas
    groupés, ce qui règle le problème par construction plutôt que par seuil.

  PASSE 2 — identité de CONTENU, pour départager/confirmer chaque groupe de la
    passe 1 (jamais comme signal autonome à l'échelle du corpus — voir « piège »
    ci-dessous) :
      - hash SHA-256 du texte des articles vivants (numero_article + contenu_texte
        de la dernière version, concaténés dans l'ordre d'affichage) : une
        égalité exacte est une quasi-certitude.
      - à défaut d'égalité exacte, similarité de texte (difflib) + recouvrement
        Jaccard des numero_article, comme signal de force intermédiaire.
      - checksum SHA-256 du PDF source et du markdown MinerU (media_files),
        comparé UNIQUEMENT entre les membres d'un même groupe déjà identifié
        par la passe 1 — voir piège ci-dessous.

  PIÈGE vérifié empiriquement avant de coder ce script (requêtes read-only
  ci-dessous) : dans ce pipeline, un Journal officiel est téléversé UNE fois
  puis découpé en actes — le PDF source ET le markdown MinerU (media_files,
  file_category SOURCE_PDF / EXTRACTION_MARKDOWN) sont donc PARTAGÉS À
  L'IDENTIQUE par TOUS les actes issus d'un même JO (vérifié : un JO à 64
  documents vivants n'a qu'1 seul checksum SOURCE_PDF distinct). Comparer ces
  checksums à l'échelle du corpus entier produirait un déluge de faux positifs
  (tout acte d'un JO « matcherait » tous les autres actes du même JO) — c'est
  pourquoi ce script ne s'en sert JAMAIS pour grouper, seulement pour corroborer
  un groupe déjà construit par la passe 1 (numéro + type + date), où il reste
  interprétable (deux vraies copies d'un même acte, si elles descendent du même
  téléversement JO réutilisé, partageront ces checksums ; si chacune a sa propre
  extraction indépendante — cas confirmé sur Arrêté n° 3831/3832/3833 avec deux
  jeux de flags article_manquant distincts — elles ne les partageront pas, d'où
  le hash de contenu comme signal principal).

Lecture SEULE stricte : ce script n'écrit jamais en base, sous aucune option.
Il produit un rapport (JSON) destiné à une décision humaine ; le dédoublonnage
lui-même (soft-delete du doublon, cf. docstring de fin de fichier) est un acte
séparé, à exécuter opération par opération, jamais depuis cet outil.

Usage :
    python scripts/detect_document_duplicates.py --target prod --out rapport.json
    python scripts/detect_document_duplicates.py --target dev            # test local

Garde-fous : identiques en esprit à scripts/reclassify_embedded_series_flags.py
(mêmes profils PROD_RO_*, lecture seule PROUVÉE avant toute requête) — mais ici
sans aucune contrepartie d'écriture, donc sans prompt « PRODUCTION ».
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "5433")

# --- Identité administrative : type d'acte + numéro --------------------------

# Du plus spécifique au plus générique : « LOI ORGANIQUE » doit être reconnu
# avant « LOI », sans quoi il serait toujours capté par le préfixe le plus court.
ACT_TYPE_KEYWORDS = [
    "LOI ORGANIQUE", "LOI CONSTITUTIONNELLE", "DECRET-LOI", "DECRET",
    "ARRETE CONJOINT", "ARRETE", "ORDONNANCE", "DECISION", "DELIBERATION",
    "CIRCULAIRE", "CONVENTION", "ACCORD", "PROTOCOLE", "RESOLUTION",
    "INSTRUCTION", "AVIS", "COMMUNIQUE", "LOI",
]

# « N° » / « N° » / « No » / « N* » / « N\* » (le « * », précédé ou non d'un
# backslash littéral — constaté en base : `LOI N\* 25-80…`, artefact d'un
# échappement Markdown jamais nettoyé — est une corruption OCR récurrente du
# signe degré) — appliqué sur le titre MAJUSCULE, SANS repli d'accents (le
# signe degré n'est pas un caractère accentuable, un strip Unicode agressif le
# supprimerait et casserait la détection).
_ACT_NUMBER_RE = re.compile(r"N\\?[°ºO*]\.?\s*([0-9][0-9A-Z]*(?:[\-/][0-9A-Z]+)*)")


def _fold_ascii(titre: str) -> str:
    """Majuscules sans accents (« Arrêté »/« Décret » -> « ARRETE »/« DECRET »),
    pour matcher le mot-clé de tête indépendamment de la qualité OCR — utilisé
    UNIQUEMENT pour le type d'acte, jamais pour le numéro (voir _ACT_NUMBER_RE)."""
    decomposed = unicodedata.normalize("NFKD", titre or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).upper()


def extract_act_type(titre: str) -> str | None:
    folded = _fold_ascii(titre).lstrip()
    for keyword in ACT_TYPE_KEYWORDS:
        if folded == keyword or folded.startswith(keyword + " "):
            return keyword
    return None


def extract_act_number(titre: str) -> str | None:
    match = _ACT_NUMBER_RE.search((titre or "").upper())
    return match.group(1) if match else None


def normalize_act_number(raw_number: str) -> str:
    """Uniformise le séparateur et retire les zéros de tête par segment, pour
    que « 025-2022 », « 25/2022 » et « 25-2022 » se regroupent."""
    segments = re.split(r"[\-/]", raw_number)
    return "-".join(segment.lstrip("0") or "0" for segment in segments)


# --- Connexion ----------------------------------------------------------------


def _session_dev():
    if (DB_HOST, str(DB_PORT)) != ("127.0.0.1", "5433"):
        raise SystemExit(
            f"Refus : cible dev demandée mais l'environnement pointe {DB_HOST}:{DB_PORT}, "
            "pas 127.0.0.1:5433. Utiliser --target prod pour la production."
        )
    db_user = os.getenv("DB_USERNAME", "root")
    db_pass = os.getenv("DB_PASSWORD", "root")
    db_name = os.getenv("DB_DATABASE", "mibeko-db")
    engine = create_engine(f"postgresql://{db_user}:{db_pass}@{DB_HOST}:{DB_PORT}/{db_name}")
    return sessionmaker(bind=engine)()


def _session_prod_readonly():
    """Session PROD en lecture seule, lecture seule PROUVÉE avant tout usage —
    jamais de requête prod hors de cette preuve, même en diagnostic pur."""
    from src.db.prod_readonly import SQLSTATE_LECTURE_SEULE, assert_read_only, charger_cible, creer_engine

    cible = charger_cible()
    engine = creer_engine(cible)
    sqlstate = assert_read_only(engine)
    if sqlstate != SQLSTATE_LECTURE_SEULE:
        raise SystemExit(f"Refus : lecture seule non prouvée par SQLSTATE {SQLSTATE_LECTURE_SEULE} (obtenu : {sqlstate}).")
    print(f"Préflight PROD : lecture seule prouvée ({cible.resume()}).")
    return sessionmaker(bind=engine)()


# --- Passe 1 : clustering par identité administrative -------------------------


def fetch_live_documents(db) -> list[dict[str, Any]]:
    rows = db.execute(text("""
        select id, titre_officiel, date_signature, document_role, curation_status,
               type_code, stock_code, created_at, official_journal_id,
               metadata->>'routage_jo_corrige' as routage_jo_corrige
        from legal_documents
        where deleted_at is null
    """)).mappings().all()
    return [dict(r) for r in rows]


def build_clusters(documents: list[dict[str, Any]]) -> dict[tuple, list[dict[str, Any]]]:
    clusters: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for doc in documents:
        if doc["date_signature"] is None:
            continue
        act_type = extract_act_type(doc["titre_officiel"])
        act_number_raw = extract_act_number(doc["titre_officiel"])
        doc["act_type"] = act_type
        doc["act_number_raw"] = act_number_raw
        if act_type is None or act_number_raw is None:
            continue
        act_number_norm = normalize_act_number(act_number_raw)
        doc["act_number_norm"] = act_number_norm
        clusters[(act_type, act_number_norm, doc["date_signature"])].append(doc)
    return {key: docs for key, docs in clusters.items() if len(docs) >= 2}


# --- Passe 2 : signaux de contenu, à l'intérieur d'un groupe déjà formé -------


def fetch_content_signals(db, doc_ids: list[str]) -> dict[str, dict[str, Any]]:
    signals: dict[str, dict[str, Any]] = {
        doc_id: {
            "article_count": 0,
            "numero_articles": set(),
            "content_hash": None,
            "flags_blocking": 0,
            "flags_warning": 0,
            "checksums": {},
            "dossier_hits": [],
        }
        for doc_id in doc_ids
    }

    article_rows = db.execute(text("""
        select a.id, a.document_id, a.numero_article, av.contenu_texte
        from articles a
        join lateral (
            select contenu_texte from article_versions v
            where v.article_id = a.id
            order by lower(v.validity_period) desc nulls last
            limit 1
        ) av on true
        where a.deleted_at is null and a.document_id = any(cast(:ids as uuid[]))
        order by a.document_id, a.ordre_affichage
    """), {"ids": doc_ids}).all()

    article_to_doc: dict[str, str] = {}
    text_buffers: dict[str, list[str]] = defaultdict(list)
    for article_id, doc_id, numero, contenu in article_rows:
        doc_id = str(doc_id)
        article_to_doc[str(article_id)] = doc_id
        signals[doc_id]["article_count"] += 1
        signals[doc_id]["numero_articles"].add(numero)
        text_buffers[doc_id].append(f"{numero}␟{contenu or ''}")

    for doc_id, parts in text_buffers.items():
        signals[doc_id]["content_hash"] = hashlib.sha256("␞".join(parts).encode("utf-8")).hexdigest()
        signals[doc_id]["content_text_sample"] = "␞".join(parts)[:20000]

    flag_rows = db.execute(text("""
        select document_id, severity, count(*) filter (where resolved = false)
        from curation_flags where document_id = any(cast(:ids as uuid[])) group by document_id, severity
    """), {"ids": doc_ids}).all()
    for doc_id, severity, count in flag_rows:
        doc_id = str(doc_id)
        if severity == "blocking":
            signals[doc_id]["flags_blocking"] = count
        elif severity == "warning":
            signals[doc_id]["flags_warning"] = count

    checksum_rows = db.execute(text("""
        select document_id, file_category, checksum_sha256 from media_files
        where document_id = any(cast(:ids as uuid[])) and file_category in ('SOURCE_PDF', 'EXTRACTION_MARKDOWN')
    """), {"ids": doc_ids}).all()
    for doc_id, category, checksum in checksum_rows:
        signals[str(doc_id)]["checksums"][category] = checksum

    # dossier_references (table Laravel — AUCUNE contrainte FK vers
    # legal_documents/articles : un soft-delete ne la nettoie pas et ne la
    # bloque pas, donc ces cibles doivent être vérifiées à la main avant tout
    # dédoublonnage. target_id peut désigner soit le document, soit un de ses
    # articles (colonne `type`).
    all_targets = doc_ids + list(article_to_doc.keys())
    ref_rows = db.execute(text("""
        select target_id, type, dossier_id from dossier_references
        where target_id = any(cast(:ids as uuid[]))
    """), {"ids": all_targets}).all()
    for target_id, ref_type, dossier_id in ref_rows:
        target_id = str(target_id)
        owner_doc = target_id if target_id in signals else article_to_doc.get(target_id)
        if owner_doc:
            signals[owner_doc]["dossier_hits"].append({
                "target_id": target_id, "type": ref_type, "dossier_id": str(dossier_id),
            })

    return signals


def text_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


# --- Tiering + recommandation (jamais exécutée depuis ce script) -------------

CURATION_RANK = {"published": 3, "validated": 2, "review": 1, "draft": 0, "parsed": 0}


def assess_cluster(key: tuple, docs: list[dict[str, Any]], signals: dict[str, dict[str, Any]]) -> dict[str, Any]:
    act_type, act_number_norm, date_signature = key
    ids = [str(d["id"]) for d in docs]

    hashes = {signals[i]["content_hash"] for i in ids if signals[i]["content_hash"]}
    exact_content_match = len(hashes) == 1 and len(hashes) > 0 and all(signals[i]["content_hash"] for i in ids)

    source_checksums = {signals[i]["checksums"].get("SOURCE_PDF") for i in ids}
    source_checksums.discard(None)
    exact_source_match = len(source_checksums) == 1 and len(source_checksums) > 0

    article_counts = {signals[i]["article_count"] for i in ids}
    equal_article_counts = len(article_counts) == 1 and 0 not in article_counts

    min_jaccard = 1.0
    min_text_sim = 1.0
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            min_jaccard = min(min_jaccard, jaccard(signals[ids[i]]["numero_articles"], signals[ids[j]]["numero_articles"]))
            min_text_sim = min(min_text_sim, text_similarity(
                signals[ids[i]].get("content_text_sample", ""), signals[ids[j]].get("content_text_sample", ""),
            ))

    # official_journal_id : signal découvert empiriquement (pas anticipé au
    # départ) en creusant les 19 groupes remontés en production — TOUS portent
    # metadata->>'routage_jo_corrige' et un created_at du 2026-07-21 ou du
    # 2026-08-02 (la remédiation qui a réingéré 52 JO ce jour-là). Sur plusieurs
    # groupes, une seule copie a `official_journal_id` renseigné (rattachement
    # correct au JO), l'autre l'a NULL (reliquat d'un passage antérieur/
    # intermédiaire de la même réingestion, jamais retiré) — bien plus fiable
    # que le contenu pour trancher, car il vient de la routine même qui a créé
    # le doublon, pas d'une heuristique de similarité de texte a posteriori.
    jo_ids = {str(d["official_journal_id"]) for d in docs if d.get("official_journal_id")}
    routed_docs = [d for d in docs if d.get("official_journal_id")]
    single_routed = len(routed_docs) == 1 and len(routed_docs) < len(docs) and len(jo_ids) == 1

    if exact_content_match or single_routed:
        tier = "TIER_1_QUASI_CERTAIN"
    elif equal_article_counts and min_jaccard >= 0.8 and min_text_sim >= 0.85:
        tier = "TIER_2_FORT"
    else:
        tier = "TIER_3_A_VERIFIER"

    document_roles = {d["document_role"] for d in docs}
    published = [d for d in docs if d["curation_status"] == "published"]

    warnings = []
    if len(published) > 1:
        warnings.append("PLUSIEURS_COPIES_PUBLIEES : décision humaine obligatoire, ne jamais automatiser.")
    if len(document_roles) > 1:
        warnings.append(f"document_role divergent dans le groupe ({sorted(document_roles)}) — pas un doublon d'ingestion simple.")
    if len(jo_ids) > 1:
        warnings.append(f"official_journal_id divergent ({sorted(jo_ids)}) — possible mauvais routage plutôt qu'un doublon simple, à vérifier au cas par cas.")
    if exact_source_match and not exact_content_match:
        warnings.append(
            "exact_source_pdf_match=true mais NE PROUVE RIEN seul : dans ce pipeline, TOUS les actes d'un même "
            "JO partagent le même PDF source (vérifié empiriquement) — ce n'est qu'un indice de même lot d'ingestion."
        )
    docs_with_refs = [str(d["id"]) for d in docs if signals[str(d["id"])]["dossier_hits"]]
    if docs_with_refs:
        warnings.append(f"dossier_references pointe sur {docs_with_refs} (aucune FK, à vérifier/migrer avant suppression).")

    # Classement, du meilleur candidat "à garder" au pire : statut de curation
    # le plus avancé, puis rattachement JO correct (signal le plus fiable,
    # cf. ci-dessus), puis le plus d'articles vivants, puis le moins de flags
    # blocking non résolus, puis (en dernier recours) la plus ANCIENNE
    # ingestion — le `-timestamp` inverse l'ordre naturel pour que "plus tôt"
    # gagne sous le `reverse=True` global (toutes les autres clés sont déjà
    # "plus grand = mieux").
    ranked = sorted(
        docs,
        key=lambda d: (
            CURATION_RANK.get(d["curation_status"], -1),
            1 if d.get("official_journal_id") else 0,
            signals[str(d["id"])]["article_count"],
            -signals[str(d["id"])]["flags_blocking"],
            -d["created_at"].timestamp(),
        ),
        reverse=True,
    )

    if len(published) == 1:
        recommended_keep = str(published[0]["id"])
    elif len(published) > 1 or tier == "TIER_3_A_VERIFIER":
        recommended_keep = None  # décision humaine obligatoire, cf. warnings
    else:
        recommended_keep = str(ranked[0]["id"])

    return {
        "act_type": act_type,
        "act_number_norm": act_number_norm,
        "date_signature": str(date_signature),
        "tier": tier,
        "warnings": warnings,
        "documents": [
            {
                "id": str(d["id"]),
                "titre_officiel": d["titre_officiel"],
                "curation_status": d["curation_status"],
                "document_role": d["document_role"],
                "created_at": d["created_at"].isoformat(),
                "official_journal_id": str(d["official_journal_id"]) if d.get("official_journal_id") else None,
                "routage_jo_corrige": d.get("routage_jo_corrige"),
                "article_count": signals[str(d["id"])]["article_count"],
                "flags_blocking": signals[str(d["id"])]["flags_blocking"],
                "flags_warning": signals[str(d["id"])]["flags_warning"],
                "content_hash": signals[str(d["id"])]["content_hash"],
                "source_pdf_checksum": signals[str(d["id"])]["checksums"].get("SOURCE_PDF"),
                "dossier_hits": signals[str(d["id"])]["dossier_hits"],
            }
            for d in docs
        ],
        "signals": {
            "exact_content_match": exact_content_match,
            "exact_source_pdf_match": exact_source_match,
            "equal_article_counts": equal_article_counts,
            "min_numero_article_jaccard": round(min_jaccard, 3),
            "min_text_similarity": round(min_text_sim, 3),
        },
        "recommended_keep": recommended_keep,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", choices=["dev", "prod"], default="dev", help="Cible (défaut : dev).")
    parser.add_argument("--out", type=Path, default=None, help="Chemin du rapport JSON complet (optionnel).")
    args = parser.parse_args()

    db = _session_prod_readonly() if args.target == "prod" else _session_dev()

    documents = fetch_live_documents(db)
    print(f"Cible : {args.target}. Documents vivants : {len(documents)}.")

    with_date = [d for d in documents if d["date_signature"] is not None]
    with_number = [d for d in with_date if extract_act_number(d["titre_officiel"])]
    print(f"  … avec date_signature : {len(with_date)}")
    print(f"  … dont numéro d'acte extractible : {len(with_number)} "
          f"({len(with_date) - len(with_number)} titres génériques/sans numéro — hors périmètre passe 1, "
          "true dédoublonnage éventuel réservé à une passe contenu séparée).")

    clusters = build_clusters(documents)
    print(f"Groupes candidats (même type + numéro normalisé + date) : {len(clusters)}")

    all_ids = [str(d["id"]) for docs in clusters.values() for d in docs]
    signals = fetch_content_signals(db, all_ids) if all_ids else {}

    results = [assess_cluster(key, docs, signals) for key, docs in clusters.items()]
    results.sort(key=lambda r: (r["tier"], r["act_number_norm"]))

    by_tier = defaultdict(list)
    for r in results:
        by_tier[r["tier"]].append(r)

    print("\nRépartition par niveau de confiance :")
    for tier in ("TIER_1_QUASI_CERTAIN", "TIER_2_FORT", "TIER_3_A_VERIFIER"):
        print(f"  {tier:<22} {len(by_tier[tier])} groupe(s)")

    print("\n--- TIER_1_QUASI_CERTAIN (détail) ---")
    for r in by_tier["TIER_1_QUASI_CERTAIN"]:
        titres = [d["titre_officiel"][:80] for d in r["documents"]]
        print(f"  [{r['act_type']} n°{r['act_number_norm']} du {r['date_signature']}] {len(r['documents'])} copies")
        for t in titres:
            print(f"      - {t}")
        if r["warnings"]:
            for w in r["warnings"]:
                print(f"      ⚠ {w}")
        print(f"      → garder recommandé : {r['recommended_keep']}")

    if args.out:
        args.out.write_text(json.dumps({
            "cible": args.target,
            "documents_vivants": len(documents),
            "avec_date_signature": len(with_date),
            "avec_numero_extractible": len(with_number),
            "groupes": results,
        }, indent=2, ensure_ascii=False, default=str))
        print(f"\nRapport complet écrit : {args.out}")


if __name__ == "__main__":
    main()


# --- Stratégie de dédoublonnage (PROPOSÉE, PAS EXÉCUTÉE PAR CE SCRIPT) --------
#
# 1. Ne jamais toucher un groupe TIER_3_A_VERIFIER automatiquement — les
#    signaux de contenu ne s'accordent pas assez pour trancher sans oeil humain
#    (cf. exemples confirmés où chaque copie a ses propres flags
#    article_manquant : une extraction peut être franchement moins complète
#    que l'autre, ce qui casse equal_article_counts sans que ce soit un faux
#    positif administratif).
# 2. Groupe avec >1 copie `published` : jamais d'automatisation, alerte
#    humaine immédiate (deux versions publiques concurrentes d'la même loi).
# 3. Sinon, copie à garder = celle recommandée par `recommended_keep`
#    (curation_status le plus avancé, puis `official_journal_id` correctement
#    renseigné — signal le plus fiable trouvé en pratique : les 19 groupes
#    remontés en prod datent tous du 2026-07-21 ou du 2026-08-02, jour de la
#    réingestion de 52 JO — sur 10 d'entre eux, une copie a le bon
#    official_journal_id et l'autre l'a NULL, reliquat d'un passage
#    intermédiaire de cette même réingestion jamais retiré ; ce signal vient
#    de la routine qui a créé le doublon, donc plus fiable qu'une similarité
#    de contenu a posteriori — puis le plus d'articles vivants, puis le moins
#    de flags blocking non résolus, puis la plus ANCIENNE ingestion en
#    dernier recours).
# 4. AVANT toute suppression de la copie perdante : vérifier `dossier_hits`
#    sur CETTE copie précise (document ET chacun de ses articles) — table
#    `dossier_references`, sans contrainte FK vers legal_documents/articles,
#    donc un soft-delete ne la nettoie ni ne la bloque : une référence
#    utilisateur y deviendrait silencieusement orpheline. S'il y en a,
#    migrer `target_id` vers l'entité correspondante de la copie gardée
#    (même dossier_id, mapper article-à-article par numero_article) avant de
#    supprimer, ou à défaut prévenir l'utilisateur concerné.
# 5. La suppression elle-même = soft-delete Eloquent côté Laravel
#    (`LegalDocument::find($id)->delete()`), PAS une écriture SQL directe
#    depuis ce dépôt Python : c'est le `booted()` de `App\Models\LegalDocument`
#    qui cascade le soft-delete vers `articles()->delete()`, et
#    `owen-it/auditing` (Auditable) journalise l'opération — une écriture SQL
#    brute perdrait les deux. Concrètement : une commande artisan dédiée,
#    dry-run par défaut, listant les paires et n'agissant que sur
#    confirmation explicite — jamais un DELETE physique (interdit absolu,
#    cf. CLAUDE.md racine), jamais un `UPDATE curation_status` (publication =
#    API Laravel uniquement).
# 6. Dump frais avant toute exécution, comme pour toute écriture prod.
