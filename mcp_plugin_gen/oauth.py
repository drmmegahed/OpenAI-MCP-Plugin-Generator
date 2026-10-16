"""OAuth 2.1 client: metadata discovery (RFC 8414 / RFC 9728 / OIDC), PKCE authorization-code
flow with a local callback server, client_credentials, and a review check of the UserInfo
endpoint (OpenAI requires `email` + `email_verified: true`)."""
from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Optional
from urllib.parse import urlencode, urlparse, parse_qs
import json

import httpx

from .config import OAuthConfig, McpConfig


@dataclass
class OAuthMetadata:
    issuer: str = ""
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    userinfo_endpoint: str = ""
    registration_endpoint: str = ""
    scopes_supported: list[str] = field(default_factory=list)
    code_challenge_methods_supported: list[str] = field(default_factory=list)
    grant_types_supported: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class TokenSet:
    access_token: str
    token_type: str = "Bearer"
    refresh_token: str = ""
    expires_in: int = 0
    scope: str = ""
    id_token: str = ""


class OAuthError(RuntimeError):
    pass


def _client(mcp: McpConfig) -> httpx.Client:
    return httpx.Client(timeout=mcp.timeout, verify=mcp.verify_tls, follow_redirects=True)


def discover(mcp: McpConfig, oauth: OAuthConfig, log: Callable[[str], None] = print) -> OAuthMetadata:
    """Find the authorization server: explicit issuer → MCP 401 WWW-Authenticate / protected-resource
    metadata → .well-known on the MCP host."""
    md = OAuthMetadata()
    if oauth.authorization_endpoint and oauth.token_endpoint:
        md.authorization_endpoint = oauth.authorization_endpoint
        md.token_endpoint = oauth.token_endpoint
        md.userinfo_endpoint = oauth.userinfo_endpoint
        md.issuer = oauth.issuer
        return md

    candidates: list[str] = []
    with _client(mcp) as c:
        if oauth.issuer:
            candidates.append(oauth.issuer.rstrip("/"))
        else:
            # RFC 9728: protected resource metadata on the MCP host
            u = urlparse(mcp.url)
            origin = f"{u.scheme}://{u.netloc}"
            for prm in (f"{origin}/.well-known/oauth-protected-resource{u.path}",
                        f"{origin}/.well-known/oauth-protected-resource"):
                try:
                    r = c.get(prm)
                    if r.status_code == 200:
                        for iss in r.json().get("authorization_servers", []):
                            candidates.append(iss.rstrip("/"))
                        if candidates:
                            log(f"[oauth] protected-resource metadata → {candidates}")
                            break
                except Exception:
                    pass
            # Probe the MCP endpoint for a 401 hint
            if not candidates:
                try:
                    r = c.post(mcp.url, json={"jsonrpc": "2.0", "id": 0, "method": "ping"},
                               headers={"Accept": "application/json, text/event-stream"})
                    www = r.headers.get("WWW-Authenticate", "")
                    for part in www.split(","):
                        part = part.strip()
                        if part.startswith("resource_metadata=") or "realm=" in part or "authorization_uri=" in part:
                            val = part.split("=", 1)[1].strip('"')
                            if val.startswith("http"):
                                candidates.append(val)
                except Exception:
                    pass
            candidates.append(origin)

        import time as _t
        tried: list[str] = []
        for attempt in range(3):                       # brief retry: IdP restarts / transient 5xx
            for base in candidates:
                for wk in (f"{base}/.well-known/oauth-authorization-server",
                           f"{base}/.well-known/openid-configuration"):
                    try:
                        r = c.get(wk)
                        if r.status_code == 200 and "token_endpoint" in r.text:
                            j = r.json()
                            md = OAuthMetadata(
                                issuer=j.get("issuer", base),
                                authorization_endpoint=j.get("authorization_endpoint", ""),
                                token_endpoint=j.get("token_endpoint", ""),
                                userinfo_endpoint=j.get("userinfo_endpoint", ""),
                                registration_endpoint=j.get("registration_endpoint", ""),
                                scopes_supported=j.get("scopes_supported", []),
                                code_challenge_methods_supported=j.get("code_challenge_methods_supported", []),
                                grant_types_supported=j.get("grant_types_supported", []),
                                raw=j,
                            )
                            log(f"[oauth] discovered metadata at {wk}")
                            return md
                        tried.append(f"{wk} → HTTP {r.status_code}")
                    except Exception as e:  # noqa: BLE001
                        tried.append(f"{wk} → {type(e).__name__}: {e}")
            if attempt < 2:
                log(f"[oauth] metadata not available yet ({tried[-1]}); retrying in 3s")
                _t.sleep(3)
    log("[oauth] discovery attempts:\n  " + "\n  ".join(tried))
    raise OAuthError(
        "Could not discover OAuth metadata — the identity provider's .well-known document is not reachable "
        "(see attempts above: 5xx/timeout = IdP down, 404 = realm/issuer path changed). Set oauth.issuer or "
        "oauth.authorization_endpoint/token_endpoint explicitly if discovery is not published.")


def negotiate_scopes(requested: list[str], md: OAuthMetadata, log: Callable[[str], None] = print) -> list[str]:
    """Keep only scopes the server advertises (when it advertises any). `openid` is always kept."""
    if not md.scopes_supported:
        return requested
    keep = [sc for sc in requested if sc in md.scopes_supported]
    dropped = [sc for sc in requested if sc not in keep]
    if dropped:
        log(f"[oauth] dropping scopes not advertised by the server: {dropped} (supported: {md.scopes_supported})")
    return keep


SCOPE_FALLBACKS = [["openid", "email", "profile"], ["openid", "email"], ["openid"]]


def probe_scopes(requested: list[str], md: OAuthMetadata, oauth: OAuthConfig, mcp: McpConfig,
                 log: Callable[[str], None] = print) -> list[str]:
    """Find the largest subset of `requested` the client may ask for — WITHOUT logging in.
    For each scope, GET the authorize endpoint with `openid <scope>`; a 302 back to the redirect_uri
    carrying error=invalid_scope means rejected, anything else (login page / IdP redirect) means accepted.
    One HTTP request per scope, no browser."""
    extras = [sc for sc in requested if sc != "openid"]
    if not extras or not md.authorization_endpoint:
        return requested
    with_oidc = "openid" in requested
    accepted, rejected = (["openid"] if with_oidc else []), []
    with _client(mcp) as c:
        c.follow_redirects = False
        for sc in extras:
            url, _, _ = build_authorization_url(md, OAuthConfig(**{**oauth.__dict__, "scopes": (["openid"] if with_oidc else []) + [sc]}))
            try:
                r = c.get(url)
                loc = r.headers.get("location", "")
                bad = r.status_code in (301, 302, 303, 307) and loc.startswith(oauth.redirect_uri) and "error=" in loc
            except Exception as e:  # noqa: BLE001
                log(f"[oauth] probe {sc}: request failed ({e}); assuming accepted")
                bad = False
            (rejected if bad else accepted).append(sc)
    if rejected:
        log(f"[oauth] scope probe: accepted {accepted}; rejected by client '{oauth.client_id}': {rejected}")
    else:
        log(f"[oauth] scope probe: all requested scopes accepted {accepted}")
    return accepted


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_authorization_url(md: OAuthMetadata, oauth: OAuthConfig) -> tuple[str, str, str]:
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": oauth.client_id,
        "redirect_uri": oauth.redirect_uri,
        **({"scope": " ".join(oauth.scopes)} if oauth.scopes else {}),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if oauth.audience:
        params["audience"] = oauth.audience
    params.update(oauth.extra_auth_params)
    return f"{md.authorization_endpoint}?{urlencode(params)}", verifier, state


class _CallbackServer:
    """Tiny local HTTP server that captures ?code=&state= on the redirect URI.
    Binds to `bind_host` (default: 127.0.0.1 for loopback redirect hosts, else 0.0.0.0) so a redirect
    URI registered under another hostname still works. If binding fails the server is disabled and the
    code must arrive via the SSO opener (browser interception) instead."""

    def __init__(self, redirect_uri: str, bind_host: str = "", log: Callable[[str], None] = print):
        u = urlparse(redirect_uri)
        self.path = u.path or "/"
        self.result: dict = {}
        self._ev = threading.Event()
        self.httpd = None
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def do_GET(self):
                p = urlparse(self.path)
                if p.path != outer.path:
                    self.send_response(404); self.end_headers(); return
                outer.result = {k: v[0] for k, v in parse_qs(p.query).items()}
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<html><body style='font-family:sans-serif'><h3>Login complete.</h3>"
                                 b"You can close this window and return to mcp-plugin-gen.</body></html>")
                outer._ev.set()

        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        candidates = [bind_host] if bind_host else (["127.0.0.1"] if host in ("127.0.0.1", "localhost", "::1") else ["0.0.0.0", "127.0.0.1"])
        err = None
        HTTPServer.allow_reuse_address = True
        for h in candidates:
            try:
                self.httpd = HTTPServer((h, port), H)
                log(f"[oauth] callback listener on {h}:{port}{self.path} (redirect_uri={redirect_uri})")
                break
            except OSError as e:
                err = e
        if self.httpd is None:
            log(f"[oauth] WARNING: cannot listen for the callback ({err}); relying on browser interception")
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True) if self.httpd else None

    def start(self):
        if self.thread:
            self.thread.start()

    def set_result(self, res: dict):
        self.result = res
        self._ev.set()

    def wait(self, timeout: float) -> dict:
        self._ev.wait(timeout)
        self.close()
        return self.result

    def close(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None


def authorization_code_flow(md: OAuthMetadata, oauth: OAuthConfig, mcp: McpConfig,
                            open_browser: Optional[Callable[[str], Optional[dict]]] = None,
                            timeout: float = 600, log: Callable[[str], None] = print) -> TokenSet:
    """Run auth-code + PKCE with automatic scope negotiation: scopes are filtered against the server's
    `scopes_supported`, and an `invalid_scope` error triggers a retry with progressively smaller
    standard scope sets. The final scope list is written back to `oauth.scopes` so mcp.json matches."""
    if md.code_challenge_methods_supported and "S256" not in md.code_challenge_methods_supported:
        log("[oauth] WARNING: server does not advertise PKCE S256 — OpenAI requires it.")
    wanted = negotiate_scopes(oauth.scopes, md, log)
    probed = probe_scopes(wanted, md, oauth, mcp, log)
    fallbacks = [f for f in SCOPE_FALLBACKS if f != probed] if "openid" in wanted else [[]]
    attempts = [probed] + [f for f in fallbacks if f != probed]
    last_err = None
    for i, scopes in enumerate(attempts):
        oauth.scopes = scopes
        log(f"[oauth] authorization request with scopes: {' '.join(scopes) or '(none — plain OAuth)'}")
        try:
            return _auth_code_once(md, oauth, mcp, open_browser, timeout, log)
        except OAuthError as e:
            last_err = e
            if "invalid_scope" in str(e) and i + 1 < len(attempts):
                log(f"[oauth] server rejected scopes ({e}); retrying with a smaller set")
                continue
            raise
    raise last_err  # pragma: no cover


def _auth_code_once(md, oauth, mcp, open_browser, timeout, log) -> TokenSet:
    url, verifier, state = build_authorization_url(md, oauth)
    srv = _CallbackServer(oauth.redirect_uri, oauth.callback_bind, log)
    if srv.httpd is None and not open_browser:
        raise OAuthError("No callback listener and no SSO driver: set oauth.redirect_uri to "
                         "http://127.0.0.1:<port>/callback or oauth.callback_bind, or enable sso.")
    srv.start()
    if open_browser:
        captured = open_browser(url)
        if captured:
            srv.set_result(captured)
            log("[oauth] redirect captured from the browser" + (" (code present)" if "code" in captured else f" (error: {captured.get('error')})"))
    else:
        log(f"[oauth] Open this URL to sign in:\n{url}\n")
        try:
            webbrowser.open(url)
        except Exception:
            pass
    res = srv.wait(timeout if not open_browser else 30)
    if not res:
        raise OAuthError("No authorization code received (no redirect to redirect_uri). "
                         "Check the SSO steps / screenshots and that redirect_uri is registered for the client.")
    if "error" in res:
        raise OAuthError(f"Authorization error: {res.get('error')} {res.get('error_description', '')}")
    if res.get("state") != state:
        raise OAuthError("State mismatch on redirect (possible CSRF or wrong callback).")
    return exchange_code(md, oauth, mcp, res["code"], verifier)


def exchange_code(md: OAuthMetadata, oauth: OAuthConfig, mcp: McpConfig, code: str, verifier: str) -> TokenSet:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": oauth.redirect_uri,
        "client_id": oauth.client_id,
        "code_verifier": verifier,
    }
    auth = (oauth.client_id, oauth.client_secret) if oauth.client_secret else None
    with _client(mcp) as c:
        r = c.post(md.token_endpoint, data=data, auth=auth,
                   headers={"Accept": "application/json"})
    if r.status_code != 200:
        raise OAuthError(f"Token exchange failed {r.status_code}: {r.text[:300]}")
    return _tokenset(r.json())


def client_credentials(md: OAuthMetadata, oauth: OAuthConfig, mcp: McpConfig, log: Callable[[str], None] = print) -> TokenSet:
    oauth.scopes = [sc for sc in negotiate_scopes(oauth.scopes, md, log) if sc != "openid"] or oauth.scopes
    data = {"grant_type": "client_credentials", "scope": " ".join(oauth.scopes)}
    if oauth.audience:
        data["audience"] = oauth.audience
    with _client(mcp) as c:
        r = c.post(md.token_endpoint, data=data, auth=(oauth.client_id, oauth.client_secret),
                   headers={"Accept": "application/json"})
    if r.status_code != 200:
        raise OAuthError(f"client_credentials failed {r.status_code}: {r.text[:300]}")
    return _tokenset(r.json())


def _tokenset(j: dict) -> TokenSet:
    if "access_token" not in j:
        raise OAuthError(f"No access_token in token response: {j}")
    return TokenSet(access_token=j["access_token"], token_type=j.get("token_type", "Bearer"),
                    refresh_token=j.get("refresh_token", ""), expires_in=int(j.get("expires_in", 0) or 0),
                    scope=j.get("scope", ""), id_token=j.get("id_token", ""))


def check_userinfo(md: OAuthMetadata, token: TokenSet, mcp: McpConfig) -> dict:
    """OpenAI review requirement: UserInfo must return email + email_verified:true,
    and the server must advertise openid + email scopes."""
    out = {"userinfo_endpoint": md.userinfo_endpoint, "ok": False, "issues": []}
    if "openid" not in (token.scope or "").split():
        out["issues"].append("skipped: token has no openid scope (OIDC disabled by config)")
        out["skipped"] = True
        return out
    if not md.userinfo_endpoint:
        out["issues"].append("no userinfo_endpoint in metadata")
        return out
    with _client(mcp) as c:
        r = c.get(md.userinfo_endpoint, headers={"Authorization": f"Bearer {token.access_token}"})
    if r.status_code != 200:
        out["issues"].append(f"userinfo returned {r.status_code}")
        return out
    j = r.json()
    if not j.get("email"):
        out["issues"].append("userinfo has no `email`")
    if j.get("email_verified") is not True:
        out["issues"].append("userinfo `email_verified` is not true")
    for s in ("openid", "email"):
        if md.scopes_supported and s not in md.scopes_supported:
            out["issues"].append(f"scope `{s}` not in scopes_supported")
    out["ok"] = not out["issues"]
    out["email"] = j.get("email", "")
    return out


# ---------------------------------------------------------------------------------------------------
# Runtime preflight — reproduce the exact authorize requests ChatGPT's runtimes send, without logging in
# ---------------------------------------------------------------------------------------------------
# With a Plugin-Builder package (mcp.json = type + url) ChatGPT discovers OAuth from the server and uses:
#   * ChatGPT web      → provided client (portal form) + redirect https://chatgpt.com/connector_platform_oauth_redirect,
#                        or the CIMD client https://chatgpt.com/oauth/chatgpt/client.json
#   * ChatGPT desktop / Codex → CIMD public client https://chatgpt.com/oauth/codex/client.json (PKCE, no secret),
#                        redirect http://127.0.0.1:<random port>/callback (RFC 8252 loopback)
# and it requests scopes_supported from the MCP server's protected-resource metadata — falling back to the
# AUTHORIZATION SERVER's scopes_supported (i.e. every realm scope) when the resource does not publish any.
CIMD_CODEX = "https://chatgpt.com/oauth/codex/client.json"
CIMD_CHATGPT = "https://chatgpt.com/oauth/chatgpt/client.json"
WEB_REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"
LOOPBACK_REDIRECT = "http://127.0.0.1:57862/callback"


def protected_resource_metadata(mcp: McpConfig) -> tuple[str, dict] | tuple[None, None] | tuple[None, str]:
    """(url, doc) when published; (None, None) when the server answers but has none; (None, '<error>') when unreachable."""
    u = urlparse(mcp.url)
    origin = f"{u.scheme}://{u.netloc}"
    last_err = None
    with _client(mcp) as c:
        for prm in (f"{origin}/.well-known/oauth-protected-resource{u.path}",
                    f"{origin}/.well-known/oauth-protected-resource"):
            try:
                r = c.get(prm)
                last_err = None
                if r.status_code == 200 and "authorization_servers" in r.text:
                    return prm, r.json()
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
    return None, last_err


def _authorize_probe(c: httpx.Client, md: OAuthMetadata, client_id: str, redirect_uri: str, scope: str) -> str:
    """GET the authorize endpoint as the runtime would; classify the IdP's answer (no login)."""
    _, challenge = _pkce()
    q = urlencode({"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "scope": scope,
                   "state": secrets.token_urlsafe(8), "code_challenge": challenge, "code_challenge_method": "S256"})
    try:
        r = c.get(f"{md.authorization_endpoint}?{q}")
    except Exception as e:  # noqa: BLE001
        return f"unreachable ({e})"
    loc = r.headers.get("location", "")
    body = r.text.lower()
    if r.status_code in (301, 302, 303, 307):
        if loc.startswith(redirect_uri.split("?")[0]) and "error=" in loc:
            err = parse_qs(urlparse(loc).query).get("error", ["?"])[0]
            return f"REJECTED {err}"
        return "OK (login page)"
    if r.status_code == 200:
        return "OK (login page)"
    for needle, label in (("client not found", "REJECTED client_not_found"),
                          ("invalid parameter: redirect_uri", "REJECTED invalid_redirect_uri"),
                          ("invalid_redirect_uri", "REJECTED invalid_redirect_uri"),
                          ("invalid scope", "REJECTED invalid_scope"),
                          ("invalid_request", "REJECTED invalid_request")):
        if needle in body:
            return label
    return f"HTTP {r.status_code}"


def runtime_preflight(mcp: McpConfig, oauth: OAuthConfig, md: OAuthMetadata,
                      log: Callable[[str], None] = print, provided_only: bool = False) -> list[str]:
    """Check that the IdP will accept what ChatGPT web / desktop / Codex actually send. Returns problems.
    provided_only=True (mcp.json declares client mode=provided): only the provided-client request must succeed;
    the CIMD/discovery checks are reported as notes, since ChatGPT will not use them for this package."""
    problems: list[str] = []
    notes: list[str] = []
    sink = notes if provided_only else problems
    wanted = [sc for sc in oauth.scopes if sc != "openid"]
    prm_url, prm = protected_resource_metadata(mcp)
    if prm_url is None and isinstance(prm, str):
        log(f"[preflight] skipped — {mcp.url} not reachable from this machine ({prm}); run the build where the server is reachable")
        return ["preflight skipped: MCP server not reachable from this machine — rerun where it is"]
    if prm is None:
        sink.append("MCP server publishes no /.well-known/oauth-protected-resource — ChatGPT cannot discover the "
                    "authorization server from mcp.json (type+url). Publish it (RFC 9728).")
        requested = md.scopes_supported
    else:
        rs = prm.get("scopes_supported")
        if not rs:
            requested = md.scopes_supported
            sink.append(f"{prm_url} has no `scopes_supported` → ChatGPT requests EVERY scope of the authorization "
                            f"server ({' '.join(md.scopes_supported) or 'none'}). Add \"scopes_supported\": "
                            f"{json.dumps(wanted)} to that document (and scope=\"{' '.join(wanted)}\" to the 401 WWW-Authenticate).")
        else:
            requested = list(rs)
            if set(rs) != set(wanted):
                sink.append(f"{prm_url} scopes_supported = {rs}; config oauth.scopes = {wanted} — ChatGPT requests "
                            f"the server's list, align the two.")
            else:
                log(f"[preflight] protected-resource metadata OK: ChatGPT will request scope '{' '.join(rs)}'")
    if not oauth.oidc_enabled and "openid" in (requested or []):
        sink.append("OIDC is still ON: `openid` is in the scope ChatGPT will request (it comes from the server-side "
                        "scopes_supported fallback). OIDC is off only when the protected-resource document lists no "
                        "`openid` — i.e. scopes_supported = " + json.dumps(wanted) + ".")
    if not md.authorization_endpoint:
        return problems + ["no authorization_endpoint discovered"]
    scope = " ".join(requested) or " ".join(wanted)
    with _client(mcp) as c:
        c.follow_redirects = False
        checks = [
            ("ChatGPT web  (provided client, portal form)", oauth.client_id, WEB_REDIRECT, " ".join(wanted), True),
            ("ChatGPT web  (CIMD client)", CIMD_CHATGPT, WEB_REDIRECT, scope, False),
            ("Desktop/Codex (CIMD client, loopback)", CIMD_CODEX, LOOPBACK_REDIRECT, scope, False),
        ]
        for label, cid, ruri, sc, is_provided in checks:
            if not cid:
                continue
            res = _authorize_probe(c, md, cid, ruri, sc)
            tag = "" if (is_provided or not provided_only) else "  [note: not used by this package]"
            log(f"[preflight] {label}: client_id={cid} redirect={ruri} scope='{sc}' → {res}{tag}")
            target = problems if (is_provided or not provided_only) else notes
            scope = sc
            if res.startswith("REJECTED") or res.startswith("HTTP 5"):
                err = res.split(" ", 1)[1] if res.startswith("REJECTED") else "server_error"
                if err == "client_not_found":
                    target.append(f"{label}: Keycloak has no client with Client ID '{cid}' — create it (public, PKCE S256, "
                                  f"no secret; scripts/keycloak_chatgpt_runtime.sh does this).")
                elif err == "invalid_redirect_uri":
                    target.append(f"{label}: redirect URI '{ruri}' not registered on client '{cid}' "
                                  f"(loopback needs the wildcard http://127.0.0.1* and http://localhost*).")
                elif err == "invalid_scope":
                    target.append(f"{label}: client '{cid}' rejects scope '{sc}' — assign exactly these client "
                                  f"scopes to it (default), or fix scopes_supported on the MCP server.")
                elif err == "server_error":
                    target.append(f"{label}: Keycloak answered {res} for client '{cid}' — unhandled exception (see the "
                                  f"Keycloak log for the Error id; typical: CIMD document fetch without egress, or a broken "
                                  f"error.ftl in the login theme).")
                else:
                    target.append(f"{label}: authorize rejected ({err}) for client '{cid}'")
    for n in notes:
        log(f"[preflight] note (discovery layout only): {n}")
    return problems
