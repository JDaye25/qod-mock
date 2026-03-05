import base64
import json
import sys
import urllib.request

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

def b64url_decode(s: str) -> bytes:
    s = (s or "").strip()
    pad = "=" * ((4 - (len(s) % 4)) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("ascii"))

def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))

def pick_jwk(jwks: dict, kid: str) -> dict:
    keys = jwks.get("keys") or []
    for k in keys:
        if k.get("kid") == kid:
            return k
    if len(keys) == 1:
        return keys[0]
    raise SystemExit(f"Could not find key for kid={kid} in JWKS")

def main():
    if len(sys.argv) != 3:
        print("usage: python verify_signature.py <base_url> <session_id>")
        sys.exit(2)

    base = sys.argv[1].rstrip("/")
    sid = sys.argv[2]

    proof = get_json(f"{base}/proof/{sid}")
    this_hash = proof["this_hash"]
    sig_b64u = proof["signature"]
    kid = proof.get("kid") or "default"

    jwks = get_json(f"{base}/.well-known/jwks.json")
    jwk = pick_jwk(jwks, kid)

    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        raise SystemExit("JWKS key is not Ed25519 OKP")

    x = jwk["x"]
    pub_raw = b64url_decode(x)
    pub = Ed25519PublicKey.from_public_bytes(pub_raw)

    sig = b64url_decode(sig_b64u)

    try:
        pub.verify(sig, this_hash.encode("utf-8"))
        print("✅ signature VERIFIED")
        print("kid:", kid)
        print("this_hash:", this_hash)
    except InvalidSignature:
        print("❌ signature INVALID")
        sys.exit(1)

if __name__ == "__main__":
    main()