"""Calcul du prochain déclenchement quotidien, en UTC.

Fonction pure et testable sans horloge réelle : le daemon (`main.py
veille-corpus`) ne fait que dormir jusqu'à `next_run_at(...)` puis relancer.
"""

from __future__ import annotations

import datetime


def next_run_at(now: datetime.datetime, hour_utc: int) -> datetime.datetime:
    """Prochain instant, strictement après `now`, à `hour_utc:00:00` UTC.

    `now` doit être timezone-aware (UTC). Si l'heure cible est déjà passée
    aujourd'hui, bascule au lendemain.
    """
    if now.tzinfo is None:
        raise ValueError("now doit être timezone-aware (UTC)")
    candidate = now.astimezone(datetime.timezone.utc).replace(
        hour=hour_utc, minute=0, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += datetime.timedelta(days=1)
    return candidate


def seconds_until(now: datetime.datetime, target: datetime.datetime) -> float:
    return max(0.0, (target - now).total_seconds())
