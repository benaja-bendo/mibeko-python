"""
Validation des tokens Sanctum directement en base PostgreSQL.
Les tokens Laravel Sanctum sont stockés dans personal_access_tokens (même DB).
"""

import hashlib
import re
from typing import Optional

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.db.database import get_db

bearer_scheme = HTTPBearer(auto_error=False)

# pat.id est un bigint signé PostgreSQL (max 9223372036854775807, 19 chiffres).
# On borne à 18 chiffres ASCII : couvre tout id réel sans jamais approcher la
# limite bigint. `str.isdigit()` était trop laxiste — il accepte les chiffres
# Unicode (« ² », « ٩ ») et n'a aucune borne, ce qui laissait passer des valeurs
# converties en 500 DataError par Postgres. Un `^[0-9]{1,18}$` strict règle les deux.
_TOKEN_ID_RE = re.compile(r"^[0-9]{1,18}$")


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
    # pat.id est un bigint : on refuse tout id qui n'est pas un entier ASCII borné
    # avant la requête (sinon Postgres lève une DataError → 500 au lieu d'un 401).
    # `isdigit()` acceptait les chiffres Unicode et débordait le bigint : un regex
    # ASCII strict borné à 18 chiffres ferme les deux failles.
    if not _TOKEN_ID_RE.match(token_id):
        return None
    token_hash = hashlib.sha256(token_value.encode()).hexdigest()

    # Miroir de la validation Sanctum côté Laravel : hash du token, mais aussi
    # expiration (expires_at NULL = sans limite) et type du modèle porteur —
    # seuls les tokens d'un User Laravel ('App\Models\User') sont acceptés ici.
    row = db.execute(
        text("""
            SELECT pat.tokenable_id, u.name, u.email
            FROM personal_access_tokens pat
            JOIN users u ON u.id::text = pat.tokenable_id::text
            WHERE pat.id = :token_id
              AND pat.token = :token_hash
              AND (pat.expires_at IS NULL OR pat.expires_at > NOW())
              AND pat.tokenable_type = 'App\\Models\\User'
        """),
        {"token_id": token_id, "token_hash": token_hash},
    ).fetchone()

    if not row:
        return None

    # Trace d'usage alignée sur Sanctum (last_used_at) — best-effort : un échec
    # de cette écriture ne doit jamais bloquer l'authentification.
    try:
        db.execute(
            text("UPDATE personal_access_tokens SET last_used_at = NOW() WHERE id = :token_id"),
            {"token_id": token_id},
        )
        db.commit()
    except Exception:
        db.rollback()

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
