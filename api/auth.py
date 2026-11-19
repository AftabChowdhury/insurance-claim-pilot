"""Static admin login (hackathon demo) with an HMAC-signed bearer token.

Credentials come from settings (ADMIN_USERNAME / ADMIN_PASSWORD, default Admin / admin).
Token format: base64url(json {"sub", "exp"}) + "." + hex HMAC-SHA256(payload, ADMIN_TOKEN_SECRET).
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core import db
from core.config import get_settings

router = APIRouter(prefix="/admin", tags=["admin"])
_bearer = HTTPBearer(auto_error=False)


def _sign(payload: str) -> str:
    key = get_settings().admin_token_secret.encode()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def issue_token(username: str) -> tuple[str, int]:
    exp = int(time.time()) + get_settings().admin_token_ttl_minutes * 60
    payload = base64.urlsafe_b64encode(json.dumps({"sub": username, "exp": exp}).encode()).decode()
    return f"{payload}.{_sign(payload)}", exp


def verify_token(token: str) -> str | None:
    """Return the username for a valid, unexpired token, else None."""
    try:
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        if int(data["exp"]) < time.time():
            return None
        return str(data["sub"])
    except (ValueError, KeyError, TypeError):
        return None


def require_admin(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> str:
    user = verify_token(creds.credentials) if creds else None
    if not user:
        raise HTTPException(401, "Admin login required", headers={"WWW-Authenticate": "Bearer"})
    return user


@router.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    s = get_settings()
    ok = secrets.compare_digest(username.encode(), s.admin_username.encode()) & \
        secrets.compare_digest(password.encode(), s.admin_password.encode())
    db.add_audit(db.ADMIN_AUDIT_ID, username[:64], "admin_login" if ok else "admin_login_failed")
    if not ok:
        raise HTTPException(401, "Invalid username or password")
    token, exp = issue_token(s.admin_username)
    return {"token": token, "username": s.admin_username,
            "expires_at": datetime.fromtimestamp(exp, timezone.utc).isoformat(timespec="seconds")}
