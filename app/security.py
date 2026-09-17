from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from itsdangerous import BadSignature, BadTimeSignature, TimedSerializer

_hasher = PasswordHasher()
_SESSION_SALT = "remember-session"


def hash_password(pw: str) -> str:
    """Hash a password with argon2."""
    return _hasher.hash(pw)


def verify_password(pw: str, hashed: str) -> bool:
    """Verify a plaintext password against an argon2 hash."""
    try:
        return _hasher.verify(hashed, pw)
    except (VerifyMismatchError, InvalidHashError):
        return False


def sign_session(payload: dict, secret: str, max_age_seconds: int) -> str:
    """Sign a session payload; the lifetime is enforced on verification."""
    if max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be positive")
    serializer = TimedSerializer(secret, salt=_SESSION_SALT)
    return serializer.dumps(payload)


def verify_session(token: str, secret: str, max_age_seconds: int) -> dict | None:
    """Verify and return a session payload, or None if invalid/expired."""
    serializer = TimedSerializer(secret, salt=_SESSION_SALT)
    try:
        payload = serializer.loads(token, max_age=max_age_seconds)
        return payload if isinstance(payload, dict) else None
    except (BadSignature, BadTimeSignature):
        return None