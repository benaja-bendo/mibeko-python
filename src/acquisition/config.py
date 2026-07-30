"""Configuration de l'acquisition : chemins data/ et identité du bot."""

import os
from pathlib import Path

# Racine du dépôt mibeko-python (…/mibeko-python).
REPO_ROOT = Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    """Dossier data/ (mibeko-python/data, surchargeable via MIBEKO_DATA_DIR)."""
    return Path(os.getenv("MIBEKO_DATA_DIR", REPO_ROOT / "data")).resolve()


def sources_dir() -> Path:
    return data_dir() / "sources"


def manifests_dir() -> Path:
    return data_dir() / "manifests"


def corpus_file() -> Path:
    return data_dir() / "corpus" / "corpus-v1.yaml"


def bot_user_agent() -> str:
    contact = os.getenv("MIBEKO_BOT_CONTACT", "benaja.bendo02@gmail.com")
    return f"MibekoBot/1.0 (+https://mibeko.fr; contact: {contact})"
