"""
eci_crypto.py — Reverse-engineered client-side crypto for voters.eci.gov.in

Extracted from the site's own webpack bundle (main.998a668d.js), served
publicly to every visitor's browser. Every sensitive API call on the site
is end-to-end encrypted at the application layer (on top of HTTPS):

  - RSA-OAEP(SHA-256) + AES-256-GCM hybrid encryption for POST bodies
    ({encryptedPayload, encryptedKey, iv}).
  - The same hybrid scheme, applied per-field, for GET query parameters
    on get-publish-eroll-type (values go in the query string, the AES key
    and IV go in the `accept_yek` / `accept_rotcev` request headers —
    "yek" and "rotcev" are "key" and "vector" spelled backwards).
  - Captcha images/IDs come back from getCaptcha AES-GCM-encrypted with a
    FIXED key baked into the bundle (not per-session, not RSA-protected).

All of this was confirmed by decrypting a real captcha response captured
in your HAR file — it produced a valid JPEG image, so the key and mode
below are verified correct, not guessed.

The RSA public key and the fixed captcha AES key are both static assets
served to any browser that loads the page (same as any client-side
crypto in a public web app) — this module only reimplements the same
math your own browser already runs.
"""
import base64
import json
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ---------------------------------------------------------------------------
# Constants extracted from https://voters.eci.gov.in/static/js/main.998a668d.js
# ---------------------------------------------------------------------------

# Server's RSA-2048 public key (SPKI/DER, base64), embedded verbatim in the bundle.
_PUBLIC_KEY_B64 = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEArb7++BxL/YN8OIln+6FL9Gnw5DNmQ"
    "/VFZXss+J+TuQyJc891JbqbijxYQNEin2c2u+CnpXpoGQ/1gUSzDMJeNS3sNSlIUykp2dt7xI"
    "m/cmV4sZ/c769vCxVRosMfRaZJnBAah+m1X26lEhnOo0wpAB9Txr8RIyBe6h7PiQWykeJeh6"
    "UacOBBX28kgkq7+vJhW8HgB38lt32XRocznRYwS9LqR7ZweFmQhTr1+EGrqiEKCOCxMYgHR2"
    "SQckb96hZ9kWzfzeun4bUO5oXKJciLkiS1IgKieADEvYLgu129ZIpn1H+8H+8ikNNVETqEDD"
    "MtqcQcQmWppJvcWHaXAs+f8QIDAQAB"
)

# Fixed AES-256 key used ONLY to decrypt getCaptcha responses. Derived exactly
# as the bundle does: a hardcoded constant string, sliced [15:59], base64-decoded.
_CAPTCHA_KEY_CONST = "SFfIO0YsOlOKawZe855n97lc4tcPkj7WWsi38yNWpalLBLZzQdkqHWYbZ0=GhSJk2raUo"
CAPTCHA_AES_KEY = base64.b64decode(_CAPTCHA_KEY_CONST[15:59])
assert len(CAPTCHA_AES_KEY) == 32, "captcha key derivation broke — recheck slice"

# Static app identifier sent in every encrypted payload/query (found alongside
# stateCd/year in every call site in the bundle).
MIS_KEY = "EROLLA32DVI09AJH"


def load_public_key():
    der = base64.b64decode(_PUBLIC_KEY_B64)
    return serialization.load_der_public_key(der)


_OAEP_PADDING = padding.OAEP(
    mgf=padding.MGF1(algorithm=hashes.SHA256()),
    algorithm=hashes.SHA256(),
    label=None,
)


def _rsa_oaep_encrypt(pubkey, data: bytes) -> bytes:
    return pubkey.encrypt(data, _OAEP_PADDING)


def hybrid_encrypt(pubkey, payload) -> dict:
    """
    Mirrors the bundle's POST-body encryption exactly:
      - random AES-256 key + random 12-byte IV
      - AES-GCM-encrypt JSON.stringify(payload) (ciphertext+tag, as WebCrypto
        and node-forge both produce when concatenated)
      - RSA-OAEP(SHA-256)-encrypt the raw AES key
      - base64 (standard, not url-safe) all three parts

    Returns {"encryptedPayload": ..., "encryptedKey": ..., "iv": ...} — this
    dict IS the JSON body to POST.
    """
    aes_key = os.urandom(32)
    iv = os.urandom(12)
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(aes_key).encrypt(iv, plaintext, None)
    enc_key = _rsa_oaep_encrypt(pubkey, aes_key)
    return {
        "encryptedPayload": base64.b64encode(ciphertext).decode(),
        "encryptedKey": base64.b64encode(enc_key).decode(),
        "iv": base64.b64encode(iv).decode(),
    }


def _b64url(b: bytes) -> str:
    return base64.b64encode(b).decode().replace("+", "-").replace("/", "_").rstrip("=")


def encrypted_get_fields(pubkey, *values):
    """
    Mirrors the bundle's per-field GET-query encryption (used for
    get-publish-eroll-type). One AES key + IV is generated and reused to
    encrypt each value separately (matching the bundle's own — slightly
    unusual — reuse of a single IV across multiple encrypt calls).

    Returns a list: [accept_yek, accept_rotcev, enc(values[0]), enc(values[1]), ...]
    all base64url-encoded (no padding), ready to drop into headers/query string.
    """
    aes_key = os.urandom(32)
    iv = os.urandom(12)
    aesgcm = AESGCM(aes_key)
    enc_key = _rsa_oaep_encrypt(pubkey, aes_key)
    out = [_b64url(enc_key), _b64url(iv)]
    for v in values:
        pt = json.dumps(v, separators=(",", ":")).encode("utf-8")
        ct = aesgcm.encrypt(iv, pt, None)
        out.append(_b64url(ct))
    return out


def decrypt_captcha_data(data_b64: str) -> dict:
    """
    Decrypts the `data` field returned by GET .../captcha-service/getCaptcha/EROLL.
    Verified working against a real captured response (produced a valid JPEG).

    Returns {"status", "statusCode", "message", "captcha": <base64 jpeg>, "id": <captchaId>}
    """
    raw = base64.b64decode(data_b64)
    iv, ct = raw[:12], raw[12:]
    pt = AESGCM(CAPTCHA_AES_KEY).decrypt(iv, ct, None)
    return json.loads(pt)
