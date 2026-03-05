import base64
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")

priv = Ed25519PrivateKey.generate()
pub = priv.public_key()

lines = [
    "QOD_ACTIVE_SIGNING_KID=default",
    "QOD_SIGNING_PRIVATE_KEY_B64URL=" + b64u(priv.private_bytes_raw()),
    "QOD_SIGNING_PUBLIC_KEY_B64URL=" + b64u(pub.public_bytes_raw()),
]

with open(".env", "w", encoding="ascii") as f:
    f.write("\n".join(lines) + "\n")

print("Created .env with new signing keys.")