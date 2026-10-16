"""Minimal MCP Streamable-HTTP client: initialize, tools/list (paginated), prompts/list,
resources/list, and optional tools/call smoke tests. Handles JSON and SSE responses."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

from .config import McpConfig

PROTOCOL = "2025-06-18"


class McpError(RuntimeError):
    pass


@dataclass
class ServerSurface:
    server_info: dict = field(default_factory=dict)
    capabilities: dict = field(default_factory=dict)
    instructions: str = ""
    tools: list[dict] = field(default_factory=list)
    prompts: list[dict] = field(default_factory=list)
    resources: list[dict] = field(default_factory=list)
    protocol_version: str = ""
    auth_required: bool = False
    oauth_md: dict = field(default_factory=dict)   # discovered OAuth metadata (for mcp.json / patch)


class McpClient:
    def __init__(self, mcp: McpConfig, token: Optional[str] = None, log: Callable[[str], None] = print):
        self.mcp = mcp
        self.token = token
        self.session_id: Optional[str] = None
        self.log = log
        self._id = 0
        self._c = httpx.Client(timeout=mcp.timeout, verify=mcp.verify_tls)

    def close(self):
        self._c.close()

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": PROTOCOL}
        h.update(self.mcp.extra_headers)
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    @staticmethod
    def _parse(r: httpx.Response) -> Any:
        ct = r.headers.get("content-type", "")
        text = r.text
        if "text/event-stream" in ct or text.lstrip().startswith(("event:", "data:")):
            datas = [l[5:].strip() for l in text.splitlines() if l.startswith("data:")]
            msgs = [json.loads(d) for d in datas if d]
            # return the last message that carries a result/error for our id
            for m in reversed(msgs):
                if isinstance(m, dict) and ("result" in m or "error" in m):
                    return m
            return msgs[-1] if msgs else {}
        return r.json() if text.strip() else {}

    def request(self, method: str, params: Optional[dict] = None) -> Any:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        r = self._c.post(self.mcp.url, json=body, headers=self._headers())
        if r.status_code == 401:
            raise McpError(f"401 Unauthorized from MCP server (WWW-Authenticate: "
                           f"{r.headers.get('WWW-Authenticate', '-')})")
        if r.status_code >= 400:
            raise McpError(f"HTTP {r.status_code} on {method}: {r.text[:300]}")
        sid = r.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        msg = self._parse(r)
        if isinstance(msg, dict) and "error" in msg:
            raise McpError(f"{method} → {msg['error']}")
        return msg.get("result", msg) if isinstance(msg, dict) else msg

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        try:
            self._c.post(self.mcp.url, json=body, headers=self._headers())
        except Exception:
            pass

    def _list_all(self, method: str, key: str) -> list[dict]:
        out, cursor = [], None
        while True:
            res = self.request(method, {"cursor": cursor} if cursor else {})
            out += res.get(key, [])
            cursor = res.get("nextCursor")
            if not cursor:
                return out

    def discover(self, smoke_test_reads: bool = False) -> ServerSurface:
        s = ServerSurface()
        init = self.request("initialize", {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "mcp-plugin-gen", "version": "1.0.0"}})
        s.server_info = init.get("serverInfo", {})
        s.capabilities = init.get("capabilities", {})
        s.instructions = init.get("instructions", "") or ""
        s.protocol_version = init.get("protocolVersion", "")
        self.notify("notifications/initialized")
        self.log(f"[mcp] connected: {s.server_info.get('name', '?')} {s.server_info.get('version', '')} "
                 f"(protocol {s.protocol_version})")
        s.tools = self._list_all("tools/list", "tools")
        self.log(f"[mcp] {len(s.tools)} tools")
        if "prompts" in s.capabilities:
            try:
                s.prompts = self._list_all("prompts/list", "prompts")
            except McpError:
                pass
        if "resources" in s.capabilities:
            try:
                s.resources = self._list_all("resources/list", "resources")
            except McpError:
                pass
        if smoke_test_reads:
            self._smoke(s)
        return s

    def _smoke(self, s: ServerSurface) -> None:
        """Call read-only tools that have no required args, to confirm they answer."""
        for t in s.tools:
            ann = t.get("annotations") or {}
            req = (t.get("inputSchema") or {}).get("required") or []
            if ann.get("readOnlyHint") and not req:
                try:
                    res = self.request("tools/call", {"name": t["name"], "arguments": {}})
                    t["_smoke"] = "ok" if not res.get("isError") else f"isError: {str(res)[:120]}"
                except McpError as e:
                    t["_smoke"] = f"error: {e}"
                self.log(f"[mcp] smoke {t['name']}: {t['_smoke']}")
        import re as _re
        need = sorted({m for t in s.tools for m in _re.findall(r"needs permission ([\w:.-]+)", str(t.get("_smoke", "")))})
        if need:
            self.log(f"[mcp] HINT: gateway enforces scopes {need} — add them to oauth.scopes and assign them to the "
                     f"Keycloak client (scripts/keycloak_setup.sh API_SCOPES='{' '.join(need)}')")


def probe_auth_required(mcp: McpConfig) -> bool:
    """True if the server rejects unauthenticated initialize with 401."""
    try:
        with httpx.Client(timeout=mcp.timeout, verify=mcp.verify_tls) as c:
            r = c.post(mcp.url, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                      "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                                                 "clientInfo": {"name": "probe", "version": "0"}}},
                       headers={"Accept": "application/json, text/event-stream"})
            return r.status_code == 401
    except Exception:
        return False
