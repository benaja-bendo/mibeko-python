"""Pousse le corpus validé du développement vers la production, de façon ADDITIVE.

Doctrine (cf. docs/infra/production.md et docs/missions/mission-usine-a-textes.md)
--------------------------------------------------------------------------------------------
- **Additif, jamais destructif** : on n'ajoute que des documents absents de la cible ;
  les documents existants de la production ne sont jamais modifiés ni supprimés. La
  production porte des références utilisateur SANS contrainte FK (tags, dossiers) :
  remplacer un document les orphelinerait en silence.
- **Copie, pas rejeu** : le contenu poussé est celui, déjà structuré et validé, de la
  base de développement. Rejouer le pipeline contre la production rappellerait le LLM
  (lent, coûteux, non déterministe, sans cache) et pourrait produire un corpus
  différent de celui qui a été relu.
- **Staging uniquement** : tout document arrive en `curation_status='draft'`, même
  s'il était publié en développement. La publication passe par l'API Laravel, jamais
  par une écriture directe.
- **Dédoublonnage par provenance** : un document source (PDF) déjà présent dans la
  cible — même SHA-256 — n'est pas repoussé, quelle que soit sa forme locale.
- **Rejouable et idempotent** : un document déjà poussé (même id) est sauté ; un
  objet MinIO déjà présent n'est pas retéléversé ; relancer la commande ne fait que
  compléter ce qui manque.
- **Dry-run par défaut** : sans `--execute`, aucune écriture ; le plan est calculé
  via le profil de LECTURE SEULE (PROD_RO_*). Les identifiants d'écriture
  (PROD_RW_DB_*, PROD_RW_MINIO_*) ne sont jamais lus depuis un fichier : ils
  s'exportent à la main dans le shell, le temps de l'opération autorisée.

Comme src/db/prod_readonly.py, ce module ne construit aucune connexion à l'import.
"""

import hashlib
import io
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime

from src.db.prod_readonly import CibleProdAmbigue, ConfigurationProdManquante

logger = logging.getLogger("mibeko.promotion.push_corpus")

# Colonnes jamais copiées, quelle que soit la table :
# - embedding / search_tsv : régénérés côté cible (cron d'embeddings, trigger tsv) ;
# - reviewed_* / resolved_* : référencent des utilisateurs qui n'existent que dans
#   la base source — une revue se refait dans l'environnement où elle est visible.
COLONNES_EXCLUES = {
    "embedding",
    "search_tsv",
    "reviewed_by",
    "reviewed_at",
    "resolved_by",
    "resolved_at",
}

# Ordre d'insertion dicté par les FK : nodes avant articles (parent_node_id),
# media_files avant extraction_runs (source_media_file_id) et avant
# article_versions (source_media_file_id), extraction_runs avant article_versions
# (source_run_id), tout avant curation_flags (node_id/article_id/run_id).
TABLES_PAR_DOCUMENT = (
    ("legal_documents", "id = %(doc)s"),
    ("media_files", "document_id = %(doc)s"),
    ("extraction_runs", "document_id = %(doc)s"),
    ("structure_nodes", "document_id = %(doc)s"),
    ("articles", "document_id = %(doc)s"),
    (
        "article_versions",
        "article_id in (select id from articles where document_id = %(doc)s)",
    ),
    (
        "curation_flags",
        "document_id = %(doc)s"
        " or article_id in (select id from articles where document_id = %(doc)s)",
    ),
)


@dataclass(frozen=True)
class DocumentSource:
    """Identité d'un document candidat au push, telle que lue dans la source."""

    id: str
    titre: str
    slug: str | None
    document_key: str | None
    stock_code: str | None
    reference_nor: str | None
    official_journal_id: str | None
    checksums_sources: frozenset  # SHA-256 de ses PDF sources

    def libelle(self) -> str:
        return self.stock_code or self.document_key or self.titre[:60]


@dataclass(frozen=True)
class JournalSource:
    id: str
    publication_date: str
    number: str | None


@dataclass(frozen=True)
class EtatCible:
    """Photographie de la cible : tout ce qui peut entrer en collision."""

    ids_documents: frozenset
    checksums_sources: frozenset
    slugs: frozenset
    document_keys: frozenset
    stock_codes: frozenset
    references_nor: frozenset
    ids_journaux: frozenset
    journaux_par_date_numero: dict  # (publication_date, number) -> id cible
    institutions_par_sigle: dict  # sigle -> id cible


@dataclass
class PlanPush:
    """Résultat du calcul : quoi pousser, quoi écarter, et pourquoi."""

    a_pousser: list = field(default_factory=list)  # DocumentSource
    ecartes: list = field(default_factory=list)  # (DocumentSource, motif)
    journaux_a_creer: list = field(default_factory=list)  # JournalSource
    remap_journaux: dict = field(default_factory=dict)  # id source -> id cible
    remap_institutions: dict = field(default_factory=dict)  # id source -> id cible


def filtrer_par_document_keys(documents: list, document_keys) -> tuple:
    """Réduit la source à un jeu précis de document_key, pour pousser un
    document urgent sans attendre ni pousser tout le reste du plan.

    Sur un corpus de plusieurs dizaines de milliers de documents, calculer le
    plan complet (dry-run compris : `executer_push` fait ~7 requêtes PAR
    document, pas de traitement par lot) devient très long — constaté en
    conditions réelles le 06/08/2026 sur 52 456 documents. `--document-key`
    contourne le problème en amont, avant même `construire_plan`.

    Renvoie `(documents_filtres, manquants)` — `manquants` est l'ensemble des
    clés demandées mais absentes de la source (faute de frappe la plupart du
    temps) : à l'appelant de décider s'il s'agit d'un refus ou d'un avertissement.
    """
    voulus = set(document_keys)
    filtres = [d for d in documents if d.document_key in voulus]
    trouves = {d.document_key for d in filtres}
    return filtres, voulus - trouves


def construire_plan(
    documents: list, journaux: list, cible: EtatCible, institutions_source: dict | None = None
) -> PlanPush:
    """Calcule le plan additif. Fonction pure : aucune connexion, testable à sec.

    Un document est écarté si — dans cet ordre, le premier motif l'emporte :
    1. son id existe déjà dans la cible (déjà poussé : idempotence) ;
    2. l'un de ses PDF sources (SHA-256) existe déjà dans la cible ET ce SHA-256
       n'est partagé par AUCUN AUTRE document de la source (même texte, autre
       forme : la version de la production fait foi) ;
    3. l'une de ses clés d'unicité (slug, document_key, stock_code, reference_nor)
       est déjà prise dans la cible — pousser échouerait sur l'index unique
       (`legal_documents_slug_unique` est TOTAL, les trois `uq_legal_documents_*`
       sont partiels sur `deleted_at IS NULL`), et « le même nom » sans « la même
       source » mérite un arbitrage humain, pas un écrasement.

    Le filtre « SHA-256 partagé par aucun autre document » de la règle 2 est
    indispensable : un Journal officiel scindé produit N documents qui référencent
    TOUS le même PDF source. Constaté en conditions réelles le 07/08/2026 — les 12
    actes du JO 2023-48 partagent un unique SHA-256 ; sans ce filtre, avoir poussé
    UN SEUL de ces actes (la loi n°33-2023) aurait fait écarter les 11 autres à
    tort au push suivant, sous prétexte que « leur » source était déjà en
    production. La règle garde tout son sens pour un vrai doublon 1 PDF = 1
    document (l'usage originel : un texte unitaire réingéré sous un autre id).

    `institutions_source` (sigle -> id source, optionnel) calcule le remap
    `legal_documents.institution_id` : dev et prod ont chacun leur propre jeu
    d'UUID pour les MÊMES 7 institutions (AN, CC, CS, GOUV, JO, PR, SEN),
    générées indépendamment lors de chaque seed — constaté en conditions
    réelles le 06/08/2026, où pousser un acte de JO échouait systématiquement
    sur `legal_documents_institution_id_fkey` (id dev absent de la cible).
    Contrairement aux journaux officiels, une institution n'est jamais créée
    ici : les 7 sont un référentiel stable des deux côtés — un sigle source
    sans correspondance en cible n'est pas remappé et échouera au COPY,
    volontairement (signal net plutôt qu'une création silencieuse).
    """
    plan = PlanPush()
    journaux_par_id = {j.id: j for j in journaux}
    journaux_requis: dict = {}

    # Occurrences de chaque SHA-256 DANS LA SOURCE : un checksum porté par
    # plusieurs documents source signale un JO scindé (documents légitimement
    # distincts, même PDF) — la collision de la règle 2 ne doit alors jamais
    # s'appliquer, sous peine d'écarter toute la fratrie dès qu'un seul acte
    # a déjà été poussé.
    occurrences_checksum: dict = {}
    for doc in documents:
        for checksum in doc.checksums_sources:
            occurrences_checksum[checksum] = occurrences_checksum.get(checksum, 0) + 1

    if institutions_source:
        plan.remap_institutions = {
            id_source: cible.institutions_par_sigle[sigle]
            for sigle, id_source in institutions_source.items()
            if sigle in cible.institutions_par_sigle
            and cible.institutions_par_sigle[sigle] != id_source
        }

    for doc in documents:
        if doc.id in cible.ids_documents:
            plan.ecartes.append((doc, "déjà poussé (id présent dans la cible)"))
            continue

        checksums_non_partages = {
            c for c in doc.checksums_sources if occurrences_checksum.get(c, 0) == 1
        }
        deja_la = checksums_non_partages & cible.checksums_sources
        if deja_la:
            plan.ecartes.append(
                (doc, f"source déjà en production (SHA-256 {sorted(deja_la)[0][:12]}…)")
            )
            continue

        collision = None
        if doc.slug and doc.slug in cible.slugs:
            collision = f"slug '{doc.slug}'"
        elif doc.document_key and doc.document_key in cible.document_keys:
            collision = f"document_key '{doc.document_key}'"
        elif doc.stock_code and doc.stock_code in cible.stock_codes:
            collision = f"stock_code '{doc.stock_code}'"
        elif doc.reference_nor and doc.reference_nor in cible.references_nor:
            collision = f"reference_nor '{doc.reference_nor}'"
        if collision:
            plan.ecartes.append((doc, f"collision d'unicité sur {collision}"))
            continue

        plan.a_pousser.append(doc)
        if doc.official_journal_id:
            journaux_requis[doc.official_journal_id] = journaux_par_id.get(
                doc.official_journal_id
            )

    for jid, journal in sorted(journaux_requis.items()):
        if jid in cible.ids_journaux:
            continue  # déjà poussé (idempotence)
        if journal is None:
            # FK orpheline côté source : anomalie à corriger à la source, pas ici.
            raise ValueError(
                f"official_journal_id {jid} référencé par un document mais absent "
                "de official_journals côté source."
            )
        cle = (journal.publication_date, journal.number)
        if journal.number is not None and cle in cible.journaux_par_date_numero:
            # Même journal (date + numéro) déjà en cible sous un autre id : on
            # rattache les documents poussés à la fiche existante plutôt que de
            # violer uq_official_journals_pubdate_number.
            plan.remap_journaux[jid] = cible.journaux_par_date_numero[cle]
        else:
            plan.journaux_a_creer.append(journal)

    return plan


# ---------------------------------------------------------------------------
# Lecture des deux états
# ---------------------------------------------------------------------------


def creer_engine_source():
    """Engine vers la base de DÉVELOPPEMENT (la source du push), et elle seule.

    Refuse tout autre port que celui du dev : la source d'un push est le corpus
    validé localement, jamais une autre production.
    """
    from sqlalchemy import create_engine

    host = os.getenv("DB_HOST", "127.0.0.1").strip()
    port = os.getenv("DB_PORT", "5433").strip()
    database = (os.getenv("DB_DATABASE") or "").strip()
    username = (os.getenv("DB_USERNAME") or "").strip()
    password = os.getenv("DB_PASSWORD") or ""

    if port != "5433":
        raise CibleProdAmbigue(
            f"La SOURCE du push doit être la base de développement (port 5433), "
            f"pas {host}:{port}."
        )
    return create_engine(
        f"postgresql://{username}:{password}@{host}:{port}/{database}", echo=False
    )


def charger_documents_source(engine) -> tuple:
    """Lit les documents vivants de la source et leurs clés de collision."""
    from sqlalchemy import text

    with engine.connect() as cnx:
        lignes = cnx.execute(
            text(
                """
                select d.id::text, d.titre_officiel, d.slug, d.document_key,
                       d.stock_code, d.reference_nor, d.official_journal_id::text,
                       coalesce(
                         array_agg(m.checksum_sha256)
                           filter (where m.checksum_sha256 is not null),
                         '{}'
                       ) as checksums
                from legal_documents d
                left join media_files m
                  on m.document_id = d.id and m.file_category = 'SOURCE_PDF'
                where d.deleted_at is null
                group by d.id
                order by d.created_at, d.id
                """
            )
        ).fetchall()
        documents = [
            DocumentSource(
                id=l[0],
                titre=l[1],
                slug=l[2],
                document_key=l[3],
                stock_code=l[4],
                reference_nor=l[5],
                official_journal_id=l[6],
                checksums_sources=frozenset(l[7]),
            )
            for l in lignes
        ]
        journaux = [
            JournalSource(id=l[0], publication_date=str(l[1]), number=l[2])
            for l in cnx.execute(
                text(
                    "select id::text, publication_date, number from official_journals "
                    "where deleted_at is null"
                )
            )
        ]
    return documents, journaux


def charger_institutions_par_sigle(engine) -> dict:
    """sigle -> id, pour un environnement donné (source ou cible) — même
    requête des deux côtés, réutilisée pour construire le remap institution_id."""
    from sqlalchemy import text

    with engine.connect() as cnx:
        return {
            sigle: str(id_)
            for id_, sigle in cnx.execute(text("select id, sigle from institutions"))
        }


def charger_etat_cible(engine) -> EtatCible:
    """Photographie la cible (production) : ids, checksums et clés d'unicité."""
    from sqlalchemy import text

    with engine.connect() as cnx:

        def colonne(sql: str) -> frozenset:
            return frozenset(v for (v,) in cnx.execute(text(sql)) if v is not None)

        journaux = {
            (str(d), n): str(i)
            for i, d, n in cnx.execute(
                text(
                    "select id, publication_date, number from official_journals "
                    "where deleted_at is null and number is not null"
                )
            )
        }
        return EtatCible(
            ids_documents=colonne("select id::text from legal_documents"),
            checksums_sources=colonne(
                "select m.checksum_sha256 from media_files m "
                "join legal_documents d on d.id = m.document_id "
                "where m.file_category = 'SOURCE_PDF' and d.deleted_at is null"
            ),
            # slug : seule clé d'unicité portée par un index TOTAL
            # (`legal_documents_slug_unique`, sans clause WHERE) — un slug porté
            # par un document soft-deleted dans la cible bloque quand même
            # l'INSERT. On lit donc TOUS les slugs, comme ids_documents ; les
            # trois autres clés restent filtrées, leurs index étant partiels
            # (`WHERE … deleted_at IS NULL`).
            slugs=colonne("select slug from legal_documents"),
            document_keys=colonne(
                "select document_key from legal_documents where deleted_at is null"
            ),
            stock_codes=colonne(
                "select stock_code from legal_documents where deleted_at is null"
            ),
            references_nor=colonne(
                "select reference_nor from legal_documents where deleted_at is null"
            ),
            ids_journaux=colonne("select id::text from official_journals"),
            journaux_par_date_numero=journaux,
            institutions_par_sigle=charger_institutions_par_sigle(engine),
        )


# ---------------------------------------------------------------------------
# Cible en ÉCRITURE : uniquement depuis le shell, jamais depuis un fichier
# ---------------------------------------------------------------------------


def charger_cible_ecriture():
    """Construit la cible d'écriture depuis PROD_RW_DB_*, avec les mêmes refus
    que le profil de lecture : variables complètes et port distinct du dev."""
    from sqlalchemy import create_engine

    host = os.getenv("PROD_RW_DB_HOST", "").strip()
    port = os.getenv("PROD_RW_DB_PORT", "").strip()
    database = os.getenv("PROD_RW_DB_DATABASE", "").strip()
    username = os.getenv("PROD_RW_DB_USERNAME", "").strip()
    password = os.getenv("PROD_RW_DB_PASSWORD") or ""

    manquantes = [
        nom
        for nom, valeur in (
            ("PROD_RW_DB_HOST", host),
            ("PROD_RW_DB_PORT", port),
            ("PROD_RW_DB_DATABASE", database),
            ("PROD_RW_DB_USERNAME", username),
            ("PROD_RW_DB_PASSWORD", password),
        )
        if not valeur
    ]
    if manquantes:
        raise ConfigurationProdManquante(
            "Écriture refusée — variables absentes : " + ", ".join(manquantes) + ". "
            "Elles s'exportent à la main dans le shell pour l'opération autorisée, "
            "et ne doivent JAMAIS être écrites dans un fichier."
        )

    dev_port = os.getenv("DB_PORT", "5433").strip()
    if port == dev_port:
        raise CibleProdAmbigue(
            f"PROD_RW_DB_PORT ({port}) est le port de la base de développement : refus."
        )

    return create_engine(
        f"postgresql://{username}:{password}@{host}:{port}/{database}",
        echo=False,
        connect_args={"connect_timeout": 10},
    )


def creer_client_minio_source():
    """Client MinIO de DÉVELOPPEMENT (lecture des objets à copier)."""
    from minio import Minio

    host = os.getenv("MINIO_HOST", "127.0.0.1").strip()
    port = os.getenv("MINIO_PORT", "9000").strip()
    if port != "9000":
        raise CibleProdAmbigue(
            f"La source MinIO du push doit être le dev (port 9000), pas {port}."
        )
    return Minio(
        f"{host}:{port}",
        access_key=(os.getenv("MINIO_ACCESS_KEY") or "").strip(),
        secret_key=os.getenv("MINIO_SECRET_KEY") or "",
        secure=False,
    )


def creer_client_minio_ecriture():
    """Client MinIO d'écriture vers la production, depuis PROD_RW_MINIO_* (shell)."""
    from minio import Minio

    endpoint = os.getenv("PROD_RW_MINIO_ENDPOINT", "").strip()
    access_key = (os.getenv("PROD_RW_MINIO_ACCESS_KEY") or "").strip()
    secret_key = os.getenv("PROD_RW_MINIO_SECRET_KEY") or ""

    manquantes = [
        nom
        for nom, valeur in (
            ("PROD_RW_MINIO_ENDPOINT", endpoint),
            ("PROD_RW_MINIO_ACCESS_KEY", access_key),
            ("PROD_RW_MINIO_SECRET_KEY", secret_key),
        )
        if not valeur
    ]
    if manquantes:
        raise ConfigurationProdManquante(
            "Écriture MinIO refusée — variables absentes : " + ", ".join(manquantes)
        )
    if endpoint.endswith(":9000"):
        raise CibleProdAmbigue(
            f"PROD_RW_MINIO_ENDPOINT ({endpoint}) est le port du MinIO de "
            "développement : refus."
        )
    return Minio(
        endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=os.getenv("PROD_RW_MINIO_SECURE", "false").strip().lower() == "true",
    )


# ---------------------------------------------------------------------------
# Exécution
# ---------------------------------------------------------------------------


def _colonnes_copiables(cnx_source, cnx_cible, table: str) -> list:
    """Colonnes communes aux deux schémas, hors exclusions — dérivées du réel
    plutôt que des modèles SQLAlchemy, connus pour dériver."""

    def colonnes(cnx):
        with cnx.cursor() as cur:
            cur.execute(
                "select column_name from information_schema.columns "
                "where table_schema = 'public' and table_name = %s "
                "order by ordinal_position",
                (table,),
            )
            return [c for (c,) in cur.fetchall()]

    source = colonnes(cnx_source)
    cible = set(colonnes(cnx_cible))
    return [c for c in source if c in cible and c not in COLONNES_EXCLUES]


def _expression_selection(table: str, colonnes: list, remap_journaux: dict,
                          remap_institutions: dict | None = None) -> str:
    """SELECT de copie : identique à la source, à quelques réécritures près."""
    remap_institutions = remap_institutions or {}
    exprs = []
    for col in colonnes:
        if table == "legal_documents" and col == "curation_status":
            # Staging uniquement : la publication se décide en production, via
            # l'API Laravel, jamais par héritage de l'état du développement.
            exprs.append("'draft' as curation_status")
        elif table == "legal_documents" and col == "official_journal_id" and remap_journaux:
            cas = " ".join(
                f"when '{src}'::uuid then '{dst}'::uuid"
                for src, dst in remap_journaux.items()
            )
            exprs.append(
                f"(case official_journal_id {cas} else official_journal_id end) "
                "as official_journal_id"
            )
        elif table == "legal_documents" and col == "institution_id" and remap_institutions:
            # Même référentiel des deux côtés (AN, CC, CS, GOUV, JO, PR, SEN),
            # mais des UUID générés indépendamment à chaque seed dev/prod —
            # constaté le 06/08/2026 (FK institution_id systématiquement en
            # échec sur tout acte de JO). cf. docstring de construire_plan.
            cas = " ".join(
                f"when '{src}'::uuid then '{dst}'::uuid"
                for src, dst in remap_institutions.items()
            )
            exprs.append(
                f"(case institution_id {cas} else institution_id end) as institution_id"
            )
        else:
            exprs.append(col)
    return ", ".join(exprs)


def copier_table(cnx_source, cnx_cible, table: str, where: str, params: dict,
                 remap_journaux: dict, remap_institutions: dict | None = None) -> int:
    """Copie les lignes d'une table via COPY TO/FROM (sans perte de types).

    Le trigger BEFORE INSERT de la cible (search_tsv) s'applique : COPY déclenche
    les triggers de lignes.
    """
    colonnes = _colonnes_copiables(cnx_source, cnx_cible, table)
    selection = _expression_selection(table, colonnes, remap_journaux, remap_institutions)

    tampon = io.StringIO()
    with cnx_source.cursor() as cur:
        requete = cur.mogrify(
            f"select {selection} from {table} where {where}", params
        ).decode()
        cur.copy_expert(f"copy ({requete}) to stdout", tampon)
    nb = tampon.getvalue().count("\n")
    if nb == 0:
        return 0
    tampon.seek(0)
    with cnx_cible.cursor() as cur:
        cur.copy_expert(
            f"copy {table} ({', '.join(colonnes)}) from stdin", tampon
        )
    return nb


def copier_objets_minio(client_source, client_cible, bucket: str, objets: list,
                        dry_run: bool) -> list:
    """Copie les objets MinIO d'un document. Vérifie le SHA-256 quand il est connu.

    Renvoie la liste [(object_key, action)] — actions : 'copié', 'déjà présent',
    ou lève en cas d'incohérence de checksum (la provenance prime sur la vitesse).
    """
    from minio.error import S3Error

    resultats = []
    for object_key, checksum_attendu in objets:
        try:
            client_cible.stat_object(bucket, object_key)
            resultats.append((object_key, "déjà présent"))
            continue
        except S3Error as exc:
            if exc.code not in ("NoSuchKey", "NoSuchObject"):
                raise
        if dry_run:
            resultats.append((object_key, "à téléverser"))
            continue

        reponse = client_source.get_object(bucket, object_key)
        try:
            donnees = reponse.read()
        finally:
            reponse.close()
            reponse.release_conn()

        if checksum_attendu:
            constate = hashlib.sha256(donnees).hexdigest()
            if constate != checksum_attendu:
                raise RuntimeError(
                    f"Checksum inattendu pour {object_key} : la source locale ne "
                    f"correspond plus à sa provenance ({constate[:12]}… ≠ "
                    f"{checksum_attendu[:12]}…). Copie interrompue."
                )
        client_cible.put_object(
            bucket, object_key, io.BytesIO(donnees), length=len(donnees)
        )
        resultats.append((object_key, "copié"))
    return resultats


def _compter_par_document_dry_run(engine_source, ids: list) -> dict:
    """Équivalent GROUPÉ des comptages de `TABLES_PAR_DOCUMENT`, pour le rapport
    dry-run uniquement : une poignée de requêtes sur l'ensemble des documents au
    lieu d'une par document. Constaté à l'usage : la version « un document =
    une requête par table » (7 tables × N documents) prend des dizaines de
    minutes sans aucun affichage de progression dès que N dépasse quelques
    milliers — inutilisable en pratique sur un corpus de cette taille. Le mode
    écriture, lui, reste document-par-document (voir `executer_push`) : c'est
    voulu, pour la reprise après interruption, pas un oubli.

    Renvoie {document_id: {table: compte}}, avec les mêmes clés/valeurs que la
    boucle d'origine produirait pour chaque document.
    """
    from sqlalchemy import text

    if not ids:
        return {}

    compte: dict = {doc_id: {"legal_documents": 1} for doc_id in ids}
    with engine_source.connect() as cnx:
        for table in ("media_files", "extraction_runs", "structure_nodes", "articles"):
            for doc_id, n in cnx.execute(
                text(
                    f"select document_id, count(*) from {table} "
                    "where document_id = any(CAST(:ids AS uuid[])) group by document_id"
                ),
                {"ids": ids},
            ):
                compte[str(doc_id)][table] = n

        for doc_id, n in cnx.execute(
            text(
                "select a.document_id, count(av.id) from articles a "
                "join article_versions av on av.article_id = a.id "
                "where a.document_id = any(CAST(:ids AS uuid[])) group by a.document_id"
            ),
            {"ids": ids},
        ):
            compte[str(doc_id)]["article_versions"] = n

        # curation_flags : même clause OR que l'original (document_id direct OU
        # via un article du document) — DISTINCT sur l'id du flag pour ne pas
        # compter deux fois une ligne qui satisferait les deux branches.
        for doc_id, n in cnx.execute(
            text(
                "select doc_id, count(distinct flag_id) from ("
                "  select document_id as doc_id, id as flag_id from curation_flags"
                "  where document_id = any(CAST(:ids AS uuid[]))"
                "  union all"
                "  select a.document_id as doc_id, cf.id as flag_id"
                "  from curation_flags cf join articles a on a.id = cf.article_id"
                "  where a.document_id = any(CAST(:ids AS uuid[]))"
                ") sous_requete group by doc_id"
            ),
            {"ids": ids},
        ):
            compte[str(doc_id)]["curation_flags"] = n

    for doc_id in ids:
        for table, _ in TABLES_PAR_DOCUMENT:
            compte[doc_id].setdefault(table, 0)
    return compte


def executer_push(engine_source, engine_cible, plan: PlanPush,
                  client_minio_source=None, client_minio_cible=None,
                  bucket: str = "mibeko-documents", dry_run: bool = True,
                  limite: int | None = None, rapport_chemin: str | None = None):
    """Déroule le plan : journaux d'abord, puis un document = une transaction.

    En dry-run, aucune écriture d'aucune sorte ; le rapport décrit ce qui serait
    fait. En exécution, un document est poussé objets MinIO d'abord (idempotents,
    versionnés côté cible), lignes ensuite, commit par document : une interruption
    laisse la cible cohérente et la relance reprend où on en était.
    """
    from sqlalchemy import text

    a_pousser = plan.a_pousser[:limite] if limite else plan.a_pousser
    rapport = {
        "horodatage": datetime.now().isoformat(timespec="seconds"),
        "dry_run": dry_run,
        "journaux_a_creer": len(plan.journaux_a_creer),
        "documents": [],
        "ecartes": [
            {"document": d.libelle(), "motif": motif} for d, motif in plan.ecartes
        ],
    }

    cnx_src = engine_source.raw_connection()
    cnx_cbl = engine_cible.raw_connection()
    try:
        # -- Journaux officiels requis (une seule transaction) ------------------
        if plan.journaux_a_creer and not dry_run:
            ids = tuple(j.id for j in plan.journaux_a_creer)
            copier_table(
                cnx_src, cnx_cbl, "official_journals",
                "id = any(%(ids)s::uuid[])", {"ids": list(ids)}, {},
            )
            cnx_cbl.commit()

        # -- Documents ------------------------------------------------------
        # Dry-run : comptages groupés (une poignée de requêtes pour tout
        # a_pousser) — voir _compter_par_document_dry_run. Écriture : un
        # document = une transaction, inchangé, pour la reprise après
        # interruption (chaque commit rend la cible cohérente).
        objets_par_doc: dict = {}
        comptes_par_doc: dict = {}
        if dry_run and a_pousser:
            ids = [doc.id for doc in a_pousser]
            comptes_par_doc = _compter_par_document_dry_run(engine_source, ids)
            with engine_source.connect() as cnx:
                for doc_id, k, c in cnx.execute(
                    text(
                        "select document_id, object_key, checksum_sha256 "
                        "from media_files where document_id = any(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": ids},
                ):
                    objets_par_doc.setdefault(str(doc_id), []).append((k, c))

        for doc in a_pousser:
            entree = {"document": doc.libelle(), "id": doc.id, "tables": {},
                      "objets": []}
            if dry_run:
                objets = objets_par_doc.get(doc.id, [])
            else:
                with engine_source.connect() as cnx:
                    objets = [
                        (k, c)
                        for k, c in cnx.execute(
                            text(
                                "select object_key, checksum_sha256 from media_files "
                                "where document_id = :doc"
                            ),
                            {"doc": doc.id},
                        )
                    ]
            if client_minio_source is not None and client_minio_cible is not None:
                entree["objets"] = copier_objets_minio(
                    client_minio_source, client_minio_cible, bucket, objets, dry_run
                )
            elif objets:
                entree["objets"] = [(k, "à téléverser") for k, _ in objets]

            if dry_run:
                entree["tables"] = comptes_par_doc.get(doc.id, {})
            else:
                for table, where in TABLES_PAR_DOCUMENT:
                    entree["tables"][table] = copier_table(
                        cnx_src, cnx_cbl, table, where, {"doc": doc.id},
                        plan.remap_journaux, plan.remap_institutions,
                    )
                cnx_cbl.commit()
                logger.info("Poussé : %s (%s)", doc.libelle(), doc.id)
            rapport["documents"].append(entree)
    except Exception:
        cnx_cbl.rollback()
        raise
    finally:
        cnx_src.close()
        cnx_cbl.close()

    if rapport_chemin:
        with open(rapport_chemin, "w", encoding="utf-8") as f:
            json.dump(rapport, f, ensure_ascii=False, indent=2)
    return rapport
