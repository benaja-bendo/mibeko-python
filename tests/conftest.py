"""Utilitaires partagés à la suite de tests.

`stub_service_modules` neutralise temporairement MinIO/MinerU
(src.services.minio_service / src.services.mineru_service) pour permettre
d'importer `src.api.main` sans backend réel. Contrairement à un simple
`sys.modules[...] = fake` non restauré, ce context manager remet
`sys.modules` dans son état d'origine en sortie de bloc : sans ça, la
pollution (cache process-wide) survit aux fichiers qui l'utilisent et peut
faire récupérer le faux singleton (`mineru_service = object()`, sans
`.backend`) à un import différé lancé plus tard dans la même session pytest
(ex. `src/parsing/batch.py::_mineru_backend_label`).
"""

import os
import sys
import types
from contextlib import contextmanager

import pytest

# Cible de développement attendue. Contrairement à Laravel (dont phpunit.xml épingle
# l'hôte et le port), rien ici n'empêcherait la suite de tests de s'exécuter contre la
# base pointée par le .env — et celle-ci peut viser la PRODUCTION via un tunnel SSH.
# Plusieurs tests écrivent en base : on refuse donc de collecter quoi que ce soit
# tant que la cible n'est pas le Postgres local.
_CIBLE_DEV = {"host": "127.0.0.1", "port": "5433"}
_VARIABLE_DEROGATION = "MIBEKO_ALLOW_NON_DEV_DB"


def pytest_configure(config):
    """Interdit d'exécuter la suite ailleurs que sur la base de développement."""
    if os.getenv(_VARIABLE_DEROGATION) == "1":
        return

    # Les variables exportées dans le shell priment sur le .env (load_dotenv ne les
    # écrase pas) : on lit donc l'environnement effectif, .env inclus.
    from dotenv import dotenv_values

    fichier = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    valeurs = {**dotenv_values(fichier), **os.environ}

    host = (valeurs.get("DB_HOST") or "127.0.0.1").strip()
    port = (valeurs.get("DB_PORT") or "5433").strip()

    if (host, port) != (_CIBLE_DEV["host"], _CIBLE_DEV["port"]):
        raise pytest.UsageError(
            f"Suite de tests interrompue : la base cible est {host}:{port}, "
            f"pas la base de développement ({_CIBLE_DEV['host']}:{_CIBLE_DEV['port']}). "
            "Plusieurs tests écrivent en base — s'il s'agit de la PRODUCTION "
            "(tunnel SSH), les lancer la corromprait. Rebasculer le .env sur le "
            "développement, ou exporter le temps d'une commande : "
            "DB_HOST=127.0.0.1 DB_PORT=5433 DB_USERNAME=root DB_PASSWORD=root pytest …"
        )

_STUBBED_MODULES = [
    ("src.services.minio_service", "minio_service"),
    ("src.services.mineru_service", "mineru_service"),
]


@contextmanager
def stub_service_modules():
    originals = {name: sys.modules.get(name) for name, _ in _STUBBED_MODULES}
    for name, attr in _STUBBED_MODULES:
        fake = types.ModuleType(name)
        setattr(fake, attr, object())
        sys.modules[name] = fake
    try:
        yield
    finally:
        for name, original in originals.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
