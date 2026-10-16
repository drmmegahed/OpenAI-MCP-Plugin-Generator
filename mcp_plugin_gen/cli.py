"""CLI: mcp-plugin-gen build -c config.yaml  |  mcp-plugin-gen validate ./dist/<name>
            mcp-plugin-gen discover -c config.yaml   (tools only, no package)
            mcp-plugin-gen init > config.yaml        (starter config)
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import zipfile
from pathlib import Path

from .config import Config
from . import oauth as oauth_mod
from .mcp_client import McpClient, McpError, ServerSurface, probe_auth_required
from . import generator as gen
from .validate import validate, write_report


def log(msg: str) -> None:
    print(msg, flush=True)


def _overrides(pairs: list[str]) -> dict:
    import yaml as _yaml
    out = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        try:
            out[k] = _yaml.safe_load(v)      # true/false, numbers, [lists], plain strings
        except Exception:
            out[k] = v
    return out


LAST_MD = None


def acquire_token(cfg: Config) -> tuple[str | None, dict | None]:
    """Returns (bearer token or None, userinfo check or None). Sets LAST_MD to the OAuth metadata."""
    global LAST_MD
    o = cfg.oauth
    if o.mode == "none":
        return None, None
    if o.mode == "api_key":
        # header credential, exactly as ChatGPT will send it after the user enters it at connection setup
        scheme = (o.api_key_header_scheme or "basic").lower()
        if scheme == "basic":
            if not (o.client_id and o.client_secret):
                sys.exit("oauth.mode=api_key with headerScheme basic needs oauth.client_id + credentials.oauth_client_secret")
            import base64 as _b64
            cfg.mcp.extra_headers["Authorization"] = "Basic " + _b64.b64encode(f"{o.client_id}:{o.client_secret}".encode()).decode()
            log(f"[auth] api_key/basic: Authorization: Basic base64({o.client_id}:<secret>) on every MCP call")
        elif scheme == "bearer":
            if not o.api_key:
                sys.exit("oauth.mode=api_key with headerScheme bearer needs credentials.api_key")
            cfg.mcp.extra_headers["Authorization"] = f"Bearer {o.api_key}"
            log("[auth] api_key/bearer: Authorization: Bearer <api_key> on every MCP call")
        elif scheme == "custom_header":
            if not (o.api_key and o.api_key_header_name) or o.api_key_header_name.lower() == "host":
                sys.exit("oauth.mode=api_key with headerScheme custom_header needs oauth.api_key_header_name (not Host) + credentials.api_key")
            cfg.mcp.extra_headers[o.api_key_header_name] = o.api_key
            log(f"[auth] api_key/custom_header: {o.api_key_header_name}: <api_key> on every MCP call")
        else:
            sys.exit("oauth.api_key_header_scheme must be basic | bearer | custom_header")
        try:
            LAST_MD = oauth_mod.discover(cfg.mcp, o, lambda *_: None)   # informational only
        except Exception:
            LAST_MD = None
        return None, None
    if o.mode == "token":
        if not o.token:
            sys.exit("oauth.mode=token but oauth.token is empty (set OAUTH_TOKEN)")
        try:
            LAST_MD = oauth_mod.discover(cfg.mcp, o, log)
        except Exception:
            pass
        return o.token, None
    md = oauth_mod.discover(cfg.mcp, o, log)
    LAST_MD = md
    log(f"[oauth] issuer={md.issuer or '-'} auth={md.authorization_endpoint} token={md.token_endpoint}")
    if o.mode == "client_creds":
        ts = oauth_mod.client_credentials(md, o, cfg.mcp, log)
        return ts.access_token, None
    opener = None
    if cfg.sso.enabled:
        from .sso import make_sso_opener
        opener = make_sso_opener(cfg.sso, o.redirect_uri, log)
    ts = oauth_mod.authorization_code_flow(md, o, cfg.mcp, open_browser=opener, log=log)
    log(f"[oauth] token acquired (expires_in={ts.expires_in}s, scope='{ts.scope}')")
    ui = oauth_mod.check_userinfo(md, ts, cfg.mcp)
    log(f"[oauth] userinfo check: {'OK' if ui['ok'] else ('skipped (no openid)' if ui.get('skipped') else 'ISSUES: ' + '; '.join(ui['issues']))}")
    return ts.access_token, ui


def discover_surface(cfg: Config, token: str | None, smoke: bool) -> ServerSurface:
    c = McpClient(cfg.mcp, token, log)
    try:
        return c.discover(smoke_test_reads=smoke)
    finally:
        c.close()


def _mcp_server_mandatory(cfg: Config, out: Path) -> list[str]:
    """The package must carry the MCP server in the form ChatGPT keeps after installation."""
    mj = out / "mcp.json"
    if not mj.exists():
        return ["mcp.json missing — the MCP server definition is mandatory"]
    servers = json.loads(mj.read_text(encoding="utf-8")).get("mcpServers", {})
    if not servers:
        return ["mcp.json has no mcpServers entry — the MCP server definition is mandatory"]
    builder = (cfg.output.target or "builder").lower() == "builder"
    force = str(cfg.oauth.declare_in_mcp_json).strip().lower() == "force"
    for n, sv in servers.items():
        if sv.get("url") != cfg.mcp.url:
            return [f"mcp.json[{n}].url != mcp.url ({sv.get('url')} vs {cfg.mcp.url})"]
        if builder and not force and ((sv.get("extensions") or {}).get("com.openai") or {}).get("auth"):
            return [f"mcp.json[{n}]: auth block present in a builder package — ChatGPT drops the server on install (verified)"]
    return []


def _provided_only(cfg: Config) -> bool:
    """True only when mcp.json really declares client mode=provided (ChatGPT then uses that client, not CIMD).
    Builder packages never carry an auth block unless declare_in_mcp_json == "force", so for them this is False."""
    d = str(cfg.oauth.declare_in_mcp_json).strip().lower()
    builder = (cfg.output.target or "builder").lower() == "builder"
    if builder:
        return d == "force" and cfg.oauth.client_mode == "provided"
    declared = d in ("", "auto", "none") or d in ("1", "true", "yes", "on", "force")
    return declared and cfg.oauth.client_mode == "provided"


def _preflight(cfg: Config, md, out: Path) -> list[str]:
    """What ChatGPT web / desktop / Codex will send to the IdP at connect time — report, never block."""
    if md is None or cfg.oauth.mode not in ("auth_code", "client_creds"):
        return []
    try:
        problems = oauth_mod.runtime_preflight(cfg.mcp, cfg.oauth, md, log, provided_only=_provided_only(cfg))
    except Exception as e:  # noqa: BLE001
        problems = [f"preflight could not run: {e}"]
    if problems and all(pr.startswith("preflight skipped") for pr in problems):
        pass
    elif problems:
        log("[preflight] ChatGPT connect will FAIL until these are fixed:")
        for pr in problems:
            log(f"  ✗ {pr}")
        log("  → scripts/keycloak_chatgpt_runtime.sh fixes the Keycloak side; see SUBMISSION.md → 'Runtime OAuth'")
    else:
        log("[preflight] IdP accepts the ChatGPT web, desktop and Codex authorize requests")
    (out / "PREFLIGHT.md").write_text("# Runtime OAuth preflight\n\n" +
        ("\n".join(f"- ✗ {pr}" for pr in problems) if problems else "- ✓ IdP accepts ChatGPT web, desktop and Codex requests") +
        "\n", encoding="utf-8")
    return problems


def cmd_build(a) -> int:
    cfg = Config.load(a.config, _overrides(a.set))
    log("[cfg] " + json.dumps(cfg.redacted()["mcp"]))
    if cfg.oauth.mode == "none" and probe_auth_required(cfg.mcp):
        log("[mcp] server requires auth but oauth.mode=none — public tools only (or none)")
    token, userinfo = acquire_token(cfg)
    try:
        surface = discover_surface(cfg, token, a.smoke)
    except McpError as e:
        sys.exit(f"[mcp] discovery failed: {e}")

    name = cfg.listing.name or gen.kebab(cfg.listing.display_name or surface.server_info.get("name", "plugin"))
    out = Path(cfg.output.dir) / name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    if LAST_MD is not None:
        surface.oauth_md = {k: getattr(LAST_MD, k) for k in ("issuer", "authorization_endpoint", "token_endpoint",
                                                              "userinfo_endpoint", "scopes_supported")}
    meta = gen.write_manifests(cfg, surface, out, log, LAST_MD)
    gen.write_skill(cfg, surface, out, meta, log)
    notes = gen.write_assets(cfg, out, meta, log)
    if cfg.output.include_scripts:
        gen.write_scripts(out)
    gen.write_readme(cfg, out, meta, surface, notes, userinfo)
    _preflight(cfg, LAST_MD, out)

    # Smoke results are the real readiness signal: if every testable tool was refused for scope/auth,
    # ChatGPT will get the same refusals. Block the package until the OAuth client is fixed.
    smoked = [t for t in surface.tools if "_smoke" in t]
    refused = [t["name"] for t in smoked if "insufficient_scope" in str(t["_smoke"]) or " 401" in str(t["_smoke"])]
    secrets = [s for s in (cfg.oauth.client_secret, cfg.oauth.token, cfg.oauth.api_key, token or "") if s]
    secrets += [v.split(" ", 1)[-1] for v in cfg.mcp.extra_headers.values() if v]   # e.g. the Basic base64 form
    secrets += [str(s.get("value", "")) for s in cfg.sso.steps if s.get("value")]
    errors, warnings = validate(out, secrets)
    errors += _mcp_server_mandatory(cfg, out)
    if smoked and refused and len(refused) == len(smoked):
        errors.append(f"every tool call was refused ({', '.join(refused)}): the OAuth client does not grant the scopes "
                      f"the gateway enforces — fix the Keycloak client (scripts/keycloak_setup.sh) before uploading")
    elif refused:
        warnings.append(f"tools refused for scope/auth: {', '.join(refused)}")
    if userinfo is not None and not userinfo.get("ok") and not userinfo.get("skipped"):
        warnings.append("UserInfo check failed (" + "; ".join(userinfo.get("issues", [])) + ") — required for OpenAI review")
    write_report(out, errors, warnings)
    for e in errors:
        log(f"  ❌ {e}")
    for w in warnings:
        log(f"  ⚠️  {w}")

    if cfg.output.zip:
        zp = Path(cfg.output.dir) / f"{name}-{cfg.listing.version}.zip"
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(out.rglob("*")):
                if f.is_file() and "__pycache__" not in f.parts and _in_zip(cfg, f.relative_to(out)):
                    z.write(f, f.relative_to(out))          # plugin.json at zip root, as the portal expects
        log(f"[out] {zp}  ({zp.stat().st_size // 1024} KB)")
    log(f"[out] package dir: {out}")
    log("[out] " + ("BLOCKED — see VALIDATION.md" if errors else "ready for portal upload (see SUBMISSION.md)"))
    return 1 if errors else 0


def cmd_discover(a) -> int:
    cfg = Config.load(a.config, _overrides(a.set))
    token, _ = acquire_token(cfg)
    s = discover_surface(cfg, token, a.smoke)
    print(json.dumps({"server": s.server_info, "tools": s.tools, "prompts": s.prompts,
                      "resources": s.resources}, indent=2, ensure_ascii=False))
    return 0


def _in_zip(cfg: Config, rel: Path) -> bool:
    """builder target: ship only what the Plugin Builder ships (manifest, mcp.json, tools.json, skills, assets)."""
    if (cfg.output.target or "builder").lower() != "builder":
        return True
    top = rel.parts[0]
    if top in ("SUBMISSION.md", "VALIDATION.md", "PREFLIGHT.md", "CONNECTOR_SETTINGS.json", "scripts"):
        return False
    if top == "skills" and rel.name == "tools.json":
        return False
    return True


def cmd_patch(a) -> int:
    """Re-generate manifests/skill/assets/docs from config + the tools.json already in dist/<name>,
    without logging in again. Use after metadata-only changes (listing copy, URLs, subtitle)."""
    cfg = Config.load(a.config, _overrides(a.set))
    name = cfg.listing.name or gen.kebab(cfg.listing.display_name or "plugin")
    out = Path(cfg.output.dir) / name
    tj = None
    if out.exists():   # builder target keeps tools.json at the package root; the other targets inside the skill
        tj = (out / "tools.json") if (out / "tools.json").exists() else next(iter(out.glob("skills/*/tools.json")), None)
    if not tj:
        sys.exit(f"[patch] no existing package with tools.json under {out} — run a full build first")
    data = json.loads(tj.read_text(encoding="utf-8"))
    surface = ServerSurface(server_info=data.get("server", {}), protocol_version=data.get("protocolVersion", ""),
                            tools=data.get("tools", []), oauth_md=data.get("oauth", {}) or {})
    md = oauth_mod.OAuthMetadata(**surface.oauth_md) if surface.oauth_md else None
    if md is None and cfg.oauth.mode in ("auth_code", "client_creds"):
        try:
            md = oauth_mod.discover(cfg.mcp, cfg.oauth, log)
            surface.oauth_md = {k: getattr(md, k) for k in ("issuer", "authorization_endpoint", "token_endpoint",
                                                            "userinfo_endpoint", "scopes_supported")}
        except Exception as e:  # noqa: BLE001
            log(f"[patch] OAuth metadata not discovered ({e}); mcp.json will rely on discovery at connect time")
    for stale in (".app.json", "plugin.json", "mcp.json", ".mcp.json", "tools.json", "SUBMISSION.md", "VALIDATION.md", "PREFLIGHT.md", "CONNECTOR_SETTINGS.json"):
        (out / stale).unlink(missing_ok=True)
    shutil.rmtree(out / ".codex-plugin", ignore_errors=True)
    meta = gen.write_manifests(cfg, surface, out, log, md)
    gen.write_skill(cfg, surface, out, meta, log)
    notes = gen.write_assets(cfg, out, meta, log)
    if cfg.output.include_scripts:
        gen.write_scripts(out)
    gen.write_readme(cfg, out, meta, surface, notes, None)
    _preflight(cfg, md, out)
    errors, warnings = validate(out, [cfg.oauth.client_secret, cfg.oauth.token, cfg.oauth.api_key])
    errors += _mcp_server_mandatory(cfg, out)
    write_report(out, errors, warnings)
    for e in errors:
        log(f"  ❌ {e}")
    for w in warnings:
        log(f"  ⚠️  {w}")
    if cfg.output.zip:
        zp = Path(cfg.output.dir) / f"{name}-{cfg.listing.version}.zip"
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(out.rglob("*")):
                if f.is_file() and "__pycache__" not in f.parts and _in_zip(cfg, f.relative_to(out)):
                    z.write(f, f.relative_to(out))
        log(f"[out] {zp}")
    log("[out] " + ("BLOCKED — see VALIDATION.md" if errors else "ready for portal upload"))
    return 1 if errors else 0


def cmd_token(a) -> int:
    """Log in (same SSO flow as build) and print a bearer token for manual MCP clients
    (e.g. ChatGPT desktop → Settings → MCP servers → custom MCP → Headers: Authorization: Bearer <token>)."""
    cfg = Config.load(a.config, _overrides(a.set))
    token, _ = acquire_token(cfg)
    if not token:
        sys.exit("[token] no token (oauth.mode=none)")
    try:
        import base64
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        exp = claims.get("exp"); scope = claims.get("scope", "")
        left = int(exp - time.time()) if exp else None
        log(f"[token] scope='{scope}' expires in {left}s")
    except Exception:
        pass
    if a.out:
        Path(a.out).write_text(token, encoding="utf-8")
        log(f"[token] written to {a.out}")
    print(token)
    return 0


def cmd_validate(a) -> int:
    pkg = Path(a.package)
    errors, warnings = validate(pkg, [])
    write_report(pkg, errors, warnings)
    print((pkg / "VALIDATION.md").read_text(encoding="utf-8"))
    return 1 if errors else 0


def cmd_preflight(a) -> int:
    """Only the runtime preflight: what will ChatGPT web / desktop / Codex send, and does the IdP accept it?
    Prints the exact authorize URL each surface will open, so it can be compared with the one ChatGPT shows."""
    cfg = Config.load(a.config, _overrides(a.set))
    try:
        md = oauth_mod.discover(cfg.mcp, cfg.oauth, log)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"[preflight] discovery failed: {e}")
    prm_url, prm = oauth_mod.protected_resource_metadata(cfg.mcp)
    requested = (prm or {}).get("scopes_supported") if isinstance(prm, dict) else None
    requested = requested or md.scopes_supported
    log(f"[preflight] protected-resource metadata: {prm_url or 'NOT PUBLISHED'}"
        + (f" → scopes_supported={prm.get('scopes_supported')}" if isinstance(prm, dict) else ""))
    log(f"[preflight] ChatGPT will request scope: {' '.join(requested)}")
    problems = oauth_mod.runtime_preflight(cfg.mcp, cfg.oauth, md, log, provided_only=_provided_only(cfg))
    for pr in problems:
        log(f"  ✗ {pr}")
    if not problems:
        log("[preflight] OK — IdP accepts ChatGPT web, desktop and Codex; scope list matches oauth.scopes")
    return 1 if problems else 0


def cmd_init(a) -> int:
    print((Path(__file__).parent.parent / "examples" / "config.example.yaml").read_text(encoding="utf-8"))
    return 0


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="mcp-plugin-gen",
                                 description="Generate an OpenAI ChatGPT/Codex plugin package from a remote MCP server.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("build", cmd_build), ("discover", cmd_discover), ("patch", cmd_patch), ("token", cmd_token),
                     ("preflight", cmd_preflight)):
        p = sub.add_parser(name)
        p.add_argument("-c", "--config", required=True)
        p.add_argument("--set", action="append", metavar="key=value", help="override config, e.g. oauth.mode=token")
        p.add_argument("--smoke", action="store_true", help="call arg-less read-only tools to confirm they answer")
        p.set_defaults(fn=fn)
    sub.choices["token"].add_argument("--out", default="", help="also write the token to this file")
    p = sub.add_parser("validate"); p.add_argument("package"); p.set_defaults(fn=cmd_validate)
    p = sub.add_parser("init"); p.set_defaults(fn=cmd_init)
    a = ap.parse_args(argv)
    t0 = time.time()
    rc = a.fn(a)
    log(f"[done] {time.time() - t0:.1f}s")
    sys.exit(rc)


if __name__ == "__main__":
    main()
