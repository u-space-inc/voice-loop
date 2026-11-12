"""voice-loop の見張り（Hermes の cron の monitor script）。

毎回、メール・Slack（自分宛て）・録音・近い予定の「新しい順の数件」を、変わらない形の行で出す。
出力が前回と同じなら、Hermes は Agent を起こさない（費用がかからない）。新しいものが来たときだけ、
差分（+ の行）が Agent の指示に入る。

決まり:
- 行には、あとから変わる値（既読・ラベル・いまの時刻など）を入れない。入れると、何も来ていないのに起きる。
- 行は出来事の目印（ID）と、振り分けに要る最小の手がかり（差出人・場所・題の頭）だけ。本文は Agent が道具で読む。
- 読むだけ。書き込みの道具は呼ばない。鍵の更新もしない（Hermes 本体が持つ）。
- 読めなかった口は「<口> 読めない: <理由の種類>」の 1 行にする（理由の種類は変わらない語にする）。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vl_mcp  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
PY = sys.executable
GAPI = str(Path(vl_mcp.HOME) / "skills/productivity/google-workspace/scripts/google_api.py")
N_MAIL, N_SLACK, N_PLAUD = 15, 15, 5
MAIL_QUERY = "newer_than:3d -in:sent -in:drafts -in:chats -category:promotions"


def clip(s: str, n: int) -> str:
    s = " ".join(str(s or "").split())
    s = s.replace("|", "/")
    return s if len(s) <= n else s[: n - 1] + "…"


def gapi(*args: str):
    r = subprocess.run([PY, GAPI, *args], capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        raise RuntimeError("google")
    return json.loads(r.stdout or "[]")


def mail() -> list[str]:
    ms = gapi("gmail", "search", MAIL_QUERY, "--max", str(N_MAIL))
    ms = ms if isinstance(ms, list) else ms.get("messages", [])
    out = []
    for m in ms:
        frm = m.get("from") or ""
        addr = (re.findall(r"<([^>]+)>", frm) or [frm])[0]
        out.append(f"mail {m.get('id')} | {clip(addr, 60)} | {clip(m.get('subject'), 50)}")
    return out


def calendar() -> list[str]:
    today = dt.datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    evs = gapi("calendar", "list", "--start", today.isoformat(), "--end", (today + dt.timedelta(days=2)).isoformat(),
               "--max", "30")
    evs = evs if isinstance(evs, list) else evs.get("events", [])
    out = []
    for e in evs:
        st = e.get("start")
        st = st.get("dateTime") or st.get("date") if isinstance(st, dict) else st
        out.append(f"cal {e.get('id')} {e.get('updated') or ''} | {clip(st, 25)} | {clip(e.get('summary'), 40)}")
    return out


_BLOCK = re.compile(r"^### Result .*?$(.*?)(?=^### Result |\Z)", re.M | re.S)


def _slack_items(text: str) -> list[tuple[str, str, str, str]]:
    try:
        text = json.loads(text).get("results", text)
    except Exception:  # noqa: BLE001
        pass
    items = []
    for b in _BLOCK.findall(text):
        ch = re.search(r"^Channel: .*?\(ID: ([A-Z0-9]+)\)", b, re.M)
        ts = re.search(r"^Message_ts: (\d+\.\d+)", b, re.M)
        fr = re.search(r"^From: .*?\(ID: ([A-Z0-9]+)\)", b, re.M)
        tx = re.search(r"^Text: ?\n?(.*?)(?:^Context before:|^Context after:|\Z)", b, re.M | re.S)
        if ch and ts:
            items.append((ts.group(1), ch.group(1), fr.group(1) if fr else "?", tx.group(1) if tx else ""))
    return items


def slack() -> list[str]:
    after = str(int((dt.datetime.now(JST) - dt.timedelta(days=3)).timestamp()) // 86400 * 86400)
    seen = {}
    with vl_mcp.Client("slack") as c:
        for q in ("to:me", "is:dm"):
            t = c.call("slack_search_public_and_private", {
                "query": q, "channel_types": "public_channel,private_channel,mpim,im", "after": after,
                "sort": "timestamp", "limit": 20, "include_bots": False})
            for ts, ch, fr, tx in _slack_items(t):
                seen[(ch, ts)] = (fr, tx)
    rows = sorted(seen.items(), key=lambda kv: float(kv[0][1]), reverse=True)[:N_SLACK]
    return [f"slack {ch} {ts} | {fr} | {clip(tx, 40)}" for (ch, ts), (fr, tx) in rows]


def plaud() -> list[str]:
    with vl_mcp.Client("plaud") as c:
        t = c.call("list_files", {"page_size": 10})
    body = t[t.find("{"): t.rfind("}") + 1]
    data = json.loads(body).get("data", [])
    data.sort(key=lambda f: f.get("start_at") or f.get("created_at") or "", reverse=True)
    return [f"plaud {f.get('id')} | {clip(f.get('start_at'), 16)} | {clip(f.get('name'), 40)}" for f in data[:N_PLAUD]]


def main() -> None:
    parts = []
    for name, fn in (("mail", mail), ("cal", calendar), ("slack", slack), ("plaud", plaud)):
        try:
            rows = fn()
        except vl_mcp.NeedsAuth:
            rows = [f"{name} 読めない: 鍵の更新が要る（{name} の道具を一度使うと直る）"]
        except Exception:  # noqa: BLE001
            rows = [f"{name} 読めない: 取得に失敗"]
        parts.append(f"## {name}\n" + "\n".join(sorted(rows)))
    print("\n".join(parts))


if __name__ == "__main__":
    main()
