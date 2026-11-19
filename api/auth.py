"""Static demo logins (hackathon) with HMAC-signed bearer tokens.

- Admin: ADMIN_USERNAME / ADMIN_PASSWORD (default Admin / admin) -> POST /admin/login.
- Customer: email of a seeded customer + CUSTOMER_PASSWORD (default user) -> POST /customer/login.

Token format: base64url(json {"sub", "role", "exp"}) + "." + hex HMAC-SHA256(payload, ADMIN_TOKEN_SECRET).
The role decides which dependency accepts the token: require_admin (role "admin") or require_customer
(role "customer"), so a customer token can never call /admin/*.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core import db
from core.config import get_settings

Role = Literal["admin", "customer"]

router = APIRouter(prefix="/admin", tags=["admin"])
customer_router = APIRouter(prefix="/customer", tags=["customer"])
_bearer = HTTPBearer(auto_error=False)


def _sign(payload: str) -> str:
    key = get_settings().admin_token_secret.encode()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def issue_token(subject: str, role: Role) -> tuple[str, int]:
    exp = int(time.time()) + get_settings().admin_token_ttl_minutes * 60
    data = {"sub": subject, "role": role, "exp": exp}
    payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
    return f"{payload}.{_sign(payload)}", exp


def verify_token(token: str) -> tuple[str, str] | None:
    """Return (subject, role) for a valid, unexpired token, else None."""
    try:
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        if int(data["exp"]) < time.time():
            return None
        return str(data["sub"]), str(data["role"])
    except (ValueError, KeyError, TypeError):
        return None


def _expires(exp: int) -> str:
    return datetime.fromtimestamp(exp, timezone.utc).isoformat(timespec="seconds")


def require_admin(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> str:
    v = verify_token(creds.credentials) if creds else None
    if not v or v[1] != "admin":
        raise HTTPException(401, "Admin login required", headers={"WWW-Authenticate": "Bearer"})
    return v[0]


def require_customer(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
    """The logged-in customer's row (without password)."""
    v = verify_token(creds.credentials) if creds else None
    customer = db.get_customer(v[0]) if v and v[1] == "customer" else None
    if not customer:
        raise HTTPException(401, "Customer login required", headers={"WWW-Authenticate": "Bearer"})
    return customer


@router.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    s = get_settings()
    ok = secrets.compare_digest(username.encode(), s.admin_username.encode()) & \
        secrets.compare_digest(password.encode(), s.admin_password.encode())
    db.add_audit(db.ADMIN_AUDIT_ID, username[:64], "admin_login" if ok else "admin_login_failed")
    if not ok:
        raise HTTPException(401, "Invalid username or password")
    token, exp = issue_token(s.admin_username, "admin")
    return {"token": token, "username": s.admin_username, "expires_at": _expires(exp)}


@customer_router.post("/login")
def customer_login(email: str = Form(..., max_length=254), password: str = Form(..., max_length=128)):
    customer = db.authenticate_customer(email, password)
    db.add_audit(db.CUSTOMER_AUDIT_ID, (customer or {}).get("id") or email.strip()[:64],
                 "customer_login" if customer else "customer_login_failed")
    if not customer:
        raise HTTPException(401, "Invalid email or password")
    token, exp = issue_token(customer["id"], "customer")
    return {"token": token, "expires_at": _expires(exp),
            "customer": {k: customer[k] for k in ("id", "name", "email", "phone")}}
