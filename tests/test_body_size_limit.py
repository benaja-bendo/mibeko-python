"""Tests du plafond ASGI de taille de corps (FIX-1).

Le middleware `BodySizeLimitMiddleware` doit couper AVANT que le corps ne soit
lu/spoolé et avant l'auth. Deux voies de défense :

- `Content-Length` honnête et trop grand → 413 immédiat, sans lire le corps ;
- `Content-Length` absent ou MENTEUR (chunked / valeur sous-évaluée) → comptage
  réel des octets et coupure nette à la limite (413).

On teste la 1re voie via Starlette TestClient (Content-Length honnête calculé par
httpx) et la 2de voie en pilotant directement le protocole ASGI avec un `receive`
qui streame plus d'octets que déclaré.
"""

import asyncio
import os
import sys

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.upload_utils import BodySizeLimitMiddleware  # noqa: E402

LIMIT = 1024  # 1 Ko : plafond de test volontairement bas.


async def _echo_len(request):
    body = await request.body()
    return PlainTextResponse(f"received={len(body)}")


def _build_client(limit=LIMIT):
    app = Starlette(routes=[Route("/echo", _echo_len, methods=["POST"])])
    app.add_middleware(BodySizeLimitMiddleware, max_body_bytes=limit)
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1) Content-Length honnête
# ---------------------------------------------------------------------------

def test_corps_sous_la_limite_passe():
    client = _build_client()
    resp = client.post("/echo", content=b"x" * 500)
    assert resp.status_code == 200
    assert resp.text == "received=500"


def test_content_length_honnete_trop_grand_rejete_413():
    client = _build_client()
    resp = client.post("/echo", content=b"x" * (LIMIT + 1))
    assert resp.status_code == 413
    assert "trop volumineux" in resp.json()["message"]


def test_corps_exactement_a_la_limite_passe():
    client = _build_client()
    resp = client.post("/echo", content=b"x" * LIMIT)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 2) Content-Length menteur / chunked : comptage réel des octets
# ---------------------------------------------------------------------------

def _drive_asgi(app, scope, body_chunks):
    """Pilote une app ASGI avec un corps envoyé en plusieurs `http.request`.

    Renvoie (status, body_bytes) capturés depuis les messages de réponse.
    """

    sent = {"status": None, "body": b""}
    chunks = list(body_chunks)

    async def receive():
        if chunks:
            chunk = chunks.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}
        # Plus de corps : on reste silencieux (le middleware a déjà coupé) —
        # un disconnect évite tout blocage si l'app tente encore de lire.
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            sent["status"] = message["status"]
        elif message["type"] == "http.response.body":
            sent["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return sent["status"], sent["body"]


def _http_scope(headers):
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/echo",
        "raw_path": b"/echo",
        "query_string": b"",
        "headers": headers,
        "scheme": "http",
        "server": ("test", 80),
        "client": ("test", 12345),
    }


def test_content_length_menteur_coupe_au_comptage():
    # Scope déclare Content-Length=10 mais on streame 4 Ko : le comptage réel
    # doit dépasser la limite et renvoyer 413, même si l'en-tête ment.
    app = Starlette(routes=[Route("/echo", _echo_len, methods=["POST"])])
    wrapped = BodySizeLimitMiddleware(app, max_body_bytes=LIMIT)
    scope = _http_scope([(b"content-length", b"10")])
    body_chunks = [b"x" * 512] * 8  # 4 Ko réels
    status, _ = _drive_asgi(wrapped, scope, body_chunks)
    assert status == 413


def test_sans_content_length_coupe_au_comptage():
    # Transfert chunked : pas de Content-Length du tout. Le comptage protège.
    app = Starlette(routes=[Route("/echo", _echo_len, methods=["POST"])])
    wrapped = BodySizeLimitMiddleware(app, max_body_bytes=LIMIT)
    scope = _http_scope([(b"transfer-encoding", b"chunked")])
    body_chunks = [b"y" * 512] * 6  # 3 Ko réels, aucun Content-Length
    status, _ = _drive_asgi(wrapped, scope, body_chunks)
    assert status == 413


def test_sans_content_length_petit_corps_passe():
    # Chunked mais sous la limite : la requête aboutit normalement.
    app = Starlette(routes=[Route("/echo", _echo_len, methods=["POST"])])
    wrapped = BodySizeLimitMiddleware(app, max_body_bytes=LIMIT)
    scope = _http_scope([(b"transfer-encoding", b"chunked")])
    body_chunks = [b"z" * 200, b"z" * 200]  # 400 octets
    status, body = _drive_asgi(wrapped, scope, body_chunks)
    assert status == 200
    assert body == b"received=400"


def test_requete_non_http_est_transmise_telle_quelle():
    # Un scope websocket/lifespan ne doit pas être plafonné : passthrough.
    called = {"ok": False}

    async def downstream(scope, receive, send):
        called["ok"] = True

    wrapped = BodySizeLimitMiddleware(downstream, max_body_bytes=LIMIT)

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(message):
        pass

    asyncio.run(wrapped({"type": "lifespan"}, receive, send))
    assert called["ok"] is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
