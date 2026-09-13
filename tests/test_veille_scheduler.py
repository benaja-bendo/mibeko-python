"""Calcul du prochain passage quotidien (mibeko-python#21) — fonction pure,
aucune horloge réelle ni base de données requise.
"""

import datetime

import pytest

from src.veille.scheduler import next_run_at, seconds_until

UTC = datetime.timezone.utc


def test_next_run_at_avant_heure_cible_reste_aujourdhui():
    now = datetime.datetime(2026, 9, 13, 1, 0, tzinfo=UTC)
    assert next_run_at(now, hour_utc=2) == datetime.datetime(2026, 9, 13, 2, 0, tzinfo=UTC)


def test_next_run_at_apres_heure_cible_bascule_demain():
    now = datetime.datetime(2026, 9, 13, 3, 0, tzinfo=UTC)
    assert next_run_at(now, hour_utc=2) == datetime.datetime(2026, 9, 14, 2, 0, tzinfo=UTC)


def test_next_run_at_pile_a_lheure_bascule_demain():
    # Un passage ne doit jamais se redéclencher immédiatement s'il tombe pile
    # à la seconde du calcul.
    now = datetime.datetime(2026, 9, 13, 2, 0, tzinfo=UTC)
    assert next_run_at(now, hour_utc=2) == datetime.datetime(2026, 9, 14, 2, 0, tzinfo=UTC)


def test_next_run_at_exige_un_datetime_timezone_aware():
    with pytest.raises(ValueError):
        next_run_at(datetime.datetime(2026, 9, 13, 1, 0), hour_utc=2)


def test_seconds_until_ne_devient_jamais_negatif():
    now = datetime.datetime(2026, 9, 13, 2, 0, 5, tzinfo=UTC)
    target = datetime.datetime(2026, 9, 13, 2, 0, 0, tzinfo=UTC)
    assert seconds_until(now, target) == 0.0


def test_seconds_until_calcule_lecart():
    now = datetime.datetime(2026, 9, 13, 1, 0, 0, tzinfo=UTC)
    target = datetime.datetime(2026, 9, 13, 2, 0, 0, tzinfo=UTC)
    assert seconds_until(now, target) == 3600.0
