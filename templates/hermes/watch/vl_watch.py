"""voice-loop の見張り（Hermes の cron の monitor script）。

毎回、メール・Slack（自分宛て）・録音・近い予定の「新しい順の数件」を、変わらない形の行で出す。
出力が前回と同じなら、Hermes は Agent を起こさない（費用がかからない）。新しいものが来たときだけ、
差分（+ の行）が Agent の指示に入る。

決まり:
- 行には、あとから変わる値（既読・ラベル・いまの時刻など）を入れない。入れると、何も来ていないのに起きる。
- 行は出来事の目印（ID と、構造のある時刻）だけ。相手が書ける文（件名・差出人の名前・本文・題）は入れない（Agent の指示に入るため）。
  中身は Agent が道具で読む。
- 読むだけ。書き込みの道具は呼ばない。鍵の更新もしない（Hermes 本体が持つ）。
- 読めなかった口は「<口> 読めない: <理由の種類>」の 1 行にする（理由の種類は変わらない語にする）。
- 行の 2 つ目の語は、そのまま記録（vl_items.py）の id にする（Agent に組み立てさせない）。
  mail <id> ／ slack <チャンネル>/<ts> <送り手> ／ cal <id>@<開始> ／ plaud <id> | <録音の開始> ／ teams <id>
- 予定の一覧は更新時刻を返さないので、予定の id には開始の時刻を入れる（時刻が動いたら別の出来事になる）。
  招待を送る側は予定の id を選べるので、形の合わない予定は 1 件ずつ外し、口ごとは止めない（外したことは 1 行で出す）。
- 行の形は vl_items.py の _LINE と同じにする（変えるときは両方を変える）。
- Teams は、Mac の側（voice-loop の scripts/teams_watch.py）が Chrome で読んで voice-loop/teams.jsonl に書いたものを読む（この台本は Teams に触れない）。
  Mac の側を使っていなければ（teams-status.json が無ければ）、Teams の口は何も出さない。
- Slack は利用者本人の発言を拾わない（-from:me）。拾うと、Hermes との DM で話すたびに Agent が起きる。
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
SLACK_QUERIES = ("to:me -from:me", "is:dm -from:me")
CAL_ODD = "cal 読めない: 形の合わない予定がある（その予定だけ見張れない）"
N_TEAMS = 15
TEAMS_STALE = 2 * 3600  # Mac の側は時間の外でも 30 分ごとに印を書く。これより古ければ止まっている
TEAMS_REASON = {"signin": "サインインが要る（Mac の Chrome で Teams を開き、サインインし直す）",
                "org": "組織の一覧を開けない（teams.json の組織の名前と ID を確かめる）",
                "parse": "一覧の形が読めない（Chrome の Teams で、左の「メンション」を選んだままにする）",
                "run": "取得に失敗"}
_HEX16 = re.compile(r"[0-9a-f]{16}")


_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff]")


def clip(s: str, n: int) -> str:
    s = " ".join(_CTRL.sub(" ", str(s or "")).split())
    s = s.replace("|", "/")
    return s if len(s) <= n else s[: n - 1] + "…"


_ID = re.compile(r"[A-Za-z0-9_.@:+=-]{1,200}")


def ident(v) -> str:
    """ID として使える文字だけの値か確かめる（行の形を崩させない）。だめなら例外。"""
    v = str(v or "")
    if not _ID.fullmatch(v):  # match と $ だと、末尾の改行を通してしまう
        raise ValueError("id")
    return v


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
        out.append(f"mail {ident(m.get('id'))}")
    return out


def calendar() -> list[str]:
    today = dt.datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    evs = gapi("calendar", "list", "--start", today.isoformat(), "--end", (today + dt.timedelta(days=2)).isoformat(),
               "--max", "30")
    evs = evs if isinstance(evs, list) else evs.get("events", [])
    out, odd = [], False
    for e in evs:
        st = e.get("start")
        st = st.get("dateTime") or st.get("date") if isinstance(st, dict) else st
        try:
            out.append("cal " + ident(ident(e.get("id")) + "@" + ident(st or "-")))  # つないだ id も 200 字まで
        except ValueError:
            odd = True
    if odd:
        out.append(CAL_ODD)
    return out


_HEAD = re.compile(r"^## Messages \((\d+) results?\)\s*$", re.M)
_RESULT = re.compile(r"^### Result (\d+) of (\d+)\s*$", re.M)


class Garbled(Exception):
    """検索結果の見出しの並びが崩れている（本文に見出しをまねた行がある、など）。どれが本物か決められないので、口ごと読めないにする。"""


def _slack_items(text: str) -> list[tuple[str, str, str]]:
    """Slack の MCP の検索結果（マークダウン）から (ts, チャンネル, 送り手)。
    本文は相手が書けるので、「## Messages (N results)」の数と「### Result i of N」の並び（1..N がちょうど一度ずつ）を照らし、
    合わなければ Garbled。各結果の項目は、その結果の見出しから「Text:」までの間（本文より前）だけから読む。"""
    try:
        text = json.loads(text).get("results", text)
    except Exception:  # noqa: BLE001
        pass
    if not isinstance(text, str):
        raise Garbled()
    head = _HEAD.search(text)
    heads = list(_RESULT.finditer(text))
    if not head:
        if heads:
            raise Garbled()
        return []
    n = int(head.group(1))
    if [(int(m.group(1)), int(m.group(2))) for m in heads] != [(i, n) for i in range(1, n + 1)] or heads and heads[0].start() < head.end():
        raise Garbled()
    items = []
    for k, m in enumerate(heads):
        end = heads[k + 1].start() if k + 1 < len(heads) else len(text)
        meta = text[m.end():end].split("\nText:", 1)[0]
        ch = re.search(r"^Channel: .*?\(ID: ([A-Z0-9]{1,40})\)\s*$", meta, re.M)
        ts = re.search(r"^Message_ts: (\d{1,12}\.\d{1,12})\s*$", meta, re.M)
        fr = re.search(r"^From: .*?\(ID: ([A-Z0-9]{1,40})\)\s*$", meta, re.M)
        if not (ch and ts):
            raise Garbled()
        items.append((ts.group(1), ch.group(1), fr.group(1) if fr else "-"))
    return items


def slack() -> list[str]:
    after = str(int((dt.datetime.now(JST) - dt.timedelta(days=3)).timestamp()) // 86400 * 86400)
    seen = {}
    with vl_mcp.Client("slack") as c:
        for q in SLACK_QUERIES:
            t = c.call("slack_search_public_and_private", {
                "query": q, "channel_types": "public_channel,private_channel,mpim,im", "content_types": "messages", "after": after,
                "sort": "timestamp", "limit": 20, "include_bots": False})
            for ts, ch, fr in _slack_items(t):
                seen[(ch, ts)] = fr
    rows = sorted(seen.items(), key=lambda kv: float(kv[0][1]), reverse=True)[:N_SLACK]
    return [f"slack {ch}/{ts} {fr}" for (ch, ts), fr in rows]


def plaud() -> list[str]:
    with vl_mcp.Client("plaud") as c:
        t = c.call("list_files", {"page_size": 10})
    body = t[t.find("{"): t.rfind("}") + 1]
    data = json.loads(body).get("data", [])
    data.sort(key=lambda f: f.get("start_at") or f.get("created_at") or "", reverse=True)
    return [f"plaud {ident(f.get('id'))} | {ident(f.get('start_at') or '-')}" for f in data[:N_PLAUD]]


def teams(now: float | None = None) -> list[str] | None:
    home = Path(vl_mcp.HOME) / "voice-loop"
    try:
        st = json.loads((home / "teams-status.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None  # Teams の見張りを使っていない（口ごと出さない）
    now = dt.datetime.now(JST).timestamp() if now is None else now
    rows = []
    if not isinstance(st, dict) or not isinstance(st.get("at"), (int, float)) or not isinstance(st.get("orgs"), dict):
        raise RuntimeError("teams")
    if now - st["at"] > TEAMS_STALE:
        rows.append("teams 読めない: Mac の見張りが 2 時間以上動いていない（Mac が止まっている・眠っている）")
    for o in st["orgs"].values():
        if isinstance(o, dict) and not o.get("ok"):
            rows.append("teams 読めない: " + TEAMS_REASON.get(o.get("reason"), TEAMS_REASON["run"]))
    items = []
    feed = home / "teams.jsonl"
    if feed.exists():
        for line in feed.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if (isinstance(r, dict) and not r.get("baseline") and isinstance(r.get("first_seen"), (int, float))
                    and now - r["first_seen"] < 3 * 86400 and _HEX16.fullmatch(str(r.get("id", "")))):
                items.append(r)
    items.sort(key=lambda r: r["first_seen"], reverse=True)
    rows += [f"teams {r['id']}" for r in items[:N_TEAMS]]
    return list(dict.fromkeys(rows))


def main() -> None:
    parts = []
    for name, fn in (("mail", mail), ("cal", calendar), ("slack", slack), ("plaud", plaud), ("teams", teams)):
        try:
            rows = fn()
            if rows is None:
                continue
        except vl_mcp.NeedsAuth:
            rows = [f"{name} 読めない: 鍵の更新が要る（{name} の道具を一度使うと直る）"]
        except Garbled:
            rows = [f"{name} 読めない: 結果の形が崩れている（本文に見出しをまねた行がある可能性。利用者に知らせる）"]
        except Exception:  # noqa: BLE001
            rows = [f"{name} 読めない: 取得に失敗"]
        parts.append(f"## {name}\n" + "\n".join(sorted(rows)))
    print("\n".join(parts))


if __name__ == "__main__":
    main()
