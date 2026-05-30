"""
Validation des tokens Sanctum directement en base PostgreSQL.
Les tokens Laravel Sanctum sont stockés dans personal_access_tokens (même DB).
"""

import hashlib
from typing import Optional

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.db.database import get_db

bearer_scheme = HTTPBearer(auto_error=False)


class AuthenticatedUser:
    def __init__(self, id: str, name: str, email: str, roles: list[str]):
        self.id = id
        self.name = name
        self.email = email
        self.roles = roles

    def has_role(self, *roles: str) -> bool:
        return any(r in self.roles for r in roles)

    def is_editor_or_above(self) -> bool:
        return self.has_role("admin", "editor")


def _resolve_token(plain_token: str, db: Session) -> Optional[AuthenticatedUser]:
    """
    Sanctum stores SHA-256 hash of the token after the first pipe separator.
    Format: "{id}|{plain_text_token}"
    """
    parts = plain_token.split("|", 1)
    if len(parts) != 2:
        return None

    token_id, token_value = parts
    token_hash = hashlib.sha256(token_value.encode()).hexdigest()

    row = db.execute(
        text("""
            SELECT pat.tokenable_id, u.name, u.email
            FROM personal_access_tokens pat
            JOIN users u ON u.id::text = pat.tokenable_id::text
            WHERE pat.id = :token_id AND pat.token = :token_hash
        """),
        {"token_id": token_id, "token_hash": token_hash},
    ).fetchone()

    if not row:
        return None

    # Fetch roles via Spatie tables
    roles_rows = db.execute(
        text("""
            SELECT r.name
            FROM model_has_roles mhr
            JOIN roles r ON r.id = mhr.role_id
            WHERE mhr.model_id::text = :user_id AND mhr.model_type = 'App\\Models\\User'
        """),
        {"user_id": str(row.tokenable_id)},
    ).fetchall()

    roles = [r.name for r in roles_rows]
    return AuthenticatedUser(id=str(row.tokenable_id), name=row.name, email=row.email, roles=roles)


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
    db: Session = Depends(get_db),
) -> AuthenticatedUser:
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token d'authentification requis.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = _resolve_token(credentials.credentials, db)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token invalide ou expiré.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_editor(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    if not user.is_editor_or_above():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Accès réservé aux éditeurs et administrateurs.",
        )
    return user
