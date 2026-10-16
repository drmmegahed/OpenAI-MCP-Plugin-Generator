"""Pre-submission validation of a generated package. Produces VALIDATION.md and a non-zero
exit when a blocking issue exists (secrets, missing manifest fields, non-HTTPS endpoint...)."""
from __future__ import annotations

import json
import re
from pathlib import Path

from PIL import Image

SECRET_PATTERNS = [
    re.compile(r"(?i)(client_secret|api[_-]?key|secret|password|otp)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\b[a-f0-9]{64}\b"),                   # hex secret / sha256-looking
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),  # JWT
]
TEXT_EXT = {".json", ".md", ".py", ".yaml", ".yml", ".txt"}


def validate(pkg: Path, secrets_to_scan: list[str] | None = None) -> tuple[list[str], list[str]]:
    errors, warnings = [], []

    m = pkg / "plugin.json"
    if not m.exists():
        return ["plugin.json missing at package root"], warnings
    man = json.loads(m.read_text(encoding="utf-8"))
    for f in ("name", "version", "description"):
        if not man.get(f):
            errors.append(f"plugin.json: `{f}` is required")
    if man.get("name") and not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", man["name"]):
        errors.append("plugin.json: `name` must be kebab-case")
    if man.get("name", "").startswith("dev-"):
        errors.append("plugin.json: `name` still has the portal's dev- placeholder")
    if not re.fullmatch(r"\d+\.\d+\.\d+", man.get("version", "")):
        warnings.append("plugin.json: `version` is not semver")
    iface = (man.get("extensions", {}).get("com.openai", {}) or {}).get("interface", {})
    if not iface:
        errors.append("plugin.json: extensions.com.openai.interface missing")
    for f in ("displayName", "developerName", "shortDescription", "longDescription", "category"):
        if not iface.get(f):
            errors.append(f"interface.{f} is required")
    if iface.get("developerName", "") in ("App developer", "", "Developer"):
        errors.append("interface.developerName is a placeholder")
    if iface.get("shortDescription") == iface.get("longDescription"):
        warnings.append("short and long descriptions are identical")
    for f in ("websiteURL", "supportURL", "privacyPolicyURL", "termsOfServiceURL"):
        if not iface.get(f):
            errors.append(f"interface.{f} is required by the portal")
    for wrong in ("websiteUrl", "supportUrl", "privacyPolicyUrl", "termsOfServiceUrl", "homepage"):
        if wrong in iface:
            errors.append(f"interface.{wrong}: wrong field name — use {wrong[:-3] + 'URL' if wrong.endswith('Url') else 'websiteURL'}")
    if not iface.get("brandColor"):
        warnings.append("interface.brandColor missing")
    if (pkg / ".app.json").exists() and (pkg / "mcp.json").exists():
        warnings.append("workspace package (.app.json + mcp.json): accepted by the Admin Console; the developer portal would "
                        "reject it because of .app.json — build with output.target: portal for that surface")
    if (pkg / ".app.json").exists():
        apps = json.loads((pkg / ".app.json").read_text(encoding="utf-8")).get("apps", {})
        if not apps or not all(str(v.get("id", "")).startswith("asdk_app_") and "REPLACE" not in str(v.get("id", "")).upper()
                               for v in apps.values()):
            errors.append(".app.json must map the plugin name to {\"id\": \"asdk_app_…\"} (the workspace MCP App id)")
        if man.get("extensions", {}).get("com.openai", {}).get("apps") != "./.app.json":
            errors.append('plugin.json extensions.com.openai.apps must be "./.app.json" for a workspace package')
        warnings.append("workspace package: upload in Admin Console → Plugins → Add → Upload plugin (NOT the developer portal). "
                        "The MCP App it binds must be enabled for members (otherwise users see 'Disabled by admin')")
    if iface.get("category") == "Other":
        warnings.append("category is `Other` — pick a specific one")

    # assets
    for key, size in (("composerIcon", 512), ("logo", 1024)):
        p = iface.get(key)
        if p:
            fp = pkg / p
            if not fp.exists():
                errors.append(f"{key} file not found: {p}")
            else:
                w, h = Image.open(fp).size
                if w != h:
                    warnings.append(f"{key} is not square ({w}x{h})")
                if w < 256:
                    warnings.append(f"{key} is small ({w}px); ≥{size}px recommended")
    for p in iface.get("screenshots", []):
        if not (pkg / p).exists():
            errors.append(f"screenshot not found: {p}")
    if iface.get("capabilities") == ["ChatGPT"]:
        pass   # Plugin Builder convention

    # package identity must be one name everywhere (root manifest, compat manifest, mcp.json key, skill dir, folder)
    pname = man.get("name", "")
    compat = pkg / ".codex-plugin" / "plugin.json"
    if compat.exists():
        cname = json.loads(compat.read_text(encoding="utf-8")).get("name", "")
        if cname != pname:
            errors.append(f"name mismatch: plugin.json '{pname}' vs .codex-plugin/plugin.json '{cname}'")
    if (pkg / "mcp.json").exists():
        keys = list(json.loads((pkg / "mcp.json").read_text(encoding="utf-8")).get("mcpServers", {}).keys())
        if keys and pname not in keys:
            warnings.append(f"mcp.json server key {keys} differs from plugin name '{pname}' (allowed, but keep them equal)")
    for sd in (pkg / "skills").glob("*/"):
        if pname and not sd.name.startswith(pname):
            warnings.append(f"skill dir '{sd.name}' is not prefixed with the plugin name '{pname}'")
    if pname and pkg.name not in (pname, "bad") and pkg.parent.name == "dist":
        warnings.append(f"package folder '{pkg.name}' differs from plugin name '{pname}' — rebuild instead of renaming")

    # Codex-format declaration (what ChatGPT desktop / Codex register under "From plugins")
    cmj = pkg / ".mcp.json"
    builder_layout = (pkg / "tools.json").exists() and not compat.exists()
    if (pkg / "mcp.json").exists() and not builder_layout:
        if not cmj.exists():
            errors.append(".mcp.json missing — the desktop/Codex runtime registers plugin MCP servers from .codex-plugin/plugin.json → ./.mcp.json")
        else:
            a = json.loads((pkg / "mcp.json").read_text(encoding="utf-8")).get("mcpServers", {})
            b = json.loads(cmj.read_text(encoding="utf-8")).get("mcpServers", {})
            for n in a:
                if n not in b or a[n].get("url") != b[n].get("url"):
                    errors.append(f".mcp.json server '{n}' missing or URL differs from mcp.json")
        if compat.exists():
            cm = json.loads(compat.read_text(encoding="utf-8"))
            if cm.get("mcpServers") != "./.mcp.json":
                errors.append('.codex-plugin/plugin.json must declare "mcpServers": "./.mcp.json" for the runtime to load the server')

    # mcp.json — the portal reads it from the zip; an invalid entry makes the plugin "skills-only"
    mj = pkg / "mcp.json"
    if mj.exists():
        for n, sv in json.loads(mj.read_text(encoding="utf-8")).get("mcpServers", {}).items():
            if sv.get("type") != "streamable-http":
                errors.append(f"mcp.json[{n}]: type must be streamable-http for the public directory")
            url = str(sv.get("url", ""))
            if not url.startswith("https://"):
                if re.match(r"https?://(127\.0\.0\.1|localhost)", url):
                    warnings.append(f"mcp.json[{n}]: loopback URL — fine for local testing, not for submission")
                else:
                    errors.append(f"mcp.json[{n}]: url must be https://")
            for bad in ("auth", "oauth", "clientSecret", "client_secret", "apiKey", "description"):
                if bad in sv:
                    errors.append(f"mcp.json[{n}]: unexpected key `{bad}` — auth goes under extensions.com.openai.auth; secrets never")
            if sv.get("headers"):
                errors.append(f"mcp.json[{n}]: non-empty headers are rejected by the portal")
            auth = ((sv.get("extensions") or {}).get("com.openai") or {}).get("auth")
            if not auth:
                # Plugin-Builder layout: ChatGPT discovers OAuth from the server (401 → protected-resource metadata)
                # and prompts for sign-in on install; client id/secret are entered in the portal connection form.
                pass
            else:
                if auth.get("type") not in ("none", "oauth", "mixed", "api_key"):
                    errors.append(f"mcp.json[{n}]: auth.type must be none|oauth|mixed|api_key")
                c = auth.get("client") or {}
                if auth.get("type") in ("oauth", "mixed"):
                    if c.get("mode") not in ("provided", "dcr", "cimd"):
                        errors.append(f"mcp.json[{n}]: auth.client.mode must be provided|dcr|cimd")
                    if c.get("mode") == "provided" and not c.get("clientId"):
                        errors.append(f"mcp.json[{n}]: auth.client.clientId required for mode=provided")
                    if c.get("mode") == "provided" and c.get("tokenEndpointAuthMethod", "none") != "none":
                        warnings.append(f"mcp.json[{n}]: confidential client ({c.get('tokenEndpointAuthMethod')}) — works only when "
                                        f"connected through the developer portal (secret stored there); personal/desktop installs "
                                        f"get NO MCP server. Use tokenEndpointAuthMethod none (public client + PKCE) for install-time SSO.")
                    if c.get("tokenEndpointAuthMethod") not in (None, "none", "client_secret_basic", "client_secret_post", "private_key_jwt"):
                        errors.append(f"mcp.json[{n}]: invalid tokenEndpointAuthMethod")
                    if "clientSecret" in c or "client_secret" in c:
                        errors.append(f"mcp.json[{n}]: clientSecret must not be in the package")
                if auth.get("type") == "api_key":
                    hs = auth.get("headerScheme")
                    if hs not in ("bearer", "basic", "custom_header"):
                        errors.append(f"mcp.json[{n}]: api_key.headerScheme must be bearer|basic|custom_header")
                    if hs == "custom_header" and (not auth.get("headerName") or str(auth.get("headerName")).lower() == "host"):
                        errors.append(f"mcp.json[{n}]: api_key with custom_header needs headerName (not Host)")
                    if hs != "custom_header" and "headerName" in auth:
                        errors.append(f"mcp.json[{n}]: headerName is only allowed with custom_header")
                    for k in ("key", "apiKey", "value", "token", "secret"):
                        if k in auth:
                            errors.append(f"mcp.json[{n}]: api_key block must not carry the credential (`{k}`)")
                    warnings.append(f"mcp.json[{n}]: api_key auth — VERIFIED: on a personal upload ChatGPT installs the plugin "
                                    f"WITHOUT the MCP server (same as any explicit auth block), and the developer portal's connection "
                                    f"form rejects api_key. Only usable where ChatGPT lets you type headers (desktop 'Add custom MCP server').")
    elif not (pkg / ".app.json").exists():
        warnings.append("no mcp.json and no .app.json — package will be treated as skills-only")

    # review block (required for MCP review)
    rv = (man.get("extensions", {}).get("com.openai", {}) or {}).get("review", {})
    if (pkg / ".app.json").exists() and rv:
        errors.append("workspace package must not contain extensions.com.openai.review (Admin Console rejects it)")
    if mj.exists():
        tc = rv.get("test_cases", {})
        if len(tc.get("positive", [])) < 5 or len(tc.get("negative", [])) < 3:
            warnings.append("review.test_cases: MCP review needs 5 positive + 3 negative cases")
        if not rv.get("demo_recording_url"):
            warnings.append("review.demo_recording_url missing — required for MCP review")
        for case in tc.get("positive", []):
            for f in ("description", "prompt", "tools_triggered", "expected_behavior"):
                if not case.get(f):
                    errors.append(f"review positive case missing `{f}`: {case.get('description', '?')}")
            for f in ("description", "prompt", "tools_triggered", "expected_behavior"):
                if f in case and not isinstance(case[f], str):
                    errors.append(f"review positive case `{f}` must be a string (portal: 'Input should be a valid string')")
        for case in tc.get("negative", []):
            for f in ("description", "prompt"):
                if not case.get(f):
                    errors.append(f"review negative case missing `{f}`")
    for f, lim in (("displayName", 30), ("shortDescription", 30), ("longDescription", 4000), ("developerName", 80)):
        if len(iface.get(f, "")) > lim:
            errors.append(f"interface.{f} must be ≤{lim} chars (is {len(iface[f])})")

    hooks = pkg / "hooks"
    if hooks.exists() or man.get("extensions", {}).get("com.openai", {}).get("hooks"):
        warnings.append("hooks present — plugins with lifecycle hooks are not eligible for the public directory")

    # skills
    skills = list((pkg / "skills").glob("*/SKILL.md"))
    if not skills:
        warnings.append("no skills/<name>/SKILL.md — allowed with MCP, but a skill greatly improves behaviour")
    for s in skills:
        txt = s.read_text(encoding="utf-8")
        if not txt.startswith("---") or "description:" not in txt.split("---")[1]:
            errors.append(f"{s.relative_to(pkg)}: frontmatter needs name + description")
        if re.search(r"\bClaude\b", txt):
            warnings.append(f"{s.relative_to(pkg)}: mentions Claude — use provider-neutral wording")
    tj = next(iter((pkg / "skills").glob("*/tools.json")), None)
    if tj:
        tools = json.loads(tj.read_text(encoding="utf-8")).get("tools", [])
        missing = [t["name"] for t in tools if "readOnlyHint" not in (t.get("annotations") or {})]
        if missing:
            warnings.append("tools without `readOnlyHint` annotation (review requires safety annotations): " + ", ".join(missing))
        bad = [t["name"] for t in tools if not t.get("description")]
        if bad:
            warnings.append("tools without description: " + ", ".join(bad))

    # secret scan
    for f in pkg.rglob("*"):
        if f.is_file() and f.suffix.lower() in TEXT_EXT:
            txt = f.read_text(encoding="utf-8", errors="ignore")
            for s in secrets_to_scan or []:
                if s and len(s) >= 8 and s in txt:
                    errors.append(f"SECRET LEAK: known secret found in {f.relative_to(pkg)}")
            for pat in SECRET_PATTERNS:
                if pat.search(txt) and f.name != "validate.py":
                    warnings.append(f"possible secret-like string in {f.relative_to(pkg)} — review")
                    break
    return errors, warnings


def write_report(pkg: Path, errors: list[str], warnings: list[str]) -> None:
    lines = ["# Validation report", ""]
    lines.append("**Result: " + ("BLOCKED — fix errors before submitting" if errors else "ready for portal upload") + "**\n")
    if errors:
        lines += ["## Errors", *[f"- ❌ {e}" for e in errors], ""]
    if warnings:
        lines += ["## Warnings", *[f"- ⚠️ {w}" for w in warnings], ""]
    if not errors and not warnings:
        lines.append("No issues found.")
    (pkg / "VALIDATION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
