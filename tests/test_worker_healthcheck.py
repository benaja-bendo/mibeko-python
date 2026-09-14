"""Ping healthcheck du worker (mibeko-python#23) : no-op par défaut, ne lève
jamais — même contrat que tests/test_veille_healthcheck.py, instance séparée.
"""

import httpx

from src.worker import healthcheck


def test_ping_noop_si_url_non_configuree(monkeypatch):
    monkeypatch.delenv("WORKER_HEALTHCHECK_URL", raising=False)
    called = []
    monkeypatch.setattr(httpx, "get", lambda *a, **k: called.append((a, k)))
    healthcheck.ping("/start")
    assert called == []


def test_ping_appelle_lurl_configuree_avec_le_suffixe(monkeypatch):
    monkeypatch.setenv("WORKER_HEALTHCHECK_URL", "https://hc.example/ping/worker123")
    appels = []

    def fake_get(url, timeout=None):
        appels.append(url)
        return httpx.Response(200)

    monkeypatch.setattr(httpx, "get", fake_get)
    healthcheck.ping("/start")
    healthcheck.ping("")
    healthcheck.ping("/fail")
    assert appels == [
        "https://hc.example/ping/worker123/start",
        "https://hc.example/ping/worker123",
        "https://hc.example/ping/worker123/fail",
    ]


def test_ping_avale_les_erreurs_reseau(monkeypatch):
    monkeypatch.setenv("WORKER_HEALTHCHECK_URL", "https://hc.example/ping/worker123")

    def fake_get(url, timeout=None):
        raise httpx.ConnectError("down", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    healthcheck.ping("/fail")  # ne doit lever aucune exception
