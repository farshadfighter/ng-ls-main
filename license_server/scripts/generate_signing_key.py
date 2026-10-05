"""
Generate the Ed25519 key pair that signs licensing answers.

Run once on the license server (or any trusted machine):

    python scripts/generate_signing_key.py

It prints two values:

* LICENSE_SIGNING_PRIVATE_KEY - put it in the license server's .env only.
  Never commit it, never send it anywhere: whoever holds it can mint licenses.
* the public key and its id - these are not secret. They go into NGCorion's
  trusted key list (app/core/license_keys.py), which is what makes NGCorion
  accept answers from this server and nobody else.

To rotate keys, generate a new pair, add the new public key to NGCorion next to
the old one, ship that NGCorion release, then switch the server to the new
private key and finally drop the old public key in a later release.
"""
import base64
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from app.utils.license_token import TokenSigner  # noqa: E402


def main() -> None:
    """Print a fresh private key and its public half."""
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    signer = TokenSigner(key)
    print("# License server .env (SECRET - keep only on the license server):")
    print(f"LICENSE_SIGNING_PRIVATE_KEY={base64.b64encode(seed).decode()}")
    print()
    print("# NGCorion app/core/license_keys.py (public, safe to share):")
    print(f'    "{signer.kid}": "{signer.public_key_b64}",')


if __name__ == "__main__":
    main()
