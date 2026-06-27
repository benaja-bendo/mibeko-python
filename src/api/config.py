"""Configuration d'exposition du service Python (brique interne).

Le service Python est une brique d'infrastructure : il est consommé par le
backend Laravel et le front éditeur via les routes ``/api/v1/*`` (token Sanctum).
Il ne doit pas se présenter comme un site web : pas de console publique ouverte,
pas de documentation OpenAPI indexable en production.
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _is_truthy(raw: str) -> bool:
    return raw.strip().lower() in ("1", "true", "yes", "on")


APP_ENV = os.getenv("APP_ENV", "production").strip().lower()
IS_PRODUCTION = APP_ENV in ("production", "prod")

SERVICE_VERSION = os.getenv("SERVICE_VERSION", "1.0.0")

# Documentation OpenAPI (Swagger / ReDoc) : activée hors production par défaut.
# Elle décrit des opérations d'écriture internes : on ne l'expose pas au public
# en production sauf demande explicite (EXPOSE_API_DOCS=true).
EXPOSE_API_DOCS = _is_truthy(
    os.getenv("EXPOSE_API_DOCS", "false" if IS_PRODUCTION else "true")
)

# Console d'ingestion HTMX héritée : désactivée par défaut. Le véritable outil
# d'ingestion vit désormais dans le front éditeur (app.mibeko.fr). On ne sert
# plus cette console publiquement ; à n'activer que ponctuellement en local.
INGESTION_CONSOLE_ENABLED = _is_truthy(os.getenv("INGESTION_CONSOLE_ENABLED", "false"))
