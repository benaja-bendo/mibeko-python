"""Auth Sanctum : un token d'utilisateur soft-deleted doit donner 401.

Audit drift schéma du 08/08/2026 : la requête de `_resolve_token` joignait
`users` sans filtrer `deleted_at` (SoftDeletes Laravel). Un compte supprimé dont
le token n'avait pas été révoqué s'authentifiait donc encore sur toute l'API
Python — `require_editor` compris, le rôle Spatie survivant à la suppression —
alors que le scope SoftDeletes du guard Laravel aurait répondu 401. La
suspension n'est volontairement PAS vérifiée ici : côté Laravel elle n'a pas de
contrôle par requête (suspendre révoque les tokens, `UserController::applyStatus`)
— même parité de ce côté-ci.

Ces tests écrivent dans la base de développement réelle (conftest refuse toute
autre cible) : un utilisateur et son token éphémères, purgés physiquement en
sortie — nettoyage de nos propres lignes de test en dev.
"""

import hashlib
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.security import HTTPAuthorizationCredentials  # noqa: E402
from sqlalchemy import text  # noqa: E402

from src.api.auth import _resolve_token, get_current_user  # noqa: E402
from src.db.database import SessionLocal  # noqa: E402


@pytest.fixture
def utilisateur_avec_token():
    """Utilisateur soft-deleted + token Sanctum valide, purge garantie."""
    marqueur = uuid.uuid4().hex[:12]
    user_id = str(uuid.uuid4())
    token_clair = f"jeton-test-{marqueur}"
    token_hash = hashlib.sha256(token_clair.encode()).hexdigest()

    db = SessionLocal()
    try:
        db.execute(
            text(
                "insert into users (id, name, email, password, deleted_at) "
                "values (:id, :name, :email, 'hash-inutile', now())"
            ),
            {"id": user_id, "name": "Compte supprimé", "email": f"test-{marqueur}@mibeko.test"},
        )
        pat_id = db.execute(
            text(
                "insert into personal_access_tokens "
                "(tokenable_type, tokenable_id, name, token, created_at, updated_at) "
                "values ('App\\Models\\User', :user_id, :name, :token, now(), now()) "
                "returning id"
            ),
            {"user_id": user_id, "name": f"test-{marqueur}", "token": token_hash},
        ).scalar_one()
        db.commit()

        yield db, user_id, f"{pat_id}|{token_clair}"
    finally:
        db.rollback()
        db.execute(text("delete from personal_access_tokens where tokenable_id = :id"), {"id": user_id})
        db.execute(text("delete from users where id = :id"), {"id": user_id})
        db.commit()
        db.close()


def test_token_d_utilisateur_soft_deleted_est_refuse(utilisateur_avec_token):
    db, _, token = utilisateur_avec_token

    assert _resolve_token(token, db) is None

    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    with pytest.raises(HTTPException) as excinfo:
        get_current_user(credentials=credentials, db=db)
    assert excinfo.value.status_code == 401


def test_le_meme_token_redevient_valide_si_le_compte_est_restaure(utilisateur_avec_token):
    """Contre-épreuve : seul deleted_at explique le refus du test précédent."""
    db, user_id, token = utilisateur_avec_token

    db.execute(text("update users set deleted_at = null where id = :id"), {"id": user_id})
    db.commit()

    user = _resolve_token(token, db)
    assert user is not None
    assert user.id == user_id
    assert user.name == "Compte supprimé"
