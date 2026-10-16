#!/usr/bin/env python3

import argparse, json, sys, urllib.error, urllib.request

PROTO = "2025-06-18"

def rpc(url, token, method, params, session=None, _id=1):
    msg = {"jsonrpc": "2.0", "method": method, "params": params}
    if _id is not None:              # JSON-RPC notifications carry NO id field at all
        msg["id"] = _id
    body = json.dumps(msg).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if session:
        req.add_header("Mcp-Session-Id", session)
    with urllib.request.urlopen(req, timeout=30) as r:
        sid = r.headers.get("Mcp-Session-Id", session)
        raw = r.read().decode()
    if _id is None:                  # notification: 202/204 with empty body is the expected answer
        return {}, sid
    # Streamable HTTP may answer as SSE; take the last data: line
    if raw.lstrip().startswith("event:") or "\ndata:" in raw or raw.startswith("data:"):
        data = [l[5:].strip() for l in raw.splitlines() if l.startswith("data:")]
        raw = data[-1] if data else "{}"
    return json.loads(raw), sid

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=None, required=True, help="MCP endpoint URL")
    ap.add_argument("--token", default=None, help="OAuth bearer token (optional for public tools)")
    ap.add_argument("--out", default="tools.json")
    a = ap.parse_args()

    init, sid = rpc(a.url, a.token, "initialize", {
        "protocolVersion": PROTO, "capabilities": {},
        "clientInfo": {"name": "plugin-discovery", "version": "1.0.0"}}, _id=1)
    if "error" in init:
        sys.exit(f"initialize failed: {init['error']}")
    try:
        rpc(a.url, a.token, "notifications/initialized", {}, sid, _id=None)
    except urllib.error.HTTPError as e:
        print(f"warning: notifications/initialized answered HTTP {e.code} (server should accept it with 202/204)", file=sys.stderr)
    tools, cursor = [], None
    while True:
        res, sid = rpc(a.url, a.token, "tools/list", {"cursor": cursor} if cursor else {}, sid, _id=2)
        if "error" in res:
            sys.exit(f"tools/list failed: {res['error']}")
        tools += res["result"].get("tools", [])
        cursor = res["result"].get("nextCursor")
        if not cursor:
            break

    json.dump({"server": init.get("result", {}).get("serverInfo"), "tools": tools},
              open(a.out, "w"), indent=2, ensure_ascii=False)

    print(f"\n{len(tools)} tools — written to {a.out}\n")
    print("| Tool | Auth | Use for |\n|------|------|---------|")
    for t in tools:
        ann = t.get("annotations", {}) or {}
        kind = "write" if not ann.get("readOnlyHint", False) else "read"
        if ann.get("destructiveHint"):
            kind = "write · destructive"
        desc = (t.get("description") or "").strip().split("\n")[0][:90]
        print(f"| `{t['name']}` | {kind} | {desc} |")

    missing = [t["name"] for t in tools if not (t.get("annotations") or {}).get("readOnlyHint", False)
               and "readOnlyHint" not in (t.get("annotations") or {})]
    if missing:
        print("\nWARNING — tools without safety annotations (OpenAI review requires them):")
        for n in missing:
            print("  -", n)

if __name__ == "__main__":
    main()
