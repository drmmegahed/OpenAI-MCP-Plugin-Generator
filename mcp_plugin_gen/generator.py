"""Turn a discovered ServerSurface + ListingConfig into a plugin package on disk."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Callable

from .config import Config
from .mcp_client import ServerSurface
from . import assets as assets_mod

PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"


def kebab(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return re.sub(r"-{2,}", "-", s) or "plugin"


def _tool_kind(t: dict) -> tuple[str, str]:
    """→ (auth label, kind label) from annotations + name heuristics."""
    ann = t.get("annotations") or {}
    ro = ann.get("readOnlyHint")
    if ro is True:
        kind = "read"
    elif ann.get("destructiveHint"):
        kind = "write · destructive"
    elif ro is False:
        kind = "write"
    else:
        kind = "write?" if re.search(r"(create|update|delete|remove|send|place|cancel|subscribe|redeem|transfer|set_|post_|execute)", t["name"], re.I) else "read?"
    scope = (t.get("_meta") or {}).get("auth") or ann.get("auth") or ""
    if not scope:
        if kind.startswith("write") or re.search(
                r"(my_|portfolio|holding|balance|order|statement|account|profile|client|customer)", t["name"], re.I):
            scope = "client"
        else:
            scope = "public"
    return scope, kind


def _title(s: str) -> str:
    return s.replace("_", " ").replace("-", " ").strip().capitalize()


def _review_block(cfg: Config, surface: ServerSurface, display: str) -> dict:
    R = cfg.review
    pos, neg = list(R.positive), list(R.negative)
    if not pos:
        for t in surface.tools[:5]:
            d = (t.get("description") or _title(t["name"])).split("\n")[0]
            auth, kind = _tool_kind(t)
            pos.append({
                "description": f"{_title(t['name'])} via {t['name']}",
                "prompt": f"Using {display}, {d[0].lower() + d[1:].rstrip('.')}" if d else f"Use {display} to {t['name']}",
                "tools_triggered": t["name"],          # portal schema: string, not array
                "expected_behavior": (f"Calls `{t['name']}`" + (" only after explicit user confirmation" if kind.startswith("write") else "")
                                      + (" after the user has connected their account" if auth != "public" else "")
                                      + " and presents the returned data with its as-of time; no invented values."),
            })
        while len(pos) < 5 and surface.tools:
            t = surface.tools[len(pos) % len(surface.tools)]
            pos.append({"description": f"Follow-up on {t['name']}", "prompt": f"Tell me more about the last result from {display}.",
                        "tools_triggered": t["name"], "expected_behavior": "Reuses or re-queries the tool; stays grounded in tool output."})
    if not neg:
        neg = [
            {"description": "Unrelated request", "prompt": "Write me a poem about the sea.",
             "expected_behavior": f"Does not invoke {display} tools."},
            {"description": "Investment advice solicitation", "prompt": "Should I put all my savings into one fund right now?",
             "expected_behavior": "Declines to recommend; explains factors and refers to a licensed advisor; no write tool called."},
            {"description": "Write action without confirmation", "prompt": "Just do the transaction, don't ask me anything.",
             "expected_behavior": "Still restates the action and asks for explicit confirmation before any write tool."},
        ]
    for c in pos:
        if isinstance(c.get("tools_triggered"), list):
            c["tools_triggered"] = ", ".join(c["tools_triggered"])
    return {"test_cases": {"positive": pos[:5], "negative": neg[:3]},
            **({"demo_recording_url": R.demo_recording_url} if R.demo_recording_url else {}),
            "commerce": R.commerce, "commerce_description": R.commerce_description}


def write_manifests(cfg: Config, surface: ServerSurface, out: Path, log: Callable[[str], None], oauth_md=None) -> dict:
    L = cfg.listing
    name = L.name or kebab(L.display_name or surface.server_info.get("name", "plugin"))
    display = L.display_name or surface.server_info.get("name", name).title()
    desc = L.description or surface.instructions.split("\n")[0][:200] or f"{display} tools and services."
    short = (L.short_description or desc).strip()
    if len(short) > 30:   # portal: "Subtitle must be 30 characters or fewer"
        cut = short[:30].rsplit(" ", 1)[0].rstrip(" ,;:-–—") or short[:30]
        log(f"[gen] shortDescription truncated to 30 chars: '{short}' → '{cut}'")
        short = cut
    long_ = L.long_description or desc

    kinds = [_tool_kind(t)[1] for t in surface.tools]
    caps = L.capabilities or (["ChatGPT"] if (cfg.output.target or "builder").lower() == "builder"
                              else sorted({"Write" if k.startswith("write") else "Read" for k in kinds} or {"Read"}))

    target = (cfg.output.target or "builder").lower()
    builder = target == "builder"
    website = L.website_url or L.homepage or L.author_url
    dp = L.default_prompt or f"What can {display} help me with?"
    if builder and isinstance(dp, str):
        dp = [dp]
    interface = {
        "displayName": display,
        "developerName": L.developer_name or display,
        "category": L.category,
        "capabilities": caps,
        "shortDescription": short,
        "longDescription": long_,
        "brandColor": L.brand_color,
        "defaultPrompt": dp,
        "composerIcon": "assets/logo.png" if builder else "./assets/icon.png",
        "logo": "assets/logo.png" if builder else "./assets/logo.png",
        **({} if builder else {"screenshots": ["./assets/screenshot-1.png", "./assets/screenshot-2.png"]}),
        # exact portal field names (case matters): websiteURL / supportURL / privacyPolicyURL / termsOfServiceURL
        "websiteURL": website,
        "supportURL": L.support_url or website,
        "privacyPolicyURL": L.privacy_policy_url,
        "termsOfServiceURL": L.terms_of_service_url,
    }
    if L.support_email:
        interface["supportEmail"] = L.support_email
    interface = {k: v for k, v in interface.items() if v not in (None, "", [])}

    ext = {"interface": interface}
    if target in ("portal", "builder"):
        ext["review"] = _review_block(cfg, surface, display)          # developer-portal review metadata
        if L.release_notes:
            ext["publication"] = {"release_notes": L.release_notes}
        if not cfg.review.demo_recording_url:
            log("[gen] review.demo_recording_url is empty — required for MCP review (set it in config)")
    else:
        log("[gen] target=workspace: no `review` block (the Admin Console rejects it)")
    # The portal rejects packages that carry .app.json when an MCP server URL is declared
    # ("App references detected: Plugins with .app.json cannot be submitted"). Only emit it when
    # explicitly requested AND no mcp.json is emitted.
    if target == "workspace":
        if not L.app_id or not str(L.app_id).startswith("asdk_app_") or "REPLACE" in str(L.app_id).upper():
            raise SystemExit("[gen] output.target=workspace needs listing.app_id = the workspace MCP App id (asdk_app_…): "
                             "Admin Console → Plugins → Add → Create MCP App (URL + OAuth), then copy its id. "
                             "ChatGPT shows 'Could not load this app's connection details' when the id is stale or not in this workspace.")
        emit_app, emit_mcp = True, cfg.output.emit_mcp_json
        log(f"[gen] target=workspace: .app.json → {L.app_id} (binding the console resolves) + mcp.json/.mcp.json "
            f"(explicit server definition; verified accepted by the Admin Console)")
    else:
        emit_app, emit_mcp = False, cfg.output.emit_mcp_json
        if L.app_id:
            log(f"[gen] target={target}: app_id ignored — the developer portal rejects .app.json; MCP declared via mcp.json")
    if emit_app:
        ext["apps"] = "./.app.json"

    if builder:
        manifest = {"$schema": PLUGIN_SCHEMA, "name": name, "version": L.version, "description": desc,
                    "author": {"name": L.developer_name or display}, "extensions": {"com.openai": ext}}
    else:
        manifest = {
            "$schema": PLUGIN_SCHEMA,
            "name": name,
            "version": L.version,
            "description": desc,
            "author": {"name": L.developer_name or display, **({"url": L.author_url} if L.author_url else {})},
            **({"homepage": L.homepage} if L.homepage else {}),
            "license": L.license,
            **({"keywords": L.keywords} if L.keywords else {}),
            "skills": "./skills",
            "extensions": {"com.openai": ext},   # mcp.json at the root is discovered automatically (no pointer key)
        }
    (out / "plugin.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if emit_app:
        (out / ".app.json").write_text(json.dumps({"apps": {name: {"id": L.app_id}}}, indent=2) + "\n", encoding="utf-8")

    if emit_mcp:
        server = {"type": cfg.mcp.transport, "url": cfg.mcp.url}
        declare = str(cfg.oauth.declare_in_mcp_json).strip().lower()
        if declare in ("", "auto", "none"):
            declare_auth = not builder          # Plugin-Builder layout: no auth block → ChatGPT discovers OAuth from the server
        else:
            declare_auth = declare in ("1", "true", "yes", "on", "force")
        # MANDATORY for builder packages: the MCP server must survive installation. Verified 4x on ChatGPT desktop/personal
        # installs (2026-10-13): ANY extensions.com.openai.auth block makes ChatGPT drop the server. So the builder target
        # never writes one — `oauth.declare_in_mcp_json: force` is the only override (portal-connected packages).
        if builder and declare_auth and declare != "force":
            log("[gen] declare_in_mcp_json=true IGNORED for target=builder: an auth block makes ChatGPT drop the MCP server "
                "on desktop/personal installs (verified). Server declared with type+url only. Use `force` to override.")
            declare_auth = False
        if not declare_auth and cfg.oauth.mode in ("auth_code", "client_creds"):
            if oauth_md is None or not oauth_md.authorization_endpoint:
                log("[gen] WARNING: mcp.json carries no auth block and the server did not expose OAuth metadata "
                    "(401 → WWW-Authenticate resource_metadata / .well-known) — ChatGPT cannot discover the login. "
                    "Fix the server or set oauth.declare_in_mcp_json: true")
            else:
                log("[gen] mcp.json: server declared with type+url only (Plugin-Builder layout). ChatGPT discovers OAuth "
                    f"from the MCP server (401 → {oauth_md.issuer or 'authorization server'}) and prompts the user to sign in "
                    f"on install; enter client id '{cfg.oauth.client_id}' + secret in the portal connection form.")
        if declare_auth and cfg.oauth.mode in ("auth_code", "client_creds"):
            client = {"mode": cfg.oauth.client_mode}
            if cfg.oauth.client_mode == "provided":
                client["clientId"] = cfg.oauth.client_id
                if (cfg.oauth.token_endpoint_auth_method or "none") != "none":
                    client["tokenEndpointAuthMethod"] = cfg.oauth.token_endpoint_auth_method   # omitted = none (public client, PKCE)
            if cfg.oauth.exact_scopes:
                # OpenAI sends exactly these scopes: baseScopes are always sent, defaultScopes: [] suppresses OpenAI's own
                # defaults (openid email offline_access), and oidcEnabled=false removes the OIDC scopes.
                base = [sc for sc in cfg.oauth.scopes if sc != "openid"]
                auth = {"type": "oauth", "client": client, "baseScopes": base, "defaultScopes": [],
                        "oidcEnabled": False}
                if cfg.oauth.oidc_enabled:
                    log("[gen] exact_scopes=true forces oidcEnabled=false (OIDC would add openid/email to the request)")
                log(f"[gen] auth: OpenAI will request exactly: {' '.join(base) or '(no scopes)'}")
            else:
                # OpenAI always adds openid email offline_access itself; baseScopes = the API scopes the gateway enforces
                auth = {"type": "oauth", "client": client,
                        "baseScopes": [sc for sc in cfg.oauth.scopes if sc not in ("openid", "email", "profile", "offline_access")] or [],
                        "oidcEnabled": bool(cfg.oauth.oidc_enabled)}
                if not auth["baseScopes"]:
                    auth.pop("baseScopes")
            if oauth_md is not None:
                if oauth_md.authorization_endpoint:
                    auth["authorizationUrl"] = oauth_md.authorization_endpoint
                if oauth_md.token_endpoint:
                    auth["tokenUrl"] = oauth_md.token_endpoint
                auth["resource"] = cfg.oauth.resource or cfg.mcp.url
                if oauth_md.issuer:
                    auth["authorizationServerBase"] = oauth_md.issuer
                if cfg.oauth.oidc_enabled and not cfg.oauth.exact_scopes:
                    if oauth_md.issuer:
                        auth["oidcConfigurationUrl"] = oauth_md.issuer.rstrip("/") + "/.well-known/openid-configuration"
                    if oauth_md.userinfo_endpoint:
                        auth["oidcUserinfoEndpoint"] = oauth_md.userinfo_endpoint
                    if oauth_md.scopes_supported:
                        auth["oidcScopesSupported"] = [sc for sc in ("openid", "email", "profile") if sc in oauth_md.scopes_supported]
            if not auth.get("oidcEnabled"):
                log("[gen] oidcEnabled=false: no oidc* fields; OpenAI review's UserInfo/email requirement is not met by this config")
            order = ["type", "client", "authorizationUrl", "tokenUrl", "resource", "baseScopes", "defaultScopes", "oidcEnabled",
                     "authorizationServerBase", "oidcConfigurationUrl", "oidcUserinfoEndpoint", "oidcScopesSupported"]
            auth = {k: auth[k] for k in order if k in auth} | {k: v for k, v in auth.items() if k not in order}
            server["extensions"] = {"com.openai": {"auth": auth}}
        elif cfg.oauth.mode == "api_key" and builder and declare != "force":
            log("[gen] api_key auth block NOT written (builder package): ChatGPT drops the server on install with any auth block. "
                "Configure the header in ChatGPT desktop → Add custom MCP server instead, or use `declare_in_mcp_json: force`.")
        elif cfg.oauth.mode == "api_key":
            scheme = (cfg.oauth.api_key_header_scheme or "basic").lower()
            auth = {"type": "api_key", "headerScheme": scheme}
            if scheme == "custom_header":
                auth["headerName"] = cfg.oauth.api_key_header_name
            server["extensions"] = {"com.openai": {"auth": auth}}
            how = {"basic": f"Authorization: Basic base64(<client id>:<client secret>) — enter '{cfg.oauth.client_id}' + secret at connection setup",
                   "bearer": "Authorization: Bearer <key> — enter the key at connection setup",
                   "custom_header": f"{cfg.oauth.api_key_header_name}: <key> — enter the key at connection setup"}[scheme]
            log(f"[gen] mcp.json auth: api_key/{scheme} → ChatGPT sends {how}. No OAuth redirect. "
                "NOTE: the developer portal's connection form does not support api_key declarations (personal/desktop installs only); "
                "the MCP server must authenticate this header itself.")
        elif declare_auth and cfg.oauth.mode == "none":
            server["extensions"] = {"com.openai": {"auth": {"type": "none"}}}
        (out / "mcp.json").write_text(json.dumps({"$schema": MCP_SCHEMA, "mcpServers": {name: server}},
                                                 indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if not builder:
            # Codex / ChatGPT-desktop runtime reads the Codex-format declaration: .mcp.json referenced from
            # .codex-plugin/plugin.json ("mcpServers": "./.mcp.json"). Same server, no $schema.
            codex_server = {k: v for k, v in server.items() if k != "type"}
            (out / ".mcp.json").write_text(json.dumps({"mcpServers": {name: codex_server}},
                                                      indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            log(f"[gen] MCP server '{name}' declared in mcp.json (portal) and .mcp.json (Codex/desktop runtime)")
        elif declare_auth:
            log(f"[gen] MCP server '{name}' declared in mcp.json with OAuth client '{cfg.oauth.client_id}' "
                f"(mode {cfg.oauth.client_mode}; secret is entered in the portal connection form, never packaged)")
            if cfg.oauth.client_mode == "provided" and cfg.oauth.token_endpoint_auth_method != "none":
                log("[gen] WARNING: confidential client (tokenEndpointAuthMethod="
                    f"{cfg.oauth.token_endpoint_auth_method}). ChatGPT only has the secret for a connection made in the "
                    "developer portal; on a personal upload or desktop install it has none and DROPS the MCP server "
                    "(plugin installs with no server / no tools). For install-time SSO everywhere use "
                    "oauth.token_endpoint_auth_method: none (public client + PKCE; Keycloak: Client authentication OFF) "
                    "or oauth.declare_in_mcp_json: false (discovery/CIMD layout).")

    if cfg.output.emit_codex_compat and not builder:
        compat = {"name": name, "version": L.version, "description": desc, "author": manifest["author"],
                  "interface": interface, "skills": "./skills",
                  **({"apps": "./.app.json"} if emit_app else {}),
                  **({"mcpServers": "./.mcp.json"} if emit_mcp else {})}
        (out / ".codex-plugin").mkdir(exist_ok=True)
        (out / ".codex-plugin" / "plugin.json").write_text(json.dumps(compat, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    log(f"[gen] manifests written (name={name}, capabilities={caps})")
    return {"name": name, "display": display, "interface": interface}


def write_skill(cfg: Config, surface: ServerSurface, out: Path, meta: dict, log: Callable[[str], None]) -> Path:
    L = cfg.listing
    name, display = meta["name"], meta["display"]
    skill_dir = out / "skills" / f"{name}-services"
    skill_dir.mkdir(parents=True, exist_ok=True)

    rows, public, client, writes = [], [], [], []
    for t in surface.tools:
        auth, kind = _tool_kind(t)
        d = (t.get("description") or _title(t["name"])).strip().split("\n")[0]
        props = (t.get("inputSchema") or {}).get("properties") or {}
        req = (t.get("inputSchema") or {}).get("required") or []
        args = ", ".join(f"`{p}`{'*' if p in req else ''}" for p in props) or "—"
        rows.append(f"| `{t['name']}` | {auth} | {kind} | {d[:110]} | {args} |")
        (client if auth != "public" else public).append(t["name"])
        if kind.startswith("write"):
            writes.append(t["name"])

    prompts = "".join(f"- `{p['name']}` — {p.get('description', '')}\n" for p in surface.prompts)
    resources = "".join(f"- `{r.get('uri')}` — {r.get('name', '')}\n" for r in surface.resources)
    domain_rules = "".join(f"- {d}\n" for d in L.domain_notes)
    instr = surface.instructions.strip()

    md = f"""---
name: {name}-services
description: Use when the user asks about {display} or wants to use {display} services{' — including, once signed in, their own account data' if client else ''}. Routes every request through the `{name}` MCP tools; never answers from memory what a tool can return.
---

# {display}

All data comes from the `{name}` MCP server{(' (' + surface.server_info.get('name', '') + ')') if surface.server_info.get('name') else ''}. Use the tools below; do not invent values, prices, balances or terms.
{('\n> Server guidance: ' + instr + '\n') if instr else ''}
## Modes

**Public information** — no sign-in needed: {', '.join(f'`{n}`' for n in public) or '(none)'}.

**Authorized services** — require the user's connected account: {', '.join(f'`{n}`' for n in client) or '(none)'}.
If a tool returns an authentication error or the account isn't connected, ask the user to connect {display} (OAuth sign-in in the browser window). Never ask the user to type passwords, ID numbers or one-time codes into the chat.

## Rules

- **Confirm before any write** ({', '.join(f'`{n}`' for n in writes) or 'none detected'}): restate every parameter, get an explicit "yes", call once, never auto-retry a write that may have succeeded.
- Prefer read tools; never chain a write after a read without a fresh confirmation.
- Quote the "as of" timestamp the tool returns; present facts, not recommendations.
- Show only the signed-in user's own data; mask identifiers when summarizing.
- On a tool error, report it plainly and suggest the next step (retry, reconnect, contact support). Don't fabricate a fallback.
{('- ' + L.language_note + chr(10)) if L.language_note else ''}{domain_rules}
## Tools

`*` = required argument.

| Tool | Auth | Kind | Use for | Arguments |
|------|------|------|---------|-----------|
{chr(10).join(rows) if rows else '| (no tools returned by tools/list) | | | | |'}
{('\n## Prompts\n\n' + prompts) if prompts else ''}{('\n## Resources\n\n' + resources) if resources else ''}
## Response style

Lead with the answer (figure, status, list), then one line of context and the as-of time. Use a compact table for lists. End with the next helpful action, not a disclaimer wall.
"""
    (skill_dir / "SKILL.md").write_text(md, encoding="utf-8")
    dump = json.dumps(
        {"server": surface.server_info, "protocolVersion": surface.protocol_version,
         "oauth": getattr(surface, "oauth_md", None) or {},
         "tools": [{k: v for k, v in t.items() if not k.startswith("_")} for t in surface.tools]},
        indent=2, ensure_ascii=False) + "\n"
    (skill_dir / "tools.json").write_text(dump, encoding="utf-8")
    if (cfg.output.target or "builder").lower() == "builder":
        (out / "tools.json").write_text(dump, encoding="utf-8")       # root tools.json, like the Plugin Builder
    log(f"[gen] skill written: {skill_dir.relative_to(out)} ({len(rows)} tools)")
    return skill_dir


def write_assets(cfg: Config, out: Path, meta: dict, log: Callable[[str], None]) -> list[str]:
    a = out / "assets"
    a.mkdir(exist_ok=True)
    L = cfg.listing
    notes = []
    mono = L.icon_text or "".join(w[0] for w in meta["display"].split()[:3]).upper()
    brand_dir = Path(__file__).resolve().parent.parent / "brand"          # bundled default brand mark
    def _pick(configured: str, bundled: str) -> Path | None:
        if configured and Path(configured).exists():
            return Path(configured)
        if (brand_dir / bundled).exists():
            return brand_dir / bundled
        return None
    src_icon = _pick(cfg.assets.icon, "logo.png")      # icon: the 512 mark (portal wants ≥48px square)
    src_logo = _pick(cfg.assets.logo, "logo-1024.png")
    if src_icon:
        shutil.copy(src_icon, a / "icon.png")
    else:
        assets_mod.monogram(512, a / "icon.png", mono, L.brand_color, L.accent_color); notes.append("icon.png is a placeholder")
    if src_logo:
        shutil.copy(src_logo, a / "logo.png")
    else:
        assets_mod.monogram(1024, a / "logo.png", mono, L.brand_color, L.accent_color); notes.append("logo.png is a placeholder")
    if (cfg.output.target or "builder").lower() == "builder":
        if src_icon:
            shutil.copy(src_icon, a / "logo.png")          # builder layout: one file, assets/logo.png (512 mark)
        (a / "icon.png").unlink(missing_ok=True)
        if notes:
            log("[gen] " + "; ".join(n for n in notes if "logo" in n))
        return [n for n in notes if "logo" in n]
    shots = [p for p in cfg.assets.screenshots if Path(p).exists()]
    for i in range(2):
        dst = a / f"screenshot-{i + 1}.png"
        if i < len(shots):
            shutil.copy(shots[i], dst)
        else:
            assets_mod.placeholder_screenshot(dst, meta["display"], L.brand_color, i + 1); notes.append(f"{dst.name} is a placeholder")
    if notes:
        log("[gen] " + "; ".join(notes))
    return notes


def write_scripts(out: Path) -> None:
    src = Path(__file__).parent / "scripts_template" / "discover_tools.py"
    (out / "scripts").mkdir(exist_ok=True)
    shutil.copy(src, out / "scripts" / "discover_tools.py")


def write_readme(cfg: Config, out: Path, meta: dict, surface: ServerSurface, asset_notes: list[str],
                 userinfo: dict | None) -> None:
    L = cfg.listing
    host = re.sub(r"^https?://", "", cfg.mcp.url).split("/")[0]
    issuer = (surface.oauth_md or {}).get("issuer") or cfg.oauth.issuer or "authorization server"
    ui = ""
    if userinfo:
        ui = f"- UserInfo check: {'OK (' + userinfo.get('email', '') + ')' if userinfo.get('ok') else 'ISSUES — ' + '; '.join(userinfo.get('issues', []))}\n"
    # Values for the ChatGPT connector "advanced settings" / developer-portal connection form (User-Defined OAuth Client).
    # Entered in the UI, never shipped in the zip (the secret stays in config.yaml credentials / the UI only).
    scopes_only = [sc for sc in cfg.oauth.scopes if sc != "openid"]
    (out / "CONNECTOR_SETTINGS.json").write_text(json.dumps({
        "purpose": "Enter these in the ChatGPT connector / developer-portal connection form. Not part of the package.",
        "registration_method": "User-Defined OAuth Client",
        "mcp_server_url": cfg.mcp.url,
        "client_id": cfg.oauth.client_id,
        "client_secret": "<from config.yaml credentials.oauth_client_secret — type it in the UI>",
        "token_endpoint_auth_method": cfg.oauth.token_endpoint_auth_method,
        "callback_url": "https://chatgpt.com/connector_platform_oauth_redirect",
        "authorization_server_base": issuer,
        "authorization_url": (surface.oauth_md or {}).get("authorization_endpoint", ""),
        "token_url": (surface.oauth_md or {}).get("token_endpoint", ""),
        "resource": cfg.mcp.url,
        "oidc_enabled": bool(cfg.oauth.oidc_enabled),
        "scopes": scopes_only,
        "keycloak_client_requirements": {
            "valid_redirect_uris": ["https://chatgpt.com/connector_platform_oauth_redirect"],
            "default_client_scopes": scopes_only,
            "client_authentication": "on" if cfg.oauth.token_endpoint_auth_method != "none" else "off (public, PKCE S256)"},
    }, indent=2) + "\n", encoding="utf-8")
    (out / "SUBMISSION.md").write_text(f"""# {meta['display']} — submission guide (generated)

Generated by mcp-plugin-gen from `{cfg.mcp.url}` ({len(surface.tools)} tools discovered).

## Secrets
The OAuth client secret is entered **only** in the OpenAI developer portal connection form.
It is not in this package. Rotate it if it was ever shared in plain text.

## Gateway checklist
- [ ] Public HTTPS, Streamable HTTP at `{cfg.mcp.url}`
- [ ] `.well-known/oauth-protected-resource` + authorization-server metadata published
- [ ] OAuth 2.1 auth-code + PKCE S256; OpenAI's redirect URIs registered for client `{cfg.oauth.client_id or '<client id>'}`
- [ ] UserInfo returns `email` + `email_verified: true`; `openid`, `email` in scopes_supported
{ui}- [ ] Every tool has `annotations.readOnlyHint` (see VALIDATION.md for gaps); writes require confirmation
- [ ] Domain verification token served at `https://{host}/.well-known/openai-apps-challenge`

## Listing
- [ ] Replace placeholder assets: {', '.join(asset_notes) or 'none — real assets supplied'}
- [ ] Check privacy / terms / support URLs resolve
- [ ] Category `{L.category}` — pick the nearest in the portal if it differs

## Organisation
- [ ] Publish from the company's OpenAI org, Apps Management write access, business identity verification
- [ ] Compliance sign-off on listing copy and exposed tools

## Which surface is this package for?
- `output.target: builder`   → Plugin-Builder layout (plugin.json, mcp.json, tools.json, skills/, assets/).
  `oauth.declare_in_mcp_json: true` (default): mcp.json declares the server AND the OAuth client — ChatGPT identifies as
  `{cfg.oauth.client_id or '<client id>'}` (mode {cfg.oauth.client_mode}, {cfg.oauth.token_endpoint_auth_method}) with the secret you enter in the
  portal connection form, redirect `https://chatgpt.com/connector_platform_oauth_redirect`, scope `{' '.join(sc for sc in cfg.oauth.scopes if sc != 'openid')}`,
  OIDC {'off' if not cfg.oauth.oidc_enabled else 'on'}. Connect it in the developer portal (web).
  `oauth.declare_in_mcp_json: false`: `type` + `url` only, exactly like the ChatGPT Plugin Builder — ChatGPT discovers OAuth
  from the server (401 → protected-resource metadata → `{issuer}`) and uses its own CIMD client
  (`https://chatgpt.com/oauth/codex/client.json`, loopback redirect) — works on desktop/Codex too, but Keycloak must accept that client.
- `output.target: portal`    → developer portal (platform.openai.com → Plugins): MCP declared in mcp.json; Connect + domain verification there.
- `output.target: workspace` → Admin Console (admin.openai.com → Plugins → Add → Upload plugin): MCP is a workspace
  *MCP App* (Add → Create MCP App: URL + OAuth client id/secret) and this zip references it via .app.json (listing.app_id).
  The zip also carries mcp.json/.mcp.json with the same server (explicit definition; desktop runtime).
  AFTER upload the admin must ENABLE the MCP App for members: Admin Console → Plugins → "Configure MCP apps /
  Review permissions" (or Workspace connections) → the app → enable / approve, then Plugins → Manage access.
  Until then users get "Disabled by admin" on Connect.

## Runtime OAuth — what ChatGPT really sends at install/connect (see PREFLIGHT.md for the live check)
With `mcp.json` = type + url, ChatGPT discovers OAuth from the MCP server and sends:
| surface | client_id | redirect_uri | secret |
|---|---|---|---|
| ChatGPT web (portal connection) | `{cfg.oauth.client_id or '<client id>'}` | `https://chatgpt.com/connector_platform_oauth_redirect` | portal form |
| ChatGPT web (CIMD) | `https://chatgpt.com/oauth/chatgpt/client.json` | `https://chatgpt.com/connector_platform_oauth_redirect` | none (PKCE) |
| ChatGPT desktop / Codex | `https://chatgpt.com/oauth/codex/client.json` | `http://127.0.0.1:<random>/callback` | none (PKCE) |

Scopes: ChatGPT requests `scopes_supported` from `{cfg.mcp.url.split('/mcp')[0]}/.well-known/oauth-protected-resource`.
If that document has none, it requests **every** scope of the authorization server. To request only
`{' '.join(sc for sc in cfg.oauth.scopes if sc != 'openid')}`, the MCP server must publish:
```json
{{"resource": "{cfg.mcp.url}", "authorization_servers": ["{issuer}"], "scopes_supported": {json.dumps([sc for sc in cfg.oauth.scopes if sc != 'openid'])}, "bearer_methods_supported": ["header"]}}
```
and answer unauthenticated calls with `401` + `WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource", scope="{' '.join(sc for sc in cfg.oauth.scopes if sc != 'openid')}"`.

Keycloak: the two CIMD client ids must exist as **public** clients (PKCE S256, no secret) with redirect URIs
`http://127.0.0.1*`, `http://localhost*`, `https://chatgpt.com/connector_platform_oauth_redirect`; the confidential
client `{cfg.oauth.client_id or '<client id>'}` needs the chatgpt.com redirect too. All three get exactly the client scopes above as
*default* scopes. `scripts/keycloak_chatgpt_runtime.sh` does all of this and replays the three authorize requests.
Web + desktop availability needs nothing in the package — a plugin is offered on web, desktop and Codex once both runtimes can connect.

## Submit — IMPORTANT: the MCP server is attached in the portal, not by this zip
`mcp.json` in the package does NOT register the server. A draft created as "Skills only" is judged as
skills-only ("We are not accepting more skills-only submissions at this time") no matter what the zip contains.

1. Portal → Plugins → **Create plugin → With MCP** (a new draft; the skills-only draft cannot be converted —
   delete it afterwards so the package name `{meta['name']}` is free).
2. MCP server URL: `{cfg.mcp.url}` · Auth: OAuth 2.1 · client ID `{cfg.oauth.client_id or '<client id>'}` · client secret (portal only).
   Register the redirect URI(s) the portal shows on the OAuth client, if not already present.
3. Domain verification: serve the token at `https://{host}/.well-known/openai-apps-challenge`.
4. Upload this zip into that draft as the skills/metadata package → fix findings → Submit for review.

Docs: https://developers.openai.com/plugins/build/plugins · https://developers.openai.com/plugins/guides/submit-claude-plugin
""", encoding="utf-8")
