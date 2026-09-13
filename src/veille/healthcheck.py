"""Ping « dead man's switch » vers un service de healthcheck externe.

Même mécanisme que `vps_infra/roles/corpus_backup/templates/mibeko-backup-corpus.sh.j2`
(ping `/start` puis succès ou `/fail`) : si aucun ping de succès n'arrive dans
la fenêtre attendue, c'est le service de healthcheck qui alerte — un cron muet
qui échoue en silence ne doit pas dépendre de sa propre capacité à le signaler.

Désactivé par défaut (`VEILLE_HEALTHCHECK_URL` vide) : ne jamais faire échouer
la veille elle-même à cause d'un ping raté.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("veille.healthcheck")


def _url() -> str:
    return os.getenv("VEILLE_HEALTHCHECK_URL", "").strip()


def ping(event: str = "") -> None:
    """Ping `<url><event>` (event = "", "/start" ou "/fail"). No-op si
    non configuré. Ne lève jamais : un ping raté n'est pas un échec de veille.
    """
    base = _url()
    if not base:
        return
    try:
        httpx.get(f"{base}{event}", timeout=10.0)
    except httpx.HTTPError as exc:
        logger.warning("ping healthcheck %s échoué : %s", event or "(succès)", exc)
