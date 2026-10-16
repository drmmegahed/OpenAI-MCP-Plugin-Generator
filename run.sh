#!/usr/bin/env bash
# One-shot runner for mcp-plugin-gen: venv → deps → (optional) Playwright → build → report.
#
# Usage:
#   ./run.sh                       # build with ./config.yaml (created from the example if missing)
#   ./run.sh --mode token          # oauth.mode override: auth_code | client_creds | token | api_key | none
#   ./run.sh --no-sso              # manual browser login instead of headless SSO
#   ./run.sh --discover            # print live tools JSON only
#   ./run.sh --patch               # re-render manifests from config + existing dist (no login)
#   ./run.sh --token               # SSO login and print a bearer token (for manual MCP clients / ChatGPT desktop)
#   ./run.sh --validate            # re-validate the existing dist/<name>
#   ./run.sh --preflight           # no login: what scopes/clients ChatGPT will send and whether Keycloak accepts them
#   ./run.sh --test                # run the mock-server test suite
#
# All values, including credentials, come from config.yaml (see the `credentials:` block).
# Environment variables / .env are still honoured as a fallback for CI.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

CONFIG="${CONFIG:-config.yaml}"
VENV="${VENV:-.venv}"
MODE=""; SSO_FLAG=""; ACTION="build"; SMOKE="--smoke"; EXTRA=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)      MODE="$2"; shift 2 ;;
    --no-sso)    SSO_FLAG="--set sso.enabled=false"; shift ;;
    --no-smoke)  SMOKE=""; shift ;;
    --discover)  ACTION="discover"; shift ;;
    --patch)     ACTION="patch"; shift ;;
    --token)     ACTION="token"; shift ;;
    --validate)  ACTION="validate"; shift ;;
    --preflight) ACTION="preflight"; shift ;;
    --test)      ACTION="test"; shift ;;
    -c|--config) CONFIG="$2"; shift 2 ;;
    --set)       EXTRA+=("--set" "$2"); shift 2 ;;
    -h|--help)   sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1"; exit 2 ;;
  esac
done

log() { printf '\033[1;34m[run]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[run] %s\033[0m\n' "$*" >&2; exit 1; }

# ── Python / venv ───────────────────────────────────────────────────────────────
PY="$(command -v python3 || command -v python || true)"
[[ -n "$PY" ]] || die "python3 not found"
"$PY" - <<'EOF' || die "Python 3.10+ required"
import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)
EOF

if [[ ! -d "$VENV" ]]; then
  log "creating venv $VENV"
  "$PY" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate" 2>/dev/null || source "$VENV/Scripts/activate"
python -m pip install -q --upgrade pip >/dev/null

if ! python -c "import httpx, yaml, PIL, pytest" 2>/dev/null || ! command -v mcp-plugin-gen >/dev/null || [[ "${REINSTALL:-0}" == "1" ]]; then
  log "installing mcp-plugin-gen (+dev)"
  pip install -q -e '.[dev]'
fi

# ── Secrets: optional .env, then the `credentials:` block of the YAML (YAML wins) ──
if [[ -f .env ]]; then
  log "loading .env"
  set -a; # shellcheck disable=SC1091
  source .env; set +a
fi

# ── Config ──────────────────────────────────────────────────────────────────────
if [[ ! -f "$CONFIG" ]]; then
  cp examples/config.example.yaml "$CONFIG"
  log "created $CONFIG from the example — review MCP URL, listing copy and SSO selectors"
fi

# Export credentials from the YAML so ${ENV} references in the config resolve.
#   credentials:
#     oauth_client_secret: "..."   → OAUTH_CLIENT_SECRET
#     oauth_token: "..."           → OAUTH_TOKEN
#     sso_mobile / sso_id / sso_otp → SSO_MOBILE / SSO_ID / SSO_OTP
#     any_other_key: "..."         → ANY_OTHER_KEY
while IFS='=' read -r k v; do
  [[ -n "$k" ]] && export "$k=$v"
done < <(python - "$CONFIG" <<'EOF'
import sys, yaml, shlex
c = (yaml.safe_load(open(sys.argv[1])) or {}).get("credentials") or {}
for k, v in c.items():
    if v not in (None, ""):
        print(f"{k.upper()}={v}")
EOF
)

# ── Test ────────────────────────────────────────────────────────────────────────
if [[ "$ACTION" == "test" ]]; then
  log "running test suite"
  pytest -q tests
  exit $?
fi

# ── Preflight only (no login, no package) ───────────────────────────────────────
if [[ "$ACTION" == "preflight" ]]; then
  mcp-plugin-gen preflight -c "$CONFIG" "${EXTRA[@]}"
  exit $?
fi

# ── Validate only ───────────────────────────────────────────────────────────────
if [[ "$ACTION" == "validate" ]]; then
  PKG="$(ls -d dist/*/ 2>/dev/null | head -1 || true)"
  [[ -n "$PKG" ]] || die "no package in dist/ — run a build first"
  mcp-plugin-gen validate "$PKG"
  exit $?
fi

# ── Resolve effective OAuth mode & SSO (for prerequisite checks) ─────────────────
EFF_MODE="$MODE"
[[ -n "$EFF_MODE" ]] || EFF_MODE="$(python - "$CONFIG" <<'EOF'
import sys, yaml; print((yaml.safe_load(open(sys.argv[1])) or {}).get("oauth", {}).get("mode", "auth_code"))
EOF
)"
SSO_ON="$(python - "$CONFIG" <<'EOF'
import sys, yaml; print(str((yaml.safe_load(open(sys.argv[1])) or {}).get("sso", {}).get("enabled", False)).lower())
EOF
)"
[[ -n "$SSO_FLAG" ]] && SSO_ON="false"

case "$EFF_MODE" in
  auth_code|client_creds)
    [[ -n "${OAUTH_CLIENT_SECRET:-}" ]] || log "WARNING: credentials.oauth_client_secret is empty (ok only for a public PKCE client)" ;;
  token)
    [[ -n "${OAUTH_TOKEN:-}" ]] || die "oauth.mode=token requires credentials.oauth_token in $CONFIG" ;;
  api_key)
    [[ -n "${OAUTH_CLIENT_SECRET:-}${API_KEY:-}" ]] || die "oauth.mode=api_key requires credentials.oauth_client_secret (basic) or credentials.api_key" ;;
esac

if [[ "$EFF_MODE" == "auth_code" && "$SSO_ON" == "true" ]]; then
  for v in SSO_MOBILE SSO_ID SSO_OTP; do
    [[ -n "${!v:-}" ]] || die "SSO auto-login enabled but credentials.${v,,} is empty in $CONFIG (fill it, or use --no-sso)"
  done
  if ! python -c "import playwright" 2>/dev/null; then
    log "installing Playwright for SSO auto-login"
    pip install -q -e '.[sso]'
  fi
  if [[ -z "${PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD:-}" ]] && ! python -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p: p.chromium.launch(headless=True).close()" 2>/dev/null; then
    log "installing Chromium for Playwright"
    python -m playwright install chromium
    python -m playwright install-deps chromium 2>/dev/null || true
  fi
fi

# ── Build / discover ────────────────────────────────────────────────────────────
ARGS=(-c "$CONFIG")
[[ -n "$MODE" ]] && ARGS+=(--set "oauth.mode=$MODE")
# shellcheck disable=SC2206
[[ -n "$SSO_FLAG" ]] && ARGS+=($SSO_FLAG)
ARGS+=("${EXTRA[@]}")

if [[ "$ACTION" == "patch" ]]; then
  log "patching metadata from $CONFIG (no login)"
  set +e; mcp-plugin-gen patch -c "$CONFIG" "${EXTRA[@]}"; RC=$?; set -e
  PKG="$(ls -d dist/*/ 2>/dev/null | head -1 || true)"
  [[ -n "$PKG" && -f "$PKG/VALIDATION.md" ]] && cat "$PKG/VALIDATION.md"
  exit $RC
fi

if [[ "$ACTION" == "token" ]]; then
  log "logging in and printing a bearer token"
  mcp-plugin-gen token "${ARGS[@]}" --out dist/token.txt
  exit $?
fi

if [[ "$ACTION" == "discover" ]]; then
  log "discovering tools (mode=$EFF_MODE)"
  mcp-plugin-gen discover "${ARGS[@]}"
  exit $?
fi

log "building plugin (mode=$EFF_MODE, sso=$SSO_ON)"
set +e
mcp-plugin-gen build "${ARGS[@]}" $SMOKE
RC=$?
set -e

echo
PKG="$(ls -d dist/*/ 2>/dev/null | head -1 || true)"
if [[ -n "$PKG" && -f "$PKG/VALIDATION.md" ]]; then
  cat "$PKG/VALIDATION.md"
fi
ZIP="$(ls -t dist/*.zip 2>/dev/null | head -1 || true)"
if [[ $RC -eq 0 && -n "$ZIP" ]]; then
  log "✅ package ready: $ZIP"
  log "next: upload the zip in the OpenAI developer portal (Create plugin → With MCP) and enter the client secret there; see $PKG/SUBMISSION.md"
else
  log "❌ build finished with issues (exit $RC) — see above${PKG:+ / ${PKG}VALIDATION.md}"
fi
exit $RC
