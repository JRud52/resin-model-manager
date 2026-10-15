"""Sign in with an OpenID Connect provider (Authelia and the like), next to the local logins.

The login page shows a "Sign in with <OIDC_NAME>" button when OIDC_ISSUER, OIDC_CLIENT_ID and
OIDC_CLIENT_SECRET are set. It runs the authorization code flow with PKCE; the tokens come
straight from the provider's token endpoint over HTTPS, so the ID token's claims are checked
(issuer, audience, expiry, nonce) without verifying its signature (OpenID Connect Core 3.1.3.7).
The username and groups come from the userinfo endpoint.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

COOKIE = "rmm_oidc"  # binds a sign-in to the browser that started it
_TIMEOUT = 15
_PENDING_SECONDS = 600

_pending: dict[str, dict] = {}  # state -> {verifier, nonce, next, at}
_lock = threading.Lock()
_discovery: tuple[float, dict] = (0.0, {})


def enabled() -> bool:
    return bool(config.OIDC_ISSUER and config.OIDC_CLIENT_ID and config.OIDC_CLIENT_SECRET)


def _get_json(url: str, headers: dict | None = None, data: bytes | None = None) -> dict:
    req = urllib.request.Request(url, data=data, headers={"Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.loads(r.read())


def _provider() -> dict:
    """The provider's endpoints, from its discovery document (cached for an hour)."""
    global _discovery
    when, doc = _discovery
    if time.time() - when > 3600:
        doc = _get_json(config.OIDC_ISSUER.rstrip("/") + "/.well-known/openid-configuration")
        _discovery = (time.time(), doc)
    return doc


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def start(redirect_uri: str, next_path: str = "/") -> tuple[str, str]:
    """(URL to send the browser to, state for the COOKIE)."""
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    now = time.time()
    with _lock:
        for k in [k for k, v in _pending.items() if now - v["at"] > _PENDING_SECONDS]:
            del _pending[k]
        _pending[state] = {"verifier": verifier, "nonce": nonce, "next": next_path, "at": now}
    query = urllib.parse.urlencode({
        "response_type": "code", "client_id": config.OIDC_CLIENT_ID, "redirect_uri": redirect_uri,
        "scope": config.OIDC_SCOPES, "state": state, "nonce": nonce,
        "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256",
    })
    return f"{_provider()['authorization_endpoint']}?{query}", state


def _claims(id_token: str) -> dict:
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as e:
        raise ValueError("The sign-in answer wasn't readable") from e


def finish(code: str, state: str, cookie_state: str | None, redirect_uri: str) -> tuple[str, set[str], str]:
    """Trades the code for the user: (username, groups, where to go next). ValueError if anything is off."""
    with _lock:
        pending = _pending.pop(state, None)
    if not pending or not cookie_state or not secrets.compare_digest(state, cookie_state):
        raise ValueError("That sign-in expired or was started in another browser; try again")
    if time.time() - pending["at"] > _PENDING_SECONDS:
        raise ValueError("That sign-in took too long; try again")
    provider = _provider()
    basic = base64.b64encode(f"{urllib.parse.quote(config.OIDC_CLIENT_ID, safe='')}:"
                             f"{urllib.parse.quote(config.OIDC_CLIENT_SECRET, safe='')}".encode()).decode()
    try:
        tokens = _get_json(provider["token_endpoint"], {"Authorization": f"Basic {basic}",
                                                        "Content-Type": "application/x-www-form-urlencoded"},
                           urllib.parse.urlencode({"grant_type": "authorization_code", "code": code,
                                                   "redirect_uri": redirect_uri,
                                                   "code_verifier": pending["verifier"]}).encode())
    except urllib.error.HTTPError as e:
        raise ValueError(f"{config.OIDC_NAME} turned down the sign-in ({e.code}: {e.read()[:200].decode(errors='replace')})")
    claims = _claims(tokens.get("id_token", ""))
    aud = claims.get("aud")
    if claims.get("iss", "").rstrip("/") != config.OIDC_ISSUER.rstrip("/"):
        raise ValueError("The sign-in came from the wrong issuer")
    if config.OIDC_CLIENT_ID not in (aud if isinstance(aud, list) else [aud]):
        raise ValueError("The sign-in was meant for another app")
    if claims.get("exp", 0) < time.time() - 60:
        raise ValueError("The sign-in expired; try again")
    if claims.get("nonce") != pending["nonce"]:
        raise ValueError("The sign-in didn't match the one started here; try again")
    info = _get_json(provider["userinfo_endpoint"], {"Authorization": f"Bearer {tokens['access_token']}"})
    if info.get("sub") != claims.get("sub"):
        raise ValueError("The sign-in answer didn't match its user")
    username = info.get(config.OIDC_USERNAME_CLAIM) or claims.get(config.OIDC_USERNAME_CLAIM)
    if not username:
        raise ValueError(f"{config.OIDC_NAME} didn't send a {config.OIDC_USERNAME_CLAIM}; add the profile scope to the client")
    groups = info.get("groups") or claims.get("groups") or []
    return str(username), {str(g) for g in groups}, pending["next"]
