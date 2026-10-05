"""
Signed license tokens.

Every licensing answer this server gives NGCorion (activate, validate,
heartbeat, consume) carries a token signed with the server's Ed25519 private
key. NGCorion has the matching public key built into its code and acts only on
what a valid token says, so a look-alike server - anything that merely answers
{"valid": true} - can no longer unlock the product: without the private key it
cannot produce a signature NGCorion accepts.

Format (deliberately small, no JWT library on either side):

    base64url(claims JSON) "." base64url(Ed25519 signature over those bytes)

Claims (see build_claims): token version, key id, type of answer, license key,
machine fingerprint, verdict and message, plan, limits, usage, license expiry,
issue time, and the nonce the client sent with its request so a recorded
answer cannot be replayed to it later.

The private key comes from LICENSE_SIGNING_PRIVATE_KEY (base64 of the 32-byte
Ed25519 seed, made by scripts/generate_signing_key.py). It must never be
committed or leave the license server; anyone holding it can mint licenses.
"""
import base64
import hashlib
import json
import time
from functools import lru_cache
from typing import Any, Dict, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

TOKEN_VERSION = 1


class SigningKeyMissing(RuntimeError):
    """LICENSE_SIGNING_PRIVATE_KEY is not set or is not a valid key."""


def b64url(data: bytes) -> str:
    """URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def load_private_key(value: str) -> Ed25519PrivateKey:
    """The private key from its base64 32-byte seed; SigningKeyMissing when unusable."""
    try:
        raw = base64.b64decode((value or "").strip(), validate=True)
    except (ValueError, TypeError) as exc:
        raise SigningKeyMissing("LICENSE_SIGNING_PRIVATE_KEY is not valid base64") from exc
    if len(raw) != 32:
        raise SigningKeyMissing(
            "LICENSE_SIGNING_PRIVATE_KEY must be the base64 of a 32-byte Ed25519 seed "
            "(generate one with scripts/generate_signing_key.py)"
        )
    return Ed25519PrivateKey.from_private_bytes(raw)


def public_key_raw(private_key: Ed25519PrivateKey) -> bytes:
    """The 32 raw bytes of the matching public key."""
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )


def key_id(public_raw: bytes) -> str:
    """Short, stable id of a public key, so clients can trust several keys during a rotation."""
    return hashlib.sha256(public_raw).hexdigest()[:16]


class TokenSigner:
    """Signs claim sets with one private key."""

    def __init__(self, private_key: Ed25519PrivateKey):
        self._key = private_key
        self.public_raw = public_key_raw(private_key)
        self.kid = key_id(self.public_raw)

    @property
    def public_key_b64(self) -> str:
        """The public key as NGCorion's trusted-key list expects it (standard base64)."""
        return base64.b64encode(self.public_raw).decode("ascii")

    def sign(self, claims: Dict[str, Any]) -> str:
        """Serialise the claims (with this key's id) and sign them."""
        body = dict(claims, v=TOKEN_VERSION, kid=self.kid)
        payload = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return f"{b64url(payload)}.{b64url(self._key.sign(payload))}"


@lru_cache(maxsize=1)
def _signer_for(value: str) -> TokenSigner:
    return TokenSigner(load_private_key(value))


def get_signer() -> TokenSigner:
    """The signer for the configured key; SigningKeyMissing when none is configured."""
    from ..core.config import settings

    value = (settings.LICENSE_SIGNING_PRIVATE_KEY or "").strip()
    if not value:
        raise SigningKeyMissing(
            "LICENSE_SIGNING_PRIVATE_KEY is not set: the license server cannot sign answers, "
            "and NGCorion refuses unsigned ones"
        )
    return _signer_for(value)


def build_claims(
    typ: str,
    *,
    license_key: str,
    vm_fingerprint: str,
    valid: bool,
    message: str,
    nonce: Optional[str],
    license=None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The claim set of one answer. `license` (a models.License, or None when the key is unknown)
    supplies plan, limits, usage and expiry; the verdict is always the caller's.
    """
    claims: Dict[str, Any] = {
        "typ": typ,
        "lic": license_key,
        "fp": vm_fingerprint,
        "valid": bool(valid),
        "msg": message,
        "nonce": nonce,
        "iat": int(time.time()),
        "plan": None,
        "pilot": False,
        "limits": None,
        "usage": None,
        "exp": None,
    }
    if license is not None:
        plan = getattr(license.plan_type, "value", license.plan_type)
        claims.update({
            "plan": plan,
            "pilot": bool(license.is_pilot_mode),
            "limits": {"max_audits": license.max_audits, "max_hardens": license.max_hardens},
            "usage": {"used_audits": license.used_audits, "used_hardens": license.used_hardens},
            "exp": int(license.expires_at.timestamp()) if license.expires_at else None,
        })
    if extra:
        claims.update(extra)
    return claims
