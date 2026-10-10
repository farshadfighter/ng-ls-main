"""
Offline (air-gapped) licenses: the request file NGCorion makes and the license file this server
signs for it.

An NGCorion server that never reaches the internet cannot activate or send heartbeats. For such
a customer the license is marked offline (licenses.offline_allowed, set by an admin) and the
exchange goes by file:

1. NGCorion writes a request file: the license key, the fingerprint of the machine it runs on,
   how many assets it manages, a nonce and the time. Not signed (NGCorion has no secret that
   could sign it); it only says what to put in the license file.
2. An admin uploads the request in the admin UI. This server checks the license (offline
   allowed, active, not expired, not bound to another machine), binds it to the request's
   machine, and signs a license file: an ordinary signed token (app/utils/license_token.py) with
   typ "offline" and offline_until = one year from now, or the license's own expiry if sooner.
3. NGCorion verifies the file with its built-in public key and runs on it, without contact, until
   offline_until. Renewal is the same exchange again.

A file cannot be edited (the signature breaks), forged (no private key) or moved to another
machine (it names the fingerprint). It also cannot be revoked before offline_until: that is
inherent to a machine nobody can reach, and the reason for the one-year bound.
"""
import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

REQUEST_HEADER = "NGCORION-OFFLINE-REQUEST-1"
LICENSE_HEADER = "NGCORION-OFFLINE-LICENSE-1"
FILE_VALIDITY = timedelta(days=365)
MAX_REQUEST_BYTES = 8192


class InvalidOfflineRequest(ValueError):
    """The uploaded text is not an NGCorion offline license request."""


class InvalidOfflineLicense(ValueError):
    """The uploaded text is not an NGCorion offline license file."""


@dataclass(frozen=True)
class OfflineRequest:
    license_key: str
    vm_fingerprint: str
    asset_count: Optional[int]
    nonce: str
    created_at: Optional[datetime]


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def parse_request(text: str) -> OfflineRequest:
    """The request in an uploaded request file; InvalidOfflineRequest when it is not one."""
    if not text or len(text) > MAX_REQUEST_BYTES:
        raise InvalidOfflineRequest("This is not an NGCorion offline license request.")
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if len(lines) != 2 or lines[0] != REQUEST_HEADER:
        raise InvalidOfflineRequest(
            "This is not an NGCorion offline license request (made on the License page of NGCorion)."
        )
    try:
        data = json.loads(_b64decode(lines[1]))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise InvalidOfflineRequest("The request file is damaged.") from exc
    if not isinstance(data, dict):
        raise InvalidOfflineRequest("The request file is damaged.")

    key, fp, nonce = data.get("lic"), data.get("fp"), data.get("nonce")
    if not isinstance(key, str) or not key.strip() or len(key) > 64:
        raise InvalidOfflineRequest("The request names no license key.")
    if not isinstance(fp, str) or not (16 <= len(fp) <= 128) or not fp.isalnum():
        raise InvalidOfflineRequest("The request carries no valid machine fingerprint.")
    if not isinstance(nonce, str) or not (8 <= len(nonce) <= 128):
        raise InvalidOfflineRequest("The request is damaged (no nonce).")
    assets = data.get("assets")
    if assets is not None and (not isinstance(assets, int) or isinstance(assets, bool) or not 0 <= assets <= 10_000_000):
        raise InvalidOfflineRequest("The request's asset count is not valid.")
    created = None
    if isinstance(data.get("created"), int):
        try:
            created = datetime.fromtimestamp(data["created"], tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            created = None
    return OfflineRequest(key.strip(), fp, assets, nonce, created)


def valid_until(now: datetime, license_expires_at: datetime) -> datetime:
    """How long a file issued now works: one year, never past the license's own expiry."""
    return min(now + FILE_VALIDITY, license_expires_at)


def license_file(token: str) -> str:
    """The license file NGCorion uploads: a header line and the signed token."""
    return f"{LICENSE_HEADER}\n{token}\n"


def file_name(organization_name: str, license_key: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in (organization_name or "license")).strip("_").lower()
    return f"{safe or 'license'}_{license_key}.ngclic"


@dataclass(frozen=True)
class OfflineLicenseFile:
    """What an uploaded license file says, and whether a given key signed it."""
    claims: Dict[str, Any]
    signature_valid: Optional[bool]  # None: no key to check it with


def kind_of(text: str) -> Optional[str]:
    """'request' or 'license' by the file's first line; None for anything else."""
    first = (text or "").strip().split("\n", 1)[0].strip()
    return {REQUEST_HEADER: "request", LICENSE_HEADER: "license"}.get(first)


def read_license_file(text: str, public_raw: Optional[bytes]) -> OfflineLicenseFile:
    """The claims in an uploaded license file, and whether public_raw (this server's key) signed
    them (None when no key is given). A wrong signature is reported, not raised: the admin wants
    to know what the file says either way. InvalidOfflineLicense when the text is not a license
    file at all."""
    if not text or len(text) > MAX_REQUEST_BYTES:
        raise InvalidOfflineLicense("This is not an NGCorion offline license file.")
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if len(lines) != 2 or lines[0] != LICENSE_HEADER:
        raise InvalidOfflineLicense("This is not an NGCorion offline license file (.ngclic).")
    parts = lines[1].split(".")
    if len(parts) != 2:
        raise InvalidOfflineLicense("The license file is damaged.")
    try:
        payload, signature = _b64decode(parts[0]), _b64decode(parts[1])
        claims = json.loads(payload)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise InvalidOfflineLicense("The license file is damaged.") from exc
    if not isinstance(claims, dict):
        raise InvalidOfflineLicense("The license file is damaged.")
    valid = None
    if public_raw is not None:
        try:
            Ed25519PublicKey.from_public_bytes(public_raw).verify(signature, payload)
            valid = True
        except (InvalidSignature, ValueError):
            valid = False
    return OfflineLicenseFile(claims=claims, signature_valid=valid)
