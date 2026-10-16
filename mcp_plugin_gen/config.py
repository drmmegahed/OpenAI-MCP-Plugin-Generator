"""Configuration model: YAML file + ${ENV_VAR} substitution + CLI overrides.

Secrets (OAuth client secret, SSO credentials, tokens) are read at runtime only and
are NEVER written into the generated package. Reference them as ${ENV} in YAML.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

import yaml

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _subst(value: Any) -> Any:
    if isinstance(value, str):
        def rep(m):
            name, default = m.group(1), m.group(2)
            return os.environ.get(name, default if default is not None else "")
        return _ENV_RE.sub(rep, value)
    if isinstance(value, list):
        return [_subst(v) for v in value]
    if isinstance(value, dict):
        return {k: _subst(v) for k, v in value.items()}
    return value


@dataclass
class McpConfig:
    url: str
    transport: str = "streamable-http"          # OpenAI public directory requires streamable-http
    timeout: float = 30.0
    verify_tls: bool = True
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class OAuthConfig:
    """OAuth 2.1 settings. `mode`:
       auth_code   – authorization-code + PKCE (user login via browser / SSO auto-login)
       client_creds – client_credentials grant (service identity; discovery only)
       token        – a pre-obtained bearer token (OAUTH_TOKEN env)
       none         – unauthenticated discovery (public tools only)
    """
    mode: str = "auth_code"
    client_id: str = ""
    client_secret: str = ""                      # runtime only, from ${ENV}
    issuer: str = ""                             # if empty: discovered from MCP 401 / .well-known
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    userinfo_endpoint: str = ""
    scopes: list[str] = field(default_factory=lambda: ["openid", "email", "profile"])
    redirect_uri: str = "http://127.0.0.1:8765/callback"
    callback_bind: str = ""                      # host to listen on for the callback; auto if empty
    token_endpoint_auth_method: str = "client_secret_basic"   # mcp.json: none | client_secret_basic | client_secret_post | private_key_jwt
    client_mode: str = "provided"                # mcp.json: provided | dcr | cimd
    oidc_enabled: bool = True                    # mcp.json auth.oidcEnabled; false → plain OAuth, no oidc* fields, no UserInfo
    exact_scopes: bool = False                   # mcp.json: baseScopes = exactly `scopes` (minus openid) and defaultScopes: [] →
                                                 # OpenAI sends ONLY those scopes (needs oidc_enabled: false)
    declare_in_mcp_json: str = "auto"            # auto (default) | true | false — write extensions.com.openai.auth into mcp.json?
                                                 # FIELD RESULT: any `client.mode: provided` block (secret OR none) makes ChatGPT DROP
                                                 # the MCP server on a personal upload / desktop install (no tools, no login). The
                                                 # provided client only works for a connection made in the developer portal.
                                                 # auto  = false for target builder (Plugin-Builder discovery layout: ChatGPT uses its
                                                 #         own CIMD client https://chatgpt.com/oauth/codex/client.json, SSO opens at
                                                 #         install on web+desktop; Keycloak must accept that client), true otherwise.
    token: str = ""                              # for mode=token
    # mode=api_key: ChatGPT sends a credential supplied at connection setup in a HEADER on every MCP call (no redirect).
    # VERIFIED 2026-10-13: a personal upload with ANY explicit auth block (oauth provided / api_key) installs WITHOUT the MCP
    # server. Only the bare type+url layout (discovery → ChatGPT's CIMD client) attaches the server and opens SSO at install.
    api_key_header_scheme: str = "basic"         # basic → Authorization: Basic base64(client_id:client_secret)
                                                 # bearer → Authorization: Bearer <api_key> | custom_header → <api_key_header_name>: <api_key>
    api_key_header_name: str = ""                # only for custom_header (e.g. X-API-Key); `Host` not allowed
    api_key: str = ""                            # the credential used for the build (bearer/custom_header); basic uses client_id:client_secret
    audience: str = ""
    resource: str = ""                           # mcp.json auth.resource (RFC 8707 resource indicator); empty → mcp.url
    extra_auth_params: dict[str, str] = field(default_factory=dict)


@dataclass
class SsoConfig:
    """Optional headless auto-login for an SSO page (Playwright). Each step fills a field by
    CSS/text selector and (optionally) clicks a button. Values come from ${ENV}. If `enabled`
    is false, the authorization URL is printed/opened for the user to log in manually."""
    enabled: bool = False
    headless: bool = True
    browser: str = "chromium"
    slow_mo_ms: int = 0
    step_timeout_ms: int = 30000
    steps: list[dict[str, Any]] = field(default_factory=list)
    # each step: {fill: "<selector>", value: "${SSO_MOBILE}", submit: "<selector>", wait_for: "<selector>"}
    screenshot_dir: str = ""                     # save a screenshot per step for debugging


@dataclass
class ListingConfig:
    name: str = ""                               # kebab-case plugin id; derived from display_name if empty
    display_name: str = ""
    developer_name: str = ""
    author_url: str = ""
    version: str = "1.0.0"
    description: str = ""
    short_description: str = ""
    long_description: str = ""
    category: str = "Other"
    capabilities: list[str] = field(default_factory=list)   # derived from tool annotations if empty
    default_prompt: Any = ""                     # string or list of strings
    release_notes: str = ""                      # extensions.com.openai.publication.release_notes
    keywords: list[str] = field(default_factory=list)
    homepage: str = ""
    website_url: str = ""                        # interface.websiteURL (falls back to homepage / author_url)
    license: str = "Proprietary"
    privacy_policy_url: str = ""
    terms_of_service_url: str = ""
    support_url: str = ""
    support_email: str = ""
    app_id: str = ""                             # asdk_app_… registered connection id (optional)
    language_note: str = ""                      # e.g. "Reply in Arabic or English, matching the user."
    domain_notes: list[str] = field(default_factory=list)   # extra bullet rules for the skill
    icon_text: str = ""                          # monogram for generated placeholder icon
    brand_color: str = "#0B2A4A"
    accent_color: str = "#C9A24D"


@dataclass
class ReviewConfig:
    """extensions.com.openai.review — required for MCP review: 5 positive + 3 negative test cases and a demo
    recording URL. Cases are auto-generated from discovered tools when not supplied; edit them in config."""
    demo_recording_url: str = ""
    commerce: bool = False
    commerce_description: str = "This plugin does not sell products or process payments."
    positive: list[dict[str, Any]] = field(default_factory=list)   # {description, prompt, tools_triggered:[..], expected_behavior}
    negative: list[dict[str, Any]] = field(default_factory=list)   # {description, prompt}


@dataclass
class AssetsConfig:
    icon: str = ""                               # paths to real brand files; placeholders generated if empty
    logo: str = ""
    screenshots: list[str] = field(default_factory=list)


@dataclass
class OutputConfig:
    dir: str = "./dist"
    zip: bool = True
    include_scripts: bool = True                 # bundle discover_tools.py into the package
    emit_codex_compat: bool = True               # .codex-plugin/plugin.json copy
    emit_mcp_json: bool = True
    emit_app_json: bool = False                  # (derived from target; kept for backwards compat)
    target: str = "builder"                      # builder   → developer portal, same layout as ChatGPT's Plugin Builder:
                                                 #             plugin.json + mcp.json (+auth) + tools.json + skills + assets/logo.png
                                                 # portal    → developer portal, extended layout (.codex-plugin compat, .mcp.json, docs)
                                                 # workspace → admin console upload: .app.json (listing.app_id = asdk_app_…)


@dataclass
class Config:
    mcp: McpConfig
    oauth: OAuthConfig = field(default_factory=OAuthConfig)
    sso: SsoConfig = field(default_factory=SsoConfig)
    listing: ListingConfig = field(default_factory=ListingConfig)
    assets: AssetsConfig = field(default_factory=AssetsConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)

    @staticmethod
    def load(path: str | Path, overrides: Optional[dict[str, Any]] = None) -> "Config":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        # `credentials:` block → env vars (UPPERCASE keys) so ${...} references resolve even
        # when the CLI is invoked directly, without run.sh. Existing env vars are not overwritten
        # unless the YAML value is non-empty (YAML wins).
        for k, v in (raw.pop("credentials", None) or {}).items():
            if v not in (None, ""):
                os.environ[str(k).upper()] = str(v)
        raw = _subst(raw)
        for k, v in (overrides or {}).items():       # dotted overrides: oauth.client_id=...
            cur = raw
            parts = k.split(".")
            for p in parts[:-1]:
                cur = cur.setdefault(p, {})
            cur[parts[-1]] = v
        return Config(
            mcp=McpConfig(**raw.get("mcp", {})),
            oauth=OAuthConfig(**raw.get("oauth", {})),
            sso=SsoConfig(**raw.get("sso", {})),
            listing=ListingConfig(**raw.get("listing", {})),
            assets=AssetsConfig(**raw.get("assets", {})),
            output=OutputConfig(**raw.get("output", {})),
            review=ReviewConfig(**raw.get("review", {})),
        )

    def redacted(self) -> dict:
        d = asdict(self)
        d["oauth"]["client_secret"] = "***" if self.oauth.client_secret else ""
        d["oauth"]["token"] = "***" if self.oauth.token else ""
        for s in d["sso"]["steps"]:
            if "value" in s:
                s["value"] = "***"
        return d
