#!/usr/bin/env python3
"""Remédiation 2026-08-02/03 phase 6 : résout 46 `CurationFlag` `bloc_manquant`
/`article_manquant`/`article_doublon`/`compilation_suspectee` confirmés FAUX
POSITIFS par une investigation en lecture seule (workflow à 8 agents,
2026-08-03) contre le markdown MinerU réellement stocké — jamais contre une
supposition.

Schéma récurrent confirmé (transitions propres, marqueurs de page continus,
aucune phrase tronquée) : soit un vrai trou historique de numérotation (Code
civil : réforme des sûretés 2006 ayant abrogé tout un titre ; Code Pénal 1836 :
citations d'autres textes en notes de bas de page, pas une compilation), soit
une loi/ordonnance MODIFICATIVE qui ne cite que quelques numéros épars d'un
texte de base (Constitution, code des impôts, code du travail) sans jamais
prétendre le reproduire en entier — le détecteur suppose à tort une
numérotation continue de 1 au numéro max trouvé.

Chaque flag ci-dessous est résolu INDIVIDUELLEMENT avec sa justification
propre (jamais un blanket UPDATE par document ou par type_probleme) : deux
documents (Loi n°076-84 et Ordonnance n°019-84) portent un mélange de flags
légitimes ET d'un vrai bug de rattachement croisé (19 articles de l'Ordonnance
glissés par erreur dans la Loi de ratification voisine, cf. mémoire) — SEULS
les flags listés ici sont résolus, le reste (article_doublon/compilation_
suspectee liés au vrai bug, et les 68 article_doublon du Code Pénal 1836 qui
demandent une revue humaine ciblée) reste ouvert intentionnellement.

Le contenu texte n'est JAMAIS modifié : `resolved` passe à true et une note
horodatée est AJOUTÉE (jamais substituée) à la description existante, pour
garder une trace intégrale de ce qui a été signalé puis pourquoi ce n'était
pas un défaut.

Usage (dev — défaut) :
    python scripts/resolve_false_positive_flags.py                    # dry-run
    python scripts/resolve_false_positive_flags.py --execute          # écrit sur le dev local

Usage (prod) :
    python scripts/resolve_false_positive_flags.py --target prod                  # dry-run, profil PROD_RO_*
    python scripts/resolve_false_positive_flags.py --target prod --execute        # écrit en PROD, profil PROD_RW_DB_*

Garde-fous : mêmes profils/vérifications que scripts/reclassify_embedded_series_flags.py
(dump frais requis avant tout --execute --target prod, saisie « PRODUCTION »
exigée). Chaque flag est revérifié individuellement juste avant écriture
(existe, appartient au bon document, encore non résolu) — un flag qui ne
correspond plus est SIGNALÉ et IGNORÉ, jamais forcé.

Procédure d'annulation : ce script ne fait qu'un UPDATE (resolved, description)
sur des lignes `curation_flags` déjà identifiées par id — jamais de DELETE. Un
dump pris juste avant restaure l'état antérieur intégralement.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.db.models import CurationFlag  # noqa: E402

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "5433")

NOTE_HISTORIQUE = (
    "[RÉSOLU 2026-08-03, investigation lecture seule contre le markdown MinerU stocké] "
    "Trou de numérotation historique confirmé légitime — transition propre (aucune phrase "
    "tronquée, marqueurs de page continus), pas une perte de contenu à l'extraction."
)
NOTE_ANNOTE = (
    "[RÉSOLU 2026-08-03, investigation lecture seule contre le markdown MinerU stocké] "
    "Ce document est un recueil annoté qui cite d'autres textes/codes en notes de bas de "
    "page (préambule historique, extraits de lois spécialisées) — pas une compilation de "
    "plusieurs actes juxtaposés à scinder. Les article_doublon liés à ces citations "
    "restent ouverts pour revue humaine (cf. mémoire code_penal_reliability)."
)
NOTE_LOI_MODIFICATIVE = (
    "[RÉSOLU 2026-08-03, investigation lecture seule contre le markdown MinerU stocké] "
    "Cet acte est une loi/ordonnance MODIFICATIVE qui ne cite que des numéros épars d'un "
    "texte de base (Constitution, code des impôts, code du travail) sans jamais prétendre "
    "le reproduire en entier — le texte source est intégralement présent, aucune perte."
)
NOTE_CITATION_UNIQUE = (
    "[RÉSOLU 2026-08-03, investigation lecture seule contre le markdown MinerU stocké] "
    "Le numéro « article cité » correspond à la reproduction d'un article externe modifié "
    "(ex. « Article 47 (Nouveau) » de la Constitution cité en entier dans une loi "
    "d'amendement) — pas un article autonome du document, pas un doublon réel."
)

# Périmètre figé : chaque tuple (flag_id, document_id, type_probleme_attendu,
# note) a été individuellement vérifié par un agent d'investigation en lecture
# seule (workflow 2026-08-03, 8 agents, 12 documents) contre le markdown déjà
# stocké. AUCUNE requête dynamique — voir docstring pour le pourquoi des
# exclusions (Loi 076-84 / Ordonnance 019-84 partiellement hors périmètre).
FLAGS_A_RESOUDRE: List[Dict[str, str]] = [
    # --- Code civil (a01036db) — 6 bloc_manquant, trous historiques (réforme
    # des sûretés 2006 notamment) ---
    {"id": "26339910-15ea-400d-b051-86db684350a2", "document_id": "a01036db-9512-46fa-9cfa-5f0f3c16e040", "type": "bloc_manquant", "note": NOTE_HISTORIQUE},
    {"id": "bcfadbd9-7898-4eba-b573-8894940dc96c", "document_id": "a01036db-9512-46fa-9cfa-5f0f3c16e040", "type": "bloc_manquant", "note": NOTE_HISTORIQUE},
    {"id": "50efbc9d-21b5-47eb-bd0c-503eb6f44ca7", "document_id": "a01036db-9512-46fa-9cfa-5f0f3c16e040", "type": "bloc_manquant", "note": NOTE_HISTORIQUE},
    {"id": "0a2615f2-af90-4971-9be4-a91968f9fbef", "document_id": "a01036db-9512-46fa-9cfa-5f0f3c16e040", "type": "bloc_manquant", "note": NOTE_HISTORIQUE},
    {"id": "e66f54db-9920-4fc4-b550-b4a5def0941e", "document_id": "a01036db-9512-46fa-9cfa-5f0f3c16e040", "type": "bloc_manquant", "note": NOTE_HISTORIQUE},
    {"id": "16b16d84-7c15-4d2c-a4a9-338cce8f3732", "document_id": "a01036db-9512-46fa-9cfa-5f0f3c16e040", "type": "bloc_manquant", "note": NOTE_HISTORIQUE},

    # --- Code Pénal — Penal-Code-1836 (67925559) — 2 bloc_manquant +
    # compilation_suspectee : recueil annoté, pas une compilation. Les 68
    # article_doublon restent HORS périmètre (revue humaine ciblée, 1 coquille
    # OCR confirmée art. 26→20). ---
    {"id": "f25c613c-1a69-4e9a-954a-5df18f53733d", "document_id": "67925559-0250-4c72-929a-68e2530b4197", "type": "bloc_manquant", "note": NOTE_ANNOTE},
    {"id": "52a87ada-f411-45d4-bb02-4de9b509635c", "document_id": "67925559-0250-4c72-929a-68e2530b4197", "type": "bloc_manquant", "note": NOTE_ANNOTE},
    {"id": "295de05a-1271-40d9-9d46-970ce12dfe0d", "document_id": "67925559-0250-4c72-929a-68e2530b4197", "type": "compilation_suspectee", "note": NOTE_ANNOTE},

    # --- LOI n°001-90 (43257af7) — révision constitutionnelle, cite des
    # articles externes par numéro ---
    {"id": "29f89f91-cc1f-48b2-a2ee-3e54c2974930", "document_id": "43257af7-2568-4e3c-975b-f3b150fac889", "type": "bloc_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "da4c33b6-d4d6-4f34-9fcd-d14fbab7e8a3", "document_id": "43257af7-2568-4e3c-975b-f3b150fac889", "type": "bloc_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "673b2144-a998-4c2e-93c0-6bf50af0c0e2", "document_id": "43257af7-2568-4e3c-975b-f3b150fac889", "type": "bloc_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "2fecd80f-54ca-4fd1-8947-17390ec70087", "document_id": "43257af7-2568-4e3c-975b-f3b150fac889", "type": "compilation_suspectee", "note": NOTE_LOI_MODIFICATIVE},

    # --- LOI n°23/59 (1d07aa5e) — modifie le code des impôts directs, ne cite
    # que des numéros épars (dont l'art.27 cité deux fois pour deux
    # amendements distincts, d'où l'article_doublon légitime) ---
    {"id": "f92ba647-a5a2-4be0-b92c-962b59142954", "document_id": "1d07aa5e-c634-4eeb-9b9d-acc136dadd88", "type": "article_doublon", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "a562d4b1-db68-45e8-8e20-cb8bc34d0a89", "document_id": "1d07aa5e-c634-4eeb-9b9d-acc136dadd88", "type": "article_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "b7a63e63-2561-4bb2-bc9c-2844cee3f771", "document_id": "1d07aa5e-c634-4eeb-9b9d-acc136dadd88", "type": "bloc_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "dc5af257-72d6-48da-8d7f-37ab0f9e9127", "document_id": "1d07aa5e-c634-4eeb-9b9d-acc136dadd88", "type": "bloc_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "acd583df-b39f-4290-a37b-c2299b50e5cd", "document_id": "1d07aa5e-c634-4eeb-9b9d-acc136dadd88", "type": "bloc_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "e960ae97-1163-4271-9d70-9fea6123d248", "document_id": "1d07aa5e-c634-4eeb-9b9d-acc136dadd88", "type": "bloc_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "1c5e2372-29ce-4b6d-815e-d4fea4bc877e", "document_id": "1d07aa5e-c634-4eeb-9b9d-acc136dadd88", "type": "bloc_manquant", "note": NOTE_LOI_MODIFICATIVE},

    # --- Loi n°49-59 (0343894d) — même schéma, code des impôts directs ---
    {"id": "662b6bc8-e0a6-48db-bf4a-d62d70e0665c", "document_id": "0343894d-af7f-4571-ab00-9bec86e5f3eb", "type": "article_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "de379d6b-7ac3-4f3a-b830-66b566d85aec", "document_id": "0343894d-af7f-4571-ab00-9bec86e5f3eb", "type": "bloc_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "53638a04-6af8-46fc-bcfa-b1ca77c4ce65", "document_id": "0343894d-af7f-4571-ab00-9bec86e5f3eb", "type": "bloc_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "f0ed7a76-2076-44e1-a11b-6cce66137b1a", "document_id": "0343894d-af7f-4571-ab00-9bec86e5f3eb", "type": "bloc_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "7048e1a4-bf90-4d37-8cd2-566d894fe29e", "document_id": "0343894d-af7f-4571-ab00-9bec86e5f3eb", "type": "bloc_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "73abb9de-6b3c-4409-a3f6-0cb36617bc6f", "document_id": "0343894d-af7f-4571-ab00-9bec86e5f3eb", "type": "bloc_manquant", "note": NOTE_LOI_MODIFICATIVE},

    # --- Loi n°47-59 (d160ef09) — même schéma, code des impôts/enregistrement ---
    {"id": "7835b9d8-9c9a-4ed7-83c6-9f2a6ced5668", "document_id": "d160ef09-d795-4140-b53e-c534662a91ef", "type": "bloc_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "ad56772b-3cbd-4dd8-bc8a-6cc441559124", "document_id": "d160ef09-d795-4140-b53e-c534662a91ef", "type": "bloc_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "05497b92-de6b-4723-85ee-9b8aaf62a55c", "document_id": "d160ef09-d795-4140-b53e-c534662a91ef", "type": "bloc_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "3f58c3aa-71c7-4942-a847-cbfb76cae6ab", "document_id": "d160ef09-d795-4140-b53e-c534662a91ef", "type": "bloc_manquant", "note": NOTE_LOI_MODIFICATIVE},

    # --- LOI n°076-84 (ff2895a1) — SOUS-ENSEMBLE SEULEMENT : ratifie
    # l'Ordonnance 019-84, dont elle ne cite que des articles constitutionnels
    # précis. Le article_doublon "2_doublon_1" (warning) et le
    # compilation_suspectee (warning) restent HORS périmètre : liés au vrai
    # bug de rattachement croisé avec de681be4 (cf. NOTE_LOI_MODIFICATIVE plus
    # bas pour la partie légitime uniquement). ---
    {"id": "6a285b27-1a28-49d0-8dbe-017c1488a923", "document_id": "ff2895a1-3412-45bd-9a77-7ffedade93c7", "type": "article_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "36a1a77c-cb8c-4dd3-8316-433fa47b03b0", "document_id": "ff2895a1-3412-45bd-9a77-7ffedade93c7", "type": "article_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "09f05f0b-d2d7-4a5a-b9e9-225e67f0ffef", "document_id": "ff2895a1-3412-45bd-9a77-7ffedade93c7", "type": "article_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "ad5b4ecb-57f3-4da2-8acc-fb036fbfb487", "document_id": "ff2895a1-3412-45bd-9a77-7ffedade93c7", "type": "bloc_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "8b6122bb-5156-41b2-affb-42538a130cd0", "document_id": "ff2895a1-3412-45bd-9a77-7ffedade93c7", "type": "bloc_manquant", "note": NOTE_LOI_MODIFICATIVE},

    # --- ORDONNANCE n°019-84 (de681be4) — SOUS-ENSEMBLE SEULEMENT : modifie
    # des articles constitutionnels précis. article_manquant 51-56/59-60/64-65/
    # 70-76/86-92 et les 2 article_doublon (warning, "3"/"48") restent HORS
    # périmètre : leur contenu existe réellement mais est actuellement
    # rattaché par erreur au document voisin ff2895a1 (vrai bug de
    # rattachement croisé, cf. mémoire — nécessite un déplacement de contenu,
    # pas une résolution de flag). ---
    {"id": "17c8bfd2-cc4c-44c8-b3db-8d37a004df22", "document_id": "de681be4-6e9c-4673-8992-c0d5bacf50f8", "type": "article_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "835f4e3b-20a5-4fa9-8b7d-f123592f7b16", "document_id": "de681be4-6e9c-4673-8992-c0d5bacf50f8", "type": "article_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "29c9ae69-6802-48c6-8ea8-20e6ce8ccc05", "document_id": "de681be4-6e9c-4673-8992-c0d5bacf50f8", "type": "article_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "eb461572-d619-4aff-b2a4-834abd371b49", "document_id": "de681be4-6e9c-4673-8992-c0d5bacf50f8", "type": "article_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "54cdf92b-4847-4427-9334-6bc4eaaaf4ca", "document_id": "de681be4-6e9c-4673-8992-c0d5bacf50f8", "type": "bloc_manquant", "note": NOTE_LOI_MODIFICATIVE},

    # --- LOI n°25-80, les deux copies (dossier doublon-document distinct, non
    # traité ici) — citation de l'art.47 (Nouveau) de la Constitution, mal
    # comptée comme article autonome ---
    {"id": "4e76014c-45e3-43be-b6e2-b804a94d0028", "document_id": "ae46ccd2-3b94-4405-922a-2da1d93b2a1f", "type": "bloc_manquant", "note": NOTE_CITATION_UNIQUE},
    {"id": "5afbfe30-862b-4bc7-a749-ab2acbef57e0", "document_id": "46a0f5c9-16d6-4ca5-9945-e00762704e18", "type": "bloc_manquant", "note": NOTE_CITATION_UNIQUE},

    # --- Décret n°59-237 (2eb79797) — 3 articles réels (nomination/non-
    # application/publication), "18" est une coquille OCR de "1er" ---
    {"id": "0a8c577a-6407-4c17-9901-ed11baab1bd4", "document_id": "2eb79797-d022-4e51-b80b-b60ad6ee951d", "type": "bloc_manquant", "note": NOTE_HISTORIQUE},

    # --- ORDONNANCE n°41-69 (38fc7b04) — réécrit les art.171-177 du code du
    # travail, l'annonce explicitement dans son propre texte ---
    {"id": "e49e5201-bc43-4aea-928d-117952d02d57", "document_id": "38fc7b04-f24d-43d9-a8c6-6e265e897b69", "type": "bloc_manquant", "note": NOTE_LOI_MODIFICATIVE},

    # --- Décret n°2025-399 (dbc6c151) — modifie l'art.22 d'un décret de 2011,
    # citation mal comptée comme article autonome ---
    {"id": "f5276b64-3857-4068-851e-5c4a3c46fff6", "document_id": "dbc6c151-891d-4e2f-9efa-bc4eb62f02b7", "type": "bloc_manquant", "note": NOTE_CITATION_UNIQUE},

    # --- DELIBERATION n°112/58 (37dc9a58) — 3 articles réels + signature,
    # texte intégralement présent (défaut de découpage pur, pas de perte) ---
    {"id": "250bf95f-6e26-4baf-b9eb-fcae98ccb150", "document_id": "37dc9a58-927d-416b-af9e-8eba9538492f", "type": "bloc_manquant", "note": NOTE_HISTORIQUE},

    # --- Ordonnance 019-84 (de681be4) — RE-résolution 2026-08-03 après le
    # correctif de rattachement croisé (scripts/fix_ordonnance019_loi076_
    # rattachement.py) : ingest_hierarchy régénère TOUJOURS les flags
    # heuristiques à chaque reconstruction de structure, sans mémoire des
    # résolutions précédentes — ces 3 flags avaient déjà été validés
    # légitimes ci-dessus (même document, mêmes plages, même justification),
    # ils reviennent simplement sous de nouveaux id après la reconstruction.
    # Le 4e flag actuel (article_doublon « 3 ») reste HORS périmètre : vraie
    # collision Constitution/Ordonnance nécessitant une revue humaine.
    {"id": "39af1834-61ea-4b88-8929-0f72806b4a9f", "document_id": "de681be4-6e9c-4673-8992-c0d5bacf50f8", "type": "bloc_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "6352ccfe-dfa4-4482-9903-d68e95ec086b", "document_id": "de681be4-6e9c-4673-8992-c0d5bacf50f8", "type": "bloc_manquant", "note": NOTE_LOI_MODIFICATIVE},
    {"id": "8213fb90-83ef-48a1-9122-6ac6a3b3335a", "document_id": "de681be4-6e9c-4673-8992-c0d5bacf50f8", "type": "compilation_suspectee", "note": NOTE_LOI_MODIFICATIVE},
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


def resolve_one(db, entry: Dict[str, str], execute: bool) -> Dict[str, Any]:
    flag = db.query(CurationFlag).filter(CurationFlag.id == entry["id"]).first()
    if flag is None:
        return {**entry, "statut": "introuvable"}
    if str(flag.document_id) != entry["document_id"]:
        return {**entry, "statut": f"REFUS : document_id actuel {flag.document_id} != attendu {entry['document_id']}"}
    if flag.type_probleme != entry["type"]:
        return {**entry, "statut": f"REFUS : type_probleme actuel {flag.type_probleme!r} != attendu {entry['type']!r}"}
    if flag.resolved:
        return {**entry, "statut": "déjà résolu (ignoré)"}

    resultat = {**entry, "statut": "à résoudre" if not execute else "résolu", "description_avant": flag.description}
    if execute:
        flag.resolved = True
        flag.description = f"{flag.description}\n\n{entry['note']}"
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

    print(f"Cible : {args.target}. Périmètre : {len(FLAGS_A_RESOUDRE)} flags (liste figée, investigation 2026-08-03).")

    if args.execute and args.target == "prod":
        print(f"\n  {len(FLAGS_A_RESOUDRE)} flags PROD vont être marqués resolved=true (curation_flags.description complétée, jamais remplacée).")
        saisie = input("Taper PRODUCTION pour confirmer : ").strip()
        if saisie != "PRODUCTION":
            print("Annulé.")
            sys.exit(1)
        db = _session_prod_ecriture(engine_ro)

    resultats = [resolve_one(db, entry, execute=args.execute) for entry in FLAGS_A_RESOUDRE]

    if args.execute:
        db.commit()
        print(f"\n--execute ({args.target}) : modifications validées (COMMIT).")
    else:
        db.rollback()
        print(f"\nDRY-RUN ({args.target}) : aucune écriture (ROLLBACK). Relancer avec --execute pour appliquer.")

    for r in resultats:
        print({k: v for k, v in r.items() if k != "note"})

    statuts = [r["statut"] for r in resultats]
    print(f"\nRésumé : {statuts.count('à résoudre') + statuts.count('résolu')}/{len(resultats)} conformes, "
          f"{statuts.count('déjà résolu (ignoré)')} déjà résolus, "
          f"{sum(1 for s in statuts if s.startswith('REFUS') or s == 'introuvable')} anomalies (voir détail ci-dessus).")


if __name__ == "__main__":
    main()
