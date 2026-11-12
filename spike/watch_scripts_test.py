"""Hermes の見張りの回の台本（templates/hermes/watch/）の試験。ネット・本物の Hermes・Google・Slack・Plaud には触れない（偽物に差し替える）。
確かめること:
- vl_watch.py: 出力が安定している（同じ入力なら同じバイト、あとから変わる値を入れない）・Slack の検索結果の形を読む・口ごとの失敗を 1 行にする・
  行の区切り（|）や改行を中身に入れさせない。
- vl_mcp.py: 読むための道具の名前のほかは呼ばない（下書き・送信の道具を呼ぼうとすると止まる）。
- vl_items.py: 同じ出来事を二度記録しない・判断待ちの一覧・片づけた印・案件の一覧・字数で切る・壊れた行を飛ばす。
- vl_gmail_draft.py: 下書きの API だけを呼び、送信の API を呼ばない・返信の宛先と件名とスレッドを元のメールから組む。
使い方: uv run spike/watch_scripts_test.py"""
import importlib
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
W = ROOT / "templates/hermes/watch"
TMP = Path(tempfile.mkdtemp())
os.environ["HERMES_HOME"] = str(TMP)
sys.path.insert(0, str(W))
results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


import vl_mcp  # noqa: E402
import vl_watch  # noqa: E402

# ---------- vl_mcp: 読むための道具だけ ----------
c = vl_mcp.Client.__new__(vl_mcp.Client)
c.server, c.url, c.token, c.sid, c.n = "slack", "http://127.0.0.1:9", "t", None, 0
c._rpc = lambda method, params=None: {"content": [{"type": "text", "text": "ok"}]}
blocked = []
for name in ("slack_send_message", "slack_send_message_draft", "slack_schedule_message", "slack_create_canvas", "slack_add_reaction"):
    try:
        c.call(name, {})
        blocked.append(False)
    except PermissionError:
        blocked.append(True)
check("vl_mcp: 送信・下書き・予約・canvas・リアクションの道具は呼ばない", blocked, [True] * 5)
check("vl_mcp: 読む道具は呼べる", c.call("slack_read_thread", {}), "ok")
try:
    vl_mcp._token("plaud")
    check("vl_mcp: 鍵が無ければ NeedsAuth", False, True)
except vl_mcp.NeedsAuth:
    check("vl_mcp: 鍵が無ければ NeedsAuth", True, True)

# ---------- vl_watch: Slack の検索結果の形 ----------
SLACK = json.dumps({"results": """# Search Results for: to:me

## Messages (2 results)
### Result 1 of 2
Channel: DM (ID: D0AAA)
Participants: 山田 (ID: U0AAA), 自分 (ID: U0ME)
From:  (ID: U0AAA)
Time: 2026-11-12 10:47:33 JST
Message_ts: 1794448053.127939
Permalink: [link](https://example.slack.com/archives/D0AAA/p1794448053127939)
Text:
見積もりの件 | 確認お願いします
二行目
Context before:
- From: 自分 (ID: U0ME) Message_ts: 1794440000.000001 Text: 前の話
### Result 2 of 2
Channel: #shop (ID: C0BBB)
From: 佐藤 (ID: U0BBB)
Time: 2026-11-12 09:00:00 JST
Message_ts: 1794440400.000100
Text: <@U0ME> 明日の打合せの時間を教えてください
"""}, ensure_ascii=False)
items = vl_watch._slack_items(SLACK)
check("Slack: 結果ごとにチャンネル・ts・送り手・本文を読む（前後の文脈の ts は拾わない）",
      [(ch, ts, fr) for ts, ch, fr, _ in items], [("D0AAA", "1794448053.127939", "U0AAA"), ("C0BBB", "1794440400.000100", "U0BBB")])
check("Slack: 本文の頭は 1 行にそろえ、区切りの | を入れない", vl_watch.clip(items[0][3], 40), "見積もりの件 / 確認お願いします 二行目")

# ---------- vl_watch: 出力の形と安定 ----------
vl_watch.mail = lambda: ["mail m2 | b@example.com | 件名|二", "mail m1 | a@example.com | 件名一"]
vl_watch.calendar = lambda: ["cal e1 2026-11-12T00:00:00Z | 2026-11-13T10:00 | 定例"]
vl_watch.slack = lambda: [f"slack {ch} {ts} | {fr} | {vl_watch.clip(tx, 40)}" for ts, ch, fr, tx in items]


def boom():
    raise RuntimeError("x")


def noauth():
    raise vl_mcp.NeedsAuth("x")


vl_watch.plaud = noauth


def out():
    import io
    from contextlib import redirect_stdout
    f = io.StringIO()
    with redirect_stdout(f):
        vl_watch.main()
    return f.getvalue()


a, b = out(), out()
check("出力は同じ入力なら同じバイト", a == b, True)
check("口ごとに見出しを付け、行は並べ替える", a.splitlines()[:3], ["## mail", "mail m1 | a@example.com | 件名一", "mail m2 | b@example.com | 件名|二"])
check("鍵の更新が要る口は、変わらない 1 行にする", "plaud 読めない: 鍵の更新が要る（plaud の道具を一度使うと直る）" in a, True)
vl_watch.mail = boom
check("取得に失敗した口は、変わらない 1 行にする（理由の細部を入れない）", "mail 読めない: 取得に失敗" in out(), True)
check("clip: 長い文は … で切る・改行と | を入れない", (vl_watch.clip("あ" * 60, 50)[-1], len(vl_watch.clip("あ" * 60, 50)), "|" in vl_watch.clip("a|b", 10), "\n" in vl_watch.clip("a\nb", 10)),
      ("…", 50, False, False))
check("出力に「いまの時刻」を入れない（時刻の書式の行が無い）", any(":" in l and l.startswith("## ") for l in a.splitlines()), False)

# ---------- vl_items ----------
PY = [sys.executable, str(W / "vl_items.py")]
run = lambda *a: subprocess.run(PY + list(a), capture_output=True, text=True, env={**os.environ, "HERMES_HOME": str(TMP)})
check("items: 初めての出来事は new（終わりの数 1）", (run("seen", "mail", "m1").stdout.strip(), run("seen", "mail", "m1").returncode), ("new", 1))
r = run("add", "--source", "mail", "--id", "m1", "--project", "shop", "--kind", "judge", "--title", "見積もりの返事", "--note", "金額の判断", "--prepared", "返信の下書き")
check("items: 記録して番号を返す", r.stdout.strip(), "記録しました [1]")
check("items: 同じ出来事は二度記録しない", run("add", "--source", "mail", "--id", "m1", "--kind", "info", "--title", "x").stdout.strip(), "既に記録にあります（追加しません）")
check("items: 記録した出来事は seen（終わりの数 0）", (run("seen", "mail", "m1").stdout.strip(), run("seen", "mail", "m1").returncode), ("seen", 0))
run("add", "--source", "slack", "--id", "C1/1.2", "--kind", "info", "--title", "連絡")
run("add", "--source", "plaud", "--id", "p1", "--kind", "baseline", "--title", "初回の既読扱い")
o = run("open").stdout
check("items: 判断待ちの一覧に、要ることと準備したことを出す", ("[1]" in o, "金額の判断" in o, "返信の下書き" in o, "連絡" in o), (True, True, True, False))
check("items: 案件の一覧（既読扱いは数えない・案件が無ければ未振り分け）", run("projects").stdout.splitlines(), ["shop（1）", "未振り分け（1）"])
check("items: 片づけた印", (run("done", "1").stdout.strip(), run("open").stdout.strip()), ("片づけた印を付けました [1]", "判断待ちはありません"))
check("items: 無い番号は断る", run("done", "99").returncode, 1)
check("items: 口と種類は決めたものだけ", (run("add", "--source", "web", "--id", "x", "--kind", "info", "--title", "x").returncode != 0,
                                    run("add", "--source", "mail", "--id", "x", "--kind", "send", "--title", "x").returncode != 0), (True, True))
run("add", "--source", "mail", "--id", "m9", "--kind", "judge", "--title", "長" * 200 + "\n改行")
row = [json.loads(l) for l in (TMP / "voice-loop/items.jsonl").read_text().splitlines() if '"m9"' in l][0]
check("items: 題は 80 字で切り、1 行にする", (len(row["title"]), "\n" in row["title"]), (80, False))
with open(TMP / "voice-loop/items.jsonl", "a") as f:
    f.write("壊れた行\n[1]\n")
check("items: 壊れた行は飛ばして読む", run("open").returncode, 0)

# ---------- vl_gmail_draft: 下書きだけ ----------
CALLS = []


class Exec:
    def __init__(self, name, ret):
        self.name, self.ret = name, ret

    def execute(self):
        CALLS.append(self.name)
        return self.ret


class Msgs:
    def get(self, **k):
        return Exec("messages.get", {"threadId": "th1", "payload": {"headers": [
            {"name": "From", "value": "山田 <yamada@example.com>"}, {"name": "Subject", "value": "見積もり"}, {"name": "Message-ID", "value": "<m@example.com>"}]}})

    def send(self, **k):
        return Exec("messages.send", {})


class Drafts:
    def create(self, userId, body):
        CALLS.append(("drafts.create", body))
        return Exec("drafts.create.exec", {"id": "d1", "message": {"id": "x1", "threadId": body["message"].get("threadId")}})


class Users:
    def messages(self):
        return Msgs()

    def drafts(self):
        return Drafts()


fake = types.ModuleType("google_api")
fake.build_service = lambda api, ver: types.SimpleNamespace(users=lambda: Users())
sys.modules["google_api"] = fake
import vl_gmail_draft  # noqa: E402
import base64  # noqa: E402
import email  # noqa: E402
import email.policy  # noqa: E402
import io  # noqa: E402
from contextlib import redirect_stdout  # noqa: E402

sys.argv = ["vl_gmail_draft.py", "--reply-to", "m1", "--body", "承知しました"]
f = io.StringIO()
with redirect_stdout(f):
    vl_gmail_draft.main()
made = [x for x in CALLS if isinstance(x, tuple)][0][1]
msg = email.message_from_bytes(base64.urlsafe_b64decode(made["message"]["raw"]), policy=email.policy.default)  # 日本語の見出しは符号化されて入る
msg_body = msg.get_content().strip()
check("draft: 送信の API は呼ばず、下書きだけを作る", ("messages.send" in CALLS, any(isinstance(x, tuple) and x[0] == "drafts.create" for x in CALLS)), (False, True))
check("draft: 返信の宛先・件名・スレッド・参照を元のメールから組む",
      (msg["To"], msg["Subject"], made["message"]["threadId"], msg["In-Reply-To"]), ("山田 <yamada@example.com>", "Re: 見積もり", "th1", "<m@example.com>"))
check("draft: 本文はそのまま", msg_body, "承知しました")
check("draft: 結果は 1 行の JSON", json.loads(f.getvalue()), {"draft_id": "d1", "message_id": "x1", "thread_id": "th1"})

# ---------- 写しと本物の置き場の差（ひな形が古くならないように） ----------
names = sorted(p.name for p in W.glob("*.py"))
check("ひな形の台本は 4 つ", names, ["vl_gmail_draft.py", "vl_items.py", "vl_mcp.py", "vl_watch.py"])
check("手順のひな形がある", (W / "voice-loop-watch/SKILL.md").is_file(), True)

ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
