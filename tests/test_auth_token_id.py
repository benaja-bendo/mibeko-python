"""Tests de la validation du token_id Sanctum (FIX-2).

`pat.id` est un bigint : un token_id non ASCII (chiffres Unicode) ou hors bornes
faisait remonter une DataError Postgres en 500 au lieu d'un 401 propre. On vérifie
que `_resolve_token` rejette ces cas AVANT toute requête SQL (donc sans jamais
toucher la base), et qu'un id ASCII valide passe la garde et atteint la DB.

Exécutable sans backend : les services MinIO/MinerU ne sont pas importés ici,
et la « DB » est un double qui explose si on l'interroge sur un id invalide.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.auth import _TOKEN_ID_RE, _resolve_token  # noqa: E402


class ExplodingDB:
    """Double de session : toute exécution SQL est une erreur de test.

    Sert à prouver que les token_id invalides sont rejetés AVANT la requête.
    """

    def execute(self, *args, **kwargs):  # pragma: no cover - ne doit jamais être appelé
        raise AssertionError("Aucune requête SQL ne doit partir pour un token_id invalide.")


# ---------------------------------------------------------------------------
# Regex de bornage (unitaire)
# ---------------------------------------------------------------------------

def test_regex_accepte_les_entiers_ascii_bornes():
    assert _TOKEN_ID_RE.match("1")
    assert _TOKEN_ID_RE.match("42")
    assert _TOKEN_ID_RE.match("123456789012345678")  # 18 chiffres


def test_regex_rejette_hors_bornes_et_non_ascii():
    # 19 chiffres : au-delà de la borne (proche du max bigint) → refusé.
    assert not _TOKEN_ID_RE.match("1234567890123456789")
    # Débordement franc du bigint.
    assert not _TOKEN_ID_RE.match("99999999999999999999")
    # Chiffres Unicode (exposant, arabe-indien) : acceptés par str.isdigit(),
    # refusés ici.
    assert not _TOKEN_ID_RE.match("²")  # ²
    assert not _TOKEN_ID_RE.match("٩")  # ٩
    # Non numérique, vide, signes.
    assert not _TOKEN_ID_RE.match("")
    assert not _TOKEN_ID_RE.match("-1")
    assert not _TOKEN_ID_RE.match("12x")
    assert not _TOKEN_ID_RE.match(" 12")


# ---------------------------------------------------------------------------
# _resolve_token : rejet AVANT toute requête SQL
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "token",
    [
        "99999999999999999999|abc",  # débordement bigint
        "²|abc",                # chiffre Unicode ²
        "٩|abc",                # chiffre arabe-indien
        "1234567890123456789|abc",   # 19 chiffres, hors borne
        "abc|def",                   # non numérique
        "|abc",                      # id vide
    ],
)
def test_resolve_token_rejette_sans_toucher_la_db(token):
    # ExplodingDB lève si une requête part : la garde doit renvoyer None avant.
    assert _resolve_token(token, ExplodingDB()) is None


def test_resolve_token_sans_pipe_renvoie_none():
    assert _resolve_token("pas_de_pipe", ExplodingDB()) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
