# =============================================================================
# Authentication service: password hashing/verification and JWT session tokens.
#
# This is where the "how do we know who is logged in" logic lives. It has
# three jobs:
#   1. Verify a submitted email/password against the stored Argon2 hash.
#   2. Issue a signed JWT ("JSON Web Token") after a successful login, which
#      is what gets stored in the browser's access_token cookie.
#   3. Decode/validate that JWT on every subsequent request (see
#      app/dependencies.py, which calls into this service).
# =============================================================================
import hashlib
from datetime import datetime, timedelta, timezone

import jwt
from pwdlib import PasswordHash

from app.config import get_settings
from app.models.user import User
from app.repositories.user_repository import UserRepository


# PasswordHash.recommended() picks pwdlib's current best-practice algorithm
# (Argon2, per the project's dependencies) and its recommended parameters
# (memory/time cost), so this project does not need to choose or tune the
# hashing algorithm manually.
password_hash = PasswordHash.recommended()


class AuthService:
    """Encapsulates login verification and JWT token issue/decode."""

    def __init__(self, repository: UserRepository) -> None:
        self.repository = repository
        self.settings = get_settings()

    def authenticate(self, email: str, password: str) -> User | None:
        """Check an email/password pair and return the matching User, or None.

        Deliberately returns the SAME result (None) whether the email does
        not exist, the account is deactivated, or the password is wrong.
        This prevents an attacker from using the login form to discover
        which email addresses have accounts ("user enumeration").
        """
        user = self.repository.get_by_email(email.strip())
        if not user or not user.is_active:
            return None
        # `verify` re-hashes the submitted password using the same salt/
        # parameters stored in user.password_hash and compares the results -
        # the plaintext password is never stored or compared directly.
        if not password_hash.verify(password, user.password_hash):
            return None
        return user

    def create_access_token(self, user: User) -> str:
        """Build a signed JWT that represents an authenticated session.

        The payload intentionally contains only non-sensitive data (user id,
        role, an expiry time, and a "password version" fingerprint) - never
        the password or its hash directly. `jwt.encode` signs this payload
        with SECRET_KEY using HMAC-SHA256, so any tampering with the payload
        (e.g. changing the role) would invalidate the signature and be
        rejected on decode.
        """
        expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=self.settings.access_token_expire_minutes
        )
        payload = {
            "sub": str(user.id),
            "role": user.role.value,
            "password_version": self._password_version(user.password_hash),
            "exp": expires_at,
        }
        return jwt.encode(payload, self.settings.secret_key, algorithm="HS256")

    def decode_user_id(self, token: str) -> int | None:
        """Extract the user id from a token, or None if it is invalid/expired.

        `jwt.decode` automatically verifies both the signature and the
        expiry ("exp") claim; any failure (wrong signature, malformed token,
        expired token) raises an exception, which is caught here and turned
        into a simple None so callers do not need to handle JWT-specific
        exception types themselves.
        """
        try:
            payload = jwt.decode(token, self.settings.secret_key, algorithms=["HS256"])
            return int(payload["sub"])
        except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
            return None

    def token_matches_user(self, token: str, user: User) -> bool:
        """Reject a token if the user's password has changed since it was issued.

        Without this check, changing your password would NOT actually log
        out other sessions/devices, since a JWT is normally valid until it
        naturally expires. To close that gap, every token embeds a
        fingerprint of the password hash at the moment of login
        (`password_version`). If the password is later changed, the hash -
        and therefore this fingerprint - changes too, so any older token
        will no longer match and is treated as invalid.
        """

        try:
            payload = jwt.decode(
                token,
                self.settings.secret_key,
                algorithms=["HS256"],
            )
            return (
                int(payload["sub"]) == user.id
                and payload["password_version"]
                == self._password_version(user.password_hash)
            )
        except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _password_version(password_hash_value: str) -> str:
        """Derive a short, stable fingerprint from a password hash.

        Not used for any security purpose itself (it is not a secret) -
        it is purely a cheap way to detect "has the password hash changed
        since this token was issued?" without storing the full hash inside
        the JWT payload.
        """
        return hashlib.sha256(password_hash_value.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    """Hash a plaintext password for storage, using the Argon2 algorithm.

    Called during registration and password changes. This is a one-way
    operation - there is no corresponding "unhash" function, because the
    plaintext password should never need to be recovered, only verified
    against (see AuthService.authenticate above).
    """
    return password_hash.hash(password)
