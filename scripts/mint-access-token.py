"""Mint an integration-only HS256 Access JWT accepted by Core's normal decoder."""
import base64, hashlib, hmac, json, os, time, uuid

def part(value): return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=")
header = part({"alg": "HS256", "typ": "JWT"})
now = int(time.time())
claims = part({"sub": os.environ.get("INTEGRATION_MEMBER_ID", "900000000000001"), "role": "MEMBER", "iss": os.environ["REFINVEST_JWT_ISSUER"], "aud": [os.environ["REFINVEST_JWT_AUDIENCE"]], "iat": now, "exp": now + 3600, "jti": str(uuid.uuid4()), "typ": "access"})
signed = header + b"." + claims
signature = base64.urlsafe_b64encode(hmac.new(os.environ["REFINVEST_JWT_SECRET"].encode(), signed, hashlib.sha256).digest()).rstrip(b"=")
print((signed + b"." + signature).decode())
