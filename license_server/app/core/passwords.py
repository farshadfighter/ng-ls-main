"""
Admin passwords: hashing and the password policy.

Hashing is scrypt from the standard library (no extra dependency), with a
16-byte random salt per password and parameters stored next to the hash, so
they can be raised later without invalidating existing hashes:

    scrypt$<log2 n>$<r>$<p>$<salt, base64>$<hash, base64>

n = 2^15, r = 8, p = 1 needs 32 MiB per check and takes ~50-100 ms: cheap for
one login, expensive for anyone trying passwords against a stolen hash.

The policy (check_policy) is what the admin UI shows next to the field:
at least 12 characters, at least three of lower case / upper case / digit /
symbol, not containing the user name, and not the same as the current one.
"""
import base64
import hashlib
import hmac
import secrets
import string
from typing import Optional

SCHEME = "scrypt"
LOG2_N = 15
R = 8
P = 1
DKLEN = 32
SALT_BYTES = 16
_MAXMEM = 64 * 1024 * 1024

MIN_LENGTH = 12
MAX_LENGTH = 128  # longer is no stronger and makes every check slower


class PasswordPolicyError(ValueError):
    """A new password does not meet the policy; the message says why."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _derive(password: str, salt: bytes, log2_n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** log2_n, r=r, p=p,
                          dklen=DKLEN, maxmem=_MAXMEM)


def hash_password(password: str) -> str:
    """A salted scrypt hash of the password, in the self-describing format above."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _derive(password, salt, LOG2_N, R, P)
    return f"{SCHEME}${LOG2_N}${R}${P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: Optional[str]) -> bool:
    """Whether the password matches the stored hash. Constant time in the comparison; a
    malformed or missing hash never matches."""
    if not stored or not isinstance(password, str) or len(password) > MAX_LENGTH:
        return False
    try:
        scheme, log2_n, r, p, salt, digest = stored.split("$")
        if scheme != SCHEME:
            return False
        expected = base64.b64decode(digest)
        actual = _derive(password, base64.b64decode(salt), int(log2_n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def dummy_verify() -> None:
    """Spend the time of one real check. Used for an unknown user name, so a login attempt
    takes as long whether or not the account exists (no user enumeration by timing)."""
    _derive("not-a-password", b"\0" * SALT_BYTES, LOG2_N, R, P)


def check_policy(password: str, username: str = "", current_hash: Optional[str] = None) -> None:
    """PasswordPolicyError unless the password meets the policy."""
    if not isinstance(password, str) or len(password) < MIN_LENGTH:
        raise PasswordPolicyError(f"The password must be at least {MIN_LENGTH} characters long.")
    if len(password) > MAX_LENGTH:
        raise PasswordPolicyError(f"The password must be at most {MAX_LENGTH} characters long.")
    classes = sum((
        any(c.islower() for c in password),
        any(c.isupper() for c in password),
        any(c.isdigit() for c in password),
        any(c in string.punctuation or c.isspace() or not c.isascii() for c in password),
    ))
    if classes < 3:
        raise PasswordPolicyError(
            "The password must contain at least three of: lower case, upper case, digits, symbols."
        )
    if username and username.lower() in password.lower():
        raise PasswordPolicyError("The password must not contain the user name.")
    if current_hash and verify_password(password, current_hash):
        raise PasswordPolicyError("The new password must differ from the current one.")
