"""voice-loop の見張り（cron の monitor script）から、Hermes がつないだ MCP（Slack・Plaud）を読むための小さな道具。

- 鍵は Hermes の置き場（HERMES_HOME/mcp-tokens/<名前>.json）の access_token をそのまま使う。更新（refresh）はしない。
  更新は Hermes 本体が持つ（ここで更新すると、Hermes の持つ refresh_token と競り合って壊れるため）。
- 読むための呼び出しだけに使う。書き込みの道具は呼ばない（呼べる名前を READ_TOOLS に絞る）。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME") or "/opt/data")
URLS = {"slack": "https://mcp.slack.com/mcp", "plaud": "https://mcp.plaud.ai/mcp"}
READ_TOOLS = {
    "slack": {"slack_search_public_and_private", "slack_list_user_channels", "slack_read_channel",
              "slack_read_thread", "slack_search_users", "slack_read_user_profile"},
    "plaud": {"list_files", "get_file", "get_note", "get_current_user"},
}


class NeedsAuth(Exception):
    """鍵が無い・切れている（Hermes が使えば更新される）。"""


def _token(server: str) -> str:
    try:
        return json.loads((HOME / "mcp-tokens" / f"{server}.json").read_text())["access_token"]
    except Exception as e:  # noqa: BLE001
        raise NeedsAuth(f"{server}: no token") from e


def _post(url: str, token: str, body: dict, sid: str | None):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
         "Authorization": f"Bearer {token}", "MCP-Protocol-Version": "2025-06-18"}
    if sid:
        h["Mcp-Session-Id"] = sid
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=40)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise NeedsAuth(f"http {e.code}") from e
        raise
    sid = r.headers.get("Mcp-Session-Id") or sid
    raw = r.read().decode("utf-8", "replace")
    if not raw.strip():
        return None, sid
    if "text/event-stream" in (r.headers.get("Content-Type") or ""):
        msgs = [json.loads(l[5:]) for l in raw.splitlines() if l.startswith("data:") and l[5:].strip()]
        msg = next((m for m in reversed(msgs) if "result" in m or "error" in m), None)
    else:
        msg = json.loads(raw)
    return msg, sid


class Client:
    def __init__(self, server: str):
        self.server, self.url, self.token, self.sid, self.n = server, URLS[server], _token(server), None, 0

    def _rpc(self, method: str, params: dict | None = None):
        self.n += 1
        msg, self.sid = _post(self.url, self.token, {"jsonrpc": "2.0", "id": self.n, "method": method,
                                                      "params": params or {}}, self.sid)
        if msg and "error" in msg:
            raise RuntimeError(f"{self.server} {method}: {msg['error']}")
        return (msg or {}).get("result")

    def __enter__(self):
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "voice-loop-watch", "version": "0.1"}})
        _post(self.url, self.token, {"jsonrpc": "2.0", "method": "notifications/initialized"}, self.sid)
        return self

    def __exit__(self, *a):
        return False

    def tools(self):
        return self._rpc("tools/list")["tools"]

    def call(self, name: str, args: dict) -> str:
        if name not in READ_TOOLS[self.server]:
            raise PermissionError(f"{self.server}.{name} は読むための道具ではない")
        res = self._rpc("tools/call", {"name": name, "arguments": args}) or {}
        if res.get("isError"):
            raise RuntimeError(f"{self.server}.{name}: " + " ".join(c.get("text", "") for c in res.get("content", []))[:300])
        sc = res.get("structuredContent")
        if sc is not None:
            return json.dumps(sc, ensure_ascii=False)
        return "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
