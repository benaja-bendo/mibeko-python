"""Diagnostic de la base de PRODUCTION en lecture seule, à travers un tunnel SSH.

Doctrine
--------
Ce module sert **uniquement** au diagnostic. Il ne doit jamais être importé par
l'API ni par le pipeline d'ingestion, et aucune écriture en production ne doit en
partir : la lecture seule est imposée côté serveur par le rôle Postgres dédié
(``GRANT SELECT`` + ``default_transaction_read_only = on``), ce module ne fait que
la vérifier et refuser de travailler si elle n'est pas prouvée.

Autonomie volontaire
--------------------
Le module n'importe ni ``src.db.database`` (qui crée l'engine de développement dès
l'import) ni ``src.services.minio_service`` (dont le singleton appelle
``make_bucket`` dès l'import : avec un tunnel ouvert, un simple import toucherait
le stockage de production). Les requêtes passent par du SQL brut plutôt que par les
modèles ORM, pour ne dépendre d'aucune de ces chaînes d'import.

Rien n'est instancié au niveau module : l'engine et le client MinIO sont construits
dans des fonctions, afin qu'importer ce fichier ne provoque aucune connexion.

Prérequis : ``ssh -N -L 5434:127.0.0.1:5432 ubuntu@<IP_VPS>``.
Voir ``docs/infra/acces-prod-diagnostic.md``.
"""

import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv

logger = logging.getLogger("mibeko.db.prod_readonly")

# Même chargement que src/db/database.py : chemin absolu, indépendant du cwd.
# load_dotenv n'écrase pas les variables déjà exportées dans le shell, qui priment.
_ENV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"
)
load_dotenv(_ENV_PATH)

# Ports de diagnostic, volontairement distincts du développement (5433 / 9000) :
# une production indiscernable du développement est le scénario d'accident à éviter.
PORT_PROD_PAR_DEFAUT = "5434"
ENDPOINT_MINIO_PAR_DEFAUT = "127.0.0.1:9100"

# SQLSTATE : seuls ces deux codes prouvent quelque chose sur l'impossibilité d'écrire.
SQLSTATE_LECTURE_SEULE = "25006"  # read_only_sql_transaction
SQLSTATE_PRIVILEGE_INSUFFISANT = "42501"  # insufficient_privilege

# Sonde d'écriture : aucune ligne ne peut correspondre (« id » est la clé primaire),
# donc même si elle était acceptée, elle ne modifierait rien. Elle est toujours annulée.
_SONDE_ECRITURE = "update legal_documents set updated_at = updated_at where id is null"


class ConfigurationProdManquante(RuntimeError):
    """Une variable obligatoire du profil de diagnostic n'est pas renseignée."""


class CibleProdAmbigue(RuntimeError):
    """La cible « production » désigne en réalité la base de développement."""


class LectureSeuleNonProuvee(RuntimeError):
    """La session n'a pas pu être prouvée incapable d'écrire."""


@dataclass(frozen=True)
class CibleProd:
    """Cible de diagnostic, sans le mot de passe dans sa représentation."""

    host: str
    port: str
    database: str
    username: str
    password: str

    def dsn(self) -> str:
        """DSN SQLAlchemy. À ne jamais journaliser : il contient le mot de passe."""
        return (
            f"postgresql://{self.username}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )

    def resume(self) -> str:
        """Description affichable de la cible, sans le mot de passe."""
        return f"{self.username}@{self.host}:{self.port}/{self.database}"

    def __repr__(self) -> str:  # pragma: no cover - protège des fuites en log
        return f"CibleProd({self.resume()})"


def charger_cible() -> CibleProd:
    """Construit la cible de diagnostic depuis les variables ``PROD_RO_DB_*``.

    Lève :
        ConfigurationProdManquante: si une variable obligatoire est absente.
        CibleProdAmbigue: si la cible désigne en fait la base de développement.
    """
    host = os.getenv("PROD_RO_DB_HOST", "127.0.0.1").strip()
    port = os.getenv("PROD_RO_DB_PORT", PORT_PROD_PAR_DEFAUT).strip()
    database = (os.getenv("PROD_RO_DB_DATABASE") or "").strip()
    username = (os.getenv("PROD_RO_DB_USERNAME") or "").strip()
    password = os.getenv("PROD_RO_DB_PASSWORD") or ""

    manquantes = [
        nom
        for nom, valeur in (
            ("PROD_RO_DB_HOST", host),
            ("PROD_RO_DB_PORT", port),
            ("PROD_RO_DB_DATABASE", database),
            ("PROD_RO_DB_USERNAME", username),
            ("PROD_RO_DB_PASSWORD", password),
        )
        if not valeur
    ]
    if manquantes:
        raise ConfigurationProdManquante(
            "Variables obligatoires absentes : "
            + ", ".join(manquantes)
            + ". Ouvrir le tunnel SSH (ssh -N -L 5434:127.0.0.1:5432 ubuntu@<IP_VPS>) "
            "puis renseigner le profil de diagnostic (cf. .env.example). "
            "Aucune variable d'écriture en production ne doit être stockée dans un fichier."
        )

    cible = CibleProd(
        host=host, port=port, database=database, username=username, password=password
    )
    _refuser_si_cible_de_developpement(cible)
    return cible


def _refuser_si_cible_de_developpement(cible: CibleProd) -> None:
    """Refuse une cible « prod » qui pointe en réalité le développement.

    Le service ne dispose d'aucun moyen de savoir à quelle base il parle : le DSN
    n'est qu'un host:port. Ce garde-fou est donc la seule protection contre le
    scénario classique — un tunnel ouvert sur le port du développement, ou une
    variable oubliée dans le shell — qui rendrait les deux environnements
    indiscernables. La config de développement est lue via os.getenv, sans importer
    src.db.database (qui créerait son engine au passage).

    Le discriminant est le PORT, et lui seul : à travers un tunnel SSH, dev et prod
    partagent légitimement l'hôte (127.0.0.1) et le nom de base (mibeko-db des deux
    côtés) — comparer l'hôte ou le nom refuserait précisément la configuration que
    documente le runbook. Toute recopie accidentelle des variables DB_* vers
    PROD_RO_DB_* emporte le port du dev avec elle, donc ce seul contrôle couvre
    aussi ce scénario.
    """
    dev_port = os.getenv("DB_PORT", "5433").strip()

    if cible.port == dev_port:
        raise CibleProdAmbigue(
            f"PROD_RO_DB_PORT ({cible.port}) est le port de la base de développement "
            f"(DB_PORT={dev_port}) : refus. Utiliser un port de tunnel distinct, "
            f"{PORT_PROD_PAR_DEFAUT} par convention."
        )


def creer_engine(cible: CibleProd | None = None):
    """Crée l'engine de diagnostic. Appelé à la demande, jamais à l'import.

    ``default_transaction_read_only`` est posé côté session en ceinture-et-bretelles :
    la garantie qui compte reste celle du rôle Postgres, côté serveur.
    """
    from sqlalchemy import create_engine

    cible = cible or charger_cible()

    return create_engine(
        cible.dsn(),
        echo=False,
        pool_pre_ping=True,
        connect_args={"options": "-c default_transaction_read_only=on"},
    )


def assert_read_only(engine) -> str:
    """Prouve que la session ne peut pas écrire, et renvoie le motif du refus.

    Émet une sonde d'écriture inoffensive dans une transaction toujours annulée. Le
    point délicat est de ne pas conclure « c'est sûr » sur un échec quelconque : une
    table absente ou une faute de syntaxe échoueraient aussi. Seuls les SQLSTATE
    25006 (transaction en lecture seule) et 42501 (privilège insuffisant) prouvent
    quelque chose ; tout autre code rend la preuve non concluante.

    Renvoie :
        Le SQLSTATE ayant fait la preuve.

    Lève :
        LectureSeuleNonProuvee: si l'écriture réussit, ou si elle échoue pour un
            motif qui ne prouve pas l'impossibilité d'écrire.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    connexion = engine.connect()
    transaction = connexion.begin()
    try:
        try:
            connexion.execute(text(_SONDE_ECRITURE))
        except DBAPIError as exc:
            sqlstate = getattr(getattr(exc, "orig", None), "pgcode", None)

            if sqlstate == SQLSTATE_LECTURE_SEULE:
                return SQLSTATE_LECTURE_SEULE
            if sqlstate == SQLSTATE_PRIVILEGE_INSUFFISANT:
                return SQLSTATE_PRIVILEGE_INSUFFISANT

            raise LectureSeuleNonProuvee(
                "La sonde d'écriture a échoué, mais pour un motif qui ne prouve rien "
                f"(SQLSTATE {sqlstate or 'inconnu'}) : {exc}. Un échec pour une autre "
                "cause (table absente, schéma inattendu) ne garantit pas que la session "
                "est incapable d'écrire. Session interrompue par précaution."
            ) from exc
        else:
            raise LectureSeuleNonProuvee(
                "La sonde d'écriture a été ACCEPTÉE : cette connexion peut écrire en "
                "production. Cause probable : le rôle utilisé n'est pas le rôle dédié "
                "en lecture seule. À corriger côté serveur (GRANT SELECT + "
                "ALTER ROLE ... SET default_transaction_read_only = on) avant tout "
                "diagnostic."
            )
    finally:
        # Annulation garantie sur tous les chemins, y compris en cas d'exception.
        try:
            transaction.rollback()
        finally:
            connexion.close()


def etat_des_lieux(engine) -> dict:
    """Dresse un état des lieux du corpus en production. SELECT uniquement.

    Les tables porteuses de SoftDeletes (Laravel) sont comptées en distinguant les
    lignes vivantes des lignes supprimées : toute lecture applicative doit filtrer
    ``deleted_at IS NULL``, et un écart entre les deux compteurs est en soi un signal.
    """
    from sqlalchemy import text

    with engine.connect() as connexion:

        def scalaire(sql: str):
            return connexion.execute(text(sql)).scalar()

        def lignes(sql: str) -> list[tuple]:
            return [tuple(ligne) for ligne in connexion.execute(text(sql))]

        extensions_presentes = {
            nom
            for (nom,) in lignes(
                "select extname from pg_extension "
                "where extname in ('ltree', 'vector', 'btree_gist', 'pg_trgm')"
            )
        }

        return {
            "version_postgres": scalaire("select current_setting('server_version')"),
            "extensions": {
                nom: nom in extensions_presentes
                for nom in ("ltree", "vector", "btree_gist", "pg_trgm")
            },
            "documents_par_statut": lignes(
                "select curation_status, "
                "count(*) filter (where deleted_at is null) as vivants, "
                "count(*) filter (where deleted_at is not null) as supprimes "
                "from legal_documents group by curation_status order by curation_status"
            ),
            "anomalies_non_resolues": lignes(
                "select severity, count(*) from curation_flags "
                "where resolved = false group by severity order by severity"
            ),
            "articles_vivants": scalaire(
                "select count(*) from articles where deleted_at is null"
            ),
            "articles_supprimes": scalaire(
                "select count(*) from articles where deleted_at is not null"
            ),
            "versions_articles": scalaire("select count(*) from article_versions"),
            "versions_sans_embedding": scalaire(
                "select count(*) from article_versions where embedding is null"
            ),
            "journaux_officiels": scalaire(
                "select count(*) from official_journals where deleted_at is null"
            ),
            "fichiers_media": scalaire("select count(*) from media_files"),
        }


def creer_client_minio_diagnostic():
    """Client MinIO de diagnostic, en lecture seule. Appelé à la demande.

    La lecture seule est garantie par la policy ``readonly`` du service account
    dédié, côté serveur : ce client n'a aucun moyen de l'imposer. Il ne doit servir
    qu'à lister et lire. Aucun appel à ``make_bucket`` n'est fait ici — contrairement
    au service MinIO du pipeline, dont le singleton crée le bucket dès l'import.

    Renvoie :
        Le couple (client, nom du bucket).
    """
    endpoint = os.getenv("PROD_RO_MINIO_ENDPOINT", ENDPOINT_MINIO_PAR_DEFAUT).strip()
    access_key = (os.getenv("PROD_RO_MINIO_ACCESS_KEY") or "").strip()
    secret_key = os.getenv("PROD_RO_MINIO_SECRET_KEY") or ""
    bucket = os.getenv("PROD_RO_MINIO_BUCKET", "mibeko-documents").strip()

    manquantes = [
        nom
        for nom, valeur in (
            ("PROD_RO_MINIO_ACCESS_KEY", access_key),
            ("PROD_RO_MINIO_SECRET_KEY", secret_key),
        )
        if not valeur
    ]
    if manquantes:
        raise ConfigurationProdManquante(
            "Variables obligatoires absentes : "
            + ", ".join(manquantes)
            + ". Ouvrir le tunnel MinIO (ssh -N -L 9100:127.0.0.1:9000 ubuntu@<IP_VPS>) "
            "et utiliser le service account en policy readonly, jamais les clés root."
        )

    if endpoint.endswith(":9000"):
        raise CibleProdAmbigue(
            f"PROD_RO_MINIO_ENDPOINT ({endpoint}) utilise le port du MinIO de "
            f"développement : refus. Utiliser {ENDPOINT_MINIO_PAR_DEFAUT} par convention."
        )

    # Import tardif, APRÈS les validations : une configuration fautive doit être
    # signalée telle quelle, sans dépendre de la présence du paquet minio.
    from minio import Minio

    client = Minio(
        endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=os.getenv("PROD_RO_MINIO_SECURE", "false").strip().lower() == "true",
    )

    return client, bucket
