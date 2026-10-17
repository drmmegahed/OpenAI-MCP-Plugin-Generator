# OpenAI MCP Plugin Generator

Turn any remote **MCP server** into a ready-to-upload **ChatGPT / Codex plugin** — with one YAML file and one command.

```
config.yaml  →  OAuth login  →  discover tools  →  build package  →  validate  →  preflight  →  plugin.zip
```

The hard part of publishing an MCP server as an OpenAI plugin is not the server. It is the manifest format, the
review test cases, the OAuth discovery, the scopes, the asset rules, and the many ways a package gets silently
rejected — or installs without its MCP server. This tool does that part for you and tells you what is left to fix.

## What it does

- **Logs in the way a user would** — OAuth 2.1 authorization code + PKCE, with optional headless SSO auto-login
  (Playwright drives your login form: mobile → ID → OTP, or any selectors you configure). Client-credentials and
  pre-obtained-token modes are available too.
- **Discovers your tools live** (`initialize`, `tools/list`) and smoke-tests the read-only ones.
- **Builds the package** in the exact layout ChatGPT's Plugin Builder produces:
  `plugin.json`, `mcp.json`, `tools.json`, a generated skill, logo. Review test cases are generated from the real tools.
- **Validates** it against the developer-portal rules: name limits, required URLs, review block, asset sizes — and
  fails the build if a secret is found anywhere inside the zip.
- **Preflights the connection**: replays the OAuth requests that ChatGPT web, ChatGPT desktop and Codex actually send
  to your identity provider, and reports what each one will hit (`Client not found`, bad redirect URI, wrong scopes…).
- **Writes the handover docs**: `SUBMISSION.md` (checklist), `PREFLIGHT.md`, `VALIDATION.md`,
  `CONNECTOR_SETTINGS.json` (values for the portal connection form — never shipped in the zip).

Secrets never enter the package. They live in `config.yaml` → `credentials:` (git-ignored) and in the portal.

## Quick start

```bash
git clone https://github.com/drmmegahed/OpenAI-MCP-Plugin-Generator
cd OpenAI-MCP-Plugin-Generator
cp examples/config.example.yaml config.yaml     # edit: MCP URL, OAuth client, scopes, listing copy, SSO selectors
./run.sh                                        # creates a venv, installs deps + Playwright, builds, validates
```

Output:

```
dist/<name>/
├── plugin.json                 manifest (interface, review test cases, publication)
├── mcp.json                    the MCP server: {type: streamable-http, url}
├── tools.json                  live tool schemas as discovered
├── skills/<name>-services/     SKILL.md generated from the real tools
├── assets/logo.png             your logo (bundled default if none given)
├── SUBMISSION.md · PREFLIGHT.md · VALIDATION.md · CONNECTOR_SETTINGS.json   (not in the zip)
└── ../<name>-<version>.zip     upload this
```

Other entry points:

```bash
./run.sh --preflight      # no login, no package: what will ChatGPT send to the IdP, and does it accept it?
./run.sh --patch          # re-render manifests after metadata-only changes (no login)
./run.sh --discover       # print the tools JSON and stop
./run.sh --validate       # re-validate the existing dist/<name>
./run.sh --test           # run the test suite (mock MCP server + mock IdP)
```

## Minimal config

```yaml
credentials:
  oauth_client_secret: "…"          # runtime only, never packaged
  sso_mobile: "+9665…"              # only if sso.enabled
  sso_id: "…"
  sso_otp: "…"

mcp:
  url: https://your-host/mcp

oauth:
  mode: auth_code                   # auth_code | client_creds | token | api_key | none
  client_id: your-client
  scopes: [holdings:read]
  redirect_uri: https://chatgpt.com/connector_platform_oauth_redirect
  oidc_enabled: false
  exact_scopes: true

listing:
  name: your-plugin                 # kebab-case package id
  display_name: Your Plugin
  short_description: Under 30 chars
  website_url: https://…
  privacy_policy_url: https://…
  terms_of_service_url: https://…
  support_url: https://…
```

See `examples/config.example.yaml` for every option with comments.

## How ChatGPT connects — the things that cost us days

These are the facts the tool is built around; the preflight checks every one of them.

1. **`mcp.json` must declare the server with `type` + `url` only.** Any `extensions.com.openai.auth` block makes
   ChatGPT drop the MCP server on desktop and personal installs. The generator enforces this for the default
   (`builder`) target; `oauth.declare_in_mcp_json: force` is the only override, for portal-connected packages.
2. **ChatGPT discovers OAuth from your server**: the `401` must carry
   `WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource[/mcp]", scope="…"`, and that
   document (serve it at both the root and the path-specific URL) must list `authorization_servers` and
   `scopes_supported`. Without `scopes_supported`, ChatGPT requests every scope your IdP advertises.
3. **ChatGPT uses its own OAuth clients** on desktop/Codex (`https://chatgpt.com/oauth/codex/client.json`, loopback
   redirect `http://127.0.0.1:<port>/callback`, PKCE, no secret) and on web personal installs
   (`https://chatgpt.com/oauth/chatgpt/client.json`). Your IdP has to accept them — Keycloak either via its CIMD
   support (needs egress to chatgpt.com) or by registering those URLs as public clients
   (`scripts/keycloak_chatgpt_runtime.sh`).
4. **Your own client id + secret** is used only when the plugin is connected through the OpenAI developer portal
   (`CONNECTOR_SETTINGS.json` has the form values). Web and published-plugin users go through it; desktop does not.
5. **OIDC off** simply means no `openid` scope is requested — publish only your API scope in `scopes_supported`.

`scripts/mcp_server_scopes/` has the nginx and FastAPI snippets for point 2; `scripts/keycloak_*.sh` cover point 3.

## Project layout

```
mcp_plugin_gen/     config · oauth (discovery, PKCE, preflight) · sso (Playwright) · mcp_client · generator · validate · cli
examples/           config.example.yaml
scripts/            Keycloak helpers, MCP-server metadata snippets, tools re-discovery script
brand/              default logo / icon
tests/              end-to-end tests against a mock MCP server + mock IdP  (./run.sh --test)
run.sh              one-command runner
```

## Requirements

Python 3.11+. `run.sh` installs everything else (httpx, PyYAML, Pillow, Playwright + Chromium) into a local venv.

## Contributing

Issues and PRs are welcome — especially findings about how ChatGPT's surfaces (web, desktop, Codex, Admin Console)
treat packages differently. Please keep secrets out of examples and logs.

## License

MIT
