import logging
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

logger = logging.getLogger("mibeko.db")

# Charger les variables d'environnement depuis le .env
# Par défaut on charge celui du dossier courant, mais on peut spécifier le chemin
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env")
load_dotenv(env_path)

_IS_PRODUCTION = os.getenv("APP_ENV", "production").strip().lower() in ("production", "prod")


def _credential_env(name: str, dev_default: str) -> str:
    """Identifiant sensible : obligatoire en production, repli loggé en dev.

    Audit S7 : les fallbacks silencieux (root/root) masquaient une variable
    absente en production. Désormais le service refuse de démarrer plutôt que
    de se connecter avec des identifiants par défaut faibles.
    """

    value = os.getenv(name)
    if value:
        return value
    if _IS_PRODUCTION:
        raise RuntimeError(
            f"Variable d'environnement {name} obligatoire en production : "
            "aucun identifiant par défaut n'est autorisé (APP_ENV=production)."
        )
    logger.warning("%s absent du .env — repli développement « %s » utilisé.", name, dev_default)
    return dev_default


DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "5433")
DB_DATABASE = os.getenv("DB_DATABASE", "mibeko")
DB_USERNAME = _credential_env("DB_USERNAME", "root")
DB_PASSWORD = _credential_env("DB_PASSWORD", "root")

DATABASE_URL = f"postgresql://{DB_USERNAME}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_DATABASE}"

# Le DSN ne contient qu'un host:port : rien dans le code ne permet de savoir si l'on
# parle au développement ou à la production. On journalise donc systématiquement la
# cible (jamais le mot de passe), et on hausse le ton dès qu'elle n'est pas la cible
# de développement attendue — typiquement une production atteinte par tunnel SSH.
_CIBLE_DEV = ("127.0.0.1", "5433")

if (DB_HOST, DB_PORT) == _CIBLE_DEV:
    logger.info("Base cible : %s@%s:%s/%s (développement).", DB_USERNAME, DB_HOST, DB_PORT, DB_DATABASE)
else:
    logger.warning(
        "Base cible NON standard : %s@%s:%s/%s — s'il s'agit de la PRODUCTION "
        "(tunnel SSH), rappel : la ré-ingestion purge physiquement les articles et "
        "DELETE /documents/{id} supprime aussi les objets MinIO, sans retour possible.",
        DB_USERNAME,
        DB_HOST,
        DB_PORT,
        DB_DATABASE,
    )

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    """
    Fournit une session de base de données.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    """
    Crée toutes les tables dans la base de données.
    Note : Comme on se connecte à la DB Laravel existante, on évite de recréer les tables
    qui sont gérées par les migrations Laravel.
    """
    import src.db.models  # Assurez-vous que les modèles sont importés
    # On ne fait pas Base.metadata.create_all(bind=engine) pour ne pas interférer avec Laravel
    pass
