"""画面のボタンからの返答（/api/prompt・/api/answer）の試験。止まっている画面を作り物にし、送るキーと断る理由を確かめる。承認の許可・断ると、選択式の質問に前の答え方（番号だけを送る choose。0.26.1 で外した）では送らないこと。
herdr には触れない（一覧・画面の読み取り・キーの送信を差し替える）。使い方: uv run spike/answer_test.py"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state")  # 本物の設定フォルダを読まない
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server as s  # noqa: E402

PERMISSION = """⏺ 一覧を見ます。
⏺ Bash(ls -la)
  ⎿  Running…
 Bash command
   ls -la
   フォルダの中身を見る
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for ls commands in /Users/me/proj
   3. No, and tell Claude what to do differently (esc)
 Esc to cancel · Tab to amend"""
RISKY = PERMISSION.replace("ls -la", "rm -rf build").replace("ls commands", "rm commands")
QUESTION = """⏺ どちらで進めますか？
 どちらの方式で進めますか？
 ❯ 1. A 案
     速い
   2. B 案
     安全
   3. Type something.
 Enter to select · ↑/↓ to navigate · Esc to cancel"""
UNKNOWN = "⏺ 考えています\n  何か入力してください"
# 2026-10-21 に本物の Claude Code で取った画面と同じ形（場所だけ架空に置き換えた）
REAL_WRITE = """⏺ Write(/Users/me/proj/shape.txt)
 Create file
 ../shape.txt
  1 ok
 Do you want to create shape.txt?
 ❯ 1. Yes
   2. Yes, and switch to accept edits (auto-approve file edits and common file commands) for this session; Yes,
      and always allow access to
      /Users/me/proj for this
      session (shift+tab)
   3. No
 Esc to cancel · Tab to amend"""
REAL_YESNO = """⏺ Write(/Users/me/proj/shape.txt)
  ⎿  User rejected write to /Users/me/proj/shape.txt
 ☐ 確認
続けますか？
❯ 1. Yes
     続けて作成を進める
  2. No
     作成を中止する
  3. Type something.
  4. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel"""
YESNO_SHORT = """⏺ どうしますか
 続けますか？
 ❯ 1. Yes
   2. No
   3. Type something.
 Enter to select · ↑/↓ to navigate · Esc to cancel"""  # Codex の再現と同じ形
from question_screens import REAL_CHOICE, REAL_MULTI, REAL_TWO  # noqa: E402  2026-11-05 に本物の Claude Code で取った画面

AGENT = {"pane": "w9:p1", "title": "テスト", "cwd": "/tmp/proj", "kind": "claude", "session": "sess-1",
         "status": "blocked", "name": "", "hub": False}
state = {"status": "blocked", "screen": PERMISSION, "trust": False, "sent": []}
s.list_agents = lambda: [{**AGENT, "status": state["status"]}]
s.pane_shows_trust = lambda pane: state["trust"]
REAL_READ = s.read_prompt  # 本物の read_prompt（herdr を呼ぶところだけ差し替えて確かめる）
s.read_prompt = lambda pane: state["screen"]
s.read_dialog = lambda pane, *_: (_ for _ in ()).throw(RuntimeError("この試験では読まない"))  # 1 問ずつの読み取りは answer_form_test・form_ui_test で確かめる（本物の herdr を呼ばない）
s.herdr = lambda *a, **k: (state["sent"].append(a[-1]), {"result": {}})[1] if a[:2] == ("agent", "send-keys") else {"result": {}}


def prompt(key="sess-1"):
    return s._asked("w9:p1", key)[1]


def answer(keep=False, **body):
    """keep=False なら、前に答えた記録を消してから送る（答えたあと相手の画面が変わった、の代わり）。"""
    state["sent"].clear()
    if not keep:
        s.ANSWERED.clear()
    r = json.loads(asyncio.run(s.api_answer({"pane": "w9:p1", "key": "sess-1", **body})).body)
    return r, list(state["sent"])


def case(name, **st):
    state.update({"status": "blocked", "screen": PERMISSION, "trust": False, **st})
    return name


results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


# 問いの読み取り
case("承認")
p = prompt()
check("承認の種類", (p["kind"], p["risky"], "ls -la" in p["text"], len(p["fp"])), ("permission", False, True, 40))
check("会話の上の部分は出さない", "一覧を見ます" in p["text"], False)
case("取り消しにくい承認", screen=RISKY)
check("取り消しにくい印", prompt()["risky"], True)
case("質問", screen=QUESTION)
check("質問の種類", prompt()["kind"], "question")
case("止まっていない", status="working")
check("止まっていない", prompt(), {"kind": "none"})
case("信頼", trust=True)
check("フォルダの信頼", prompt(), {"kind": "trust"})
r, sent = answer(fp="0" * 40, choice="allow")
check("フォルダの信頼の確認には、画面のボタンからも答えない", (r["reason"], sent), ("trust", []))
case("読めない", trust=None)
check("画面を読めない", prompt(), {"kind": "unreadable"})
case("入れ替わり")
check("別のセッション", prompt("sess-2"), {"kind": "gone"})

# 返答
case("許可")
fp = prompt()["fp"]
check("許可は 1（今回だけ）", answer(fp=fp, choice="allow"), ({"ok": True, "said": "許可（今回だけ）"}, ["1"]))
check("断るは esc", answer(fp=fp, choice="deny"), ({"ok": True, "said": "断る"}, ["esc"]))
r, sent = answer(fp="0" * 40, choice="allow")
check("画面が変わったら送らない", (r["ok"], r["reason"], sent), (False, "changed", []))
r, sent = answer(fp=fp, choice="always")
check("知らない答えは送らない", (r["reason"], sent), ("bad_choice", []))
for name, key in (("別のセッション", "sess-2"), ("key が空", ""), ("key が無い", None)):
    state["sent"].clear()
    s.ANSWERED.clear()
    req = {"pane": "w9:p1", "fp": fp, "choice": "allow", **({} if key is None else {"key": key})}
    r = json.loads(asyncio.run(s.api_answer(req)).body)
    check(f"{name}には送らない", (r["reason"], state["sent"]), ("gone", []))
check("問いの読み取りも key が要る", prompt(""), {"kind": "gone"})

# 二度押し・重ねた送信
answer(fp=fp, choice="allow")
r, sent = answer(keep=True, fp=fp, choice="allow")
check("同じ問いへの二度目は送らない", (r["reason"], sent), ("answered", []))
r, sent = answer(keep=True, fp=fp, choice="deny")
check("同じ問いに、続けて断るも送らない", (r["reason"], sent), ("answered", []))


async def both():
    return await asyncio.gather(*(s.api_answer({"pane": "w9:p1", "key": "sess-1", "fp": fp, "choice": "allow"}) for _ in range(2)))
state["sent"].clear()
s.ANSWERED.clear()
rs = sorted(json.loads(r.body).get("reason", "ok") for r in asyncio.run(both()))
check("同時に2つ届いても、送るのは1回", (rs, state["sent"]), (["answered", "ok"], ["1"]))

case("取り消しにくい許可", screen=RISKY)
fp = prompt()["fp"]
r, sent = answer(fp=fp, choice="allow")
check("取り消しにくい許可は、もう一度押すまで送らない", (r["reason"], sent), ("confirm", []))
check("もう一度押したら 1", answer(fp=fp, choice="allow", confirmed=True)[1], ["1"])
check("取り消しにくくても、断るはすぐ", answer(fp=fp, choice="deny")[1], ["esc"])

case("質問への返答", screen=QUESTION)
fp = prompt()["fp"]
check("質問には、許可も断るも送らない（承認の画面だけに答える）",
      [(answer(fp=fp, choice=c)[0]["reason"], state["sent"]) for c in ("allow", "deny")], [("not_permission", []), ("not_permission", [])])

case("本物の Write の承認", screen=REAL_WRITE)
check("本物の Write の承認はボタンを出す", prompt()["kind"], "permission")
check("本物の Write の承認に許可を送れる", answer(fp=prompt()["fp"], choice="allow")[1], ["1"])
for name, screen in (("本物の Yes／No の質問", REAL_YESNO), ("Yes／No の質問（短い形）", YESNO_SHORT)):
    case(name, screen=screen)
    p = prompt()
    check(f"{name}は承認と取り違えない", p["kind"], "question")
    check(f"{name}には許可も断るも送らない", [answer(fp=p["fp"], choice=c)[1] for c in ("allow", "deny")], [[], []])
case("Do you want to が無い", screen=PERMISSION.replace("Do you want to proceed?", "続けますか？"))
check("承認の形がそろわなければボタンを出さない", prompt()["kind"], "unknown")
# ペインの高さが足りず、選択肢が「1. Yes」しか映っていない（2026-10-21 実画面で確認）
case("選択肢が映りきっていない", screen="⏺ Write(/Users/me/proj/a.txt)\n Do you want to create a.txt?\n ❯ 1. Yes\n Esc to cancel · Tab to amend")
p = prompt()
check("選択肢が映りきっていなければボタンを出さず、送らない", (p["kind"], [answer(fp=p["fp"], choice=c)[1] for c in ("allow", "deny")]), ("unknown", [[], []]))

# 選択式の質問: 前の「選択肢の番号だけを送る」答え方（choose）は 0.26.1 で外した。いまの 1 問ずつの形（step）が断る画面にも
# 番号を送れてしまうため（2026-11-17 a2a 32b67f4e）。この試験では画面の読み取り（read_dialog）を止めているので、step も付かない
case("本物の選択式の質問", screen=REAL_CHOICE)
p = prompt()
check("1 つを選ぶ質問にも、前の選択肢（choices）を付けない", (p["kind"], "choices" in p, "step" in p), ("question", False, False))
check("質問の中身は見出しの印から見せる（会話の上の部分は出さない）", (p["text"].splitlines()[0], "試験です" in p["text"]), (" ☐ 試験", False))
for n in (1, 2):
    r, sent = answer(fp=p["fp"], choice="choose", n=n)
    check(f"前の答え方（choose）で {n} 番を送っても、何も送らない", (r.get("reason"), sent), ("bad_choice", []))
r, sent = answer(fp=p["fp"], choice="step", sfp="x", act="pick", n=1)
check("1 問ずつの形を読めない画面には、step でも送らない", (r.get("reason"), sent), ("not_form", []))
check("選ぶ質問にも、許可・断るは送らない", [(lambda r: (r[0].get("reason"), r[1]))(answer(fp=p["fp"], choice=c)) for c in ("allow", "deny")],
      [("not_permission", []), ("not_permission", [])])
case("本物の Yes／No の質問を選ぶ", screen=REAL_YESNO)
p = prompt()
check("本物の Yes／No の質問は、承認と取り違えず、前の答え方でも送らない", (p["kind"], answer(fp=p["fp"], choice="choose", n=2)[1]), ("question", []))
for name, screen in (("複数選択", REAL_MULTI), ("問いが 2 つ", REAL_TWO)):
    case(name, screen=screen)
    check(f"{name}も、中身は見出しの印から見せる", prompt()["text"].splitlines()[0].startswith("←  ☐"), True)
case("質問の形の下に承認", screen=REAL_CHOICE + "\n" + PERMISSION)
check("いちばん下が承認なら、上に書かれた質問の形は読まない（承認として扱う）", prompt()["kind"], "permission")

# 長い質問で、問いが末尾 15 行の外に出ても、取り消しにくい印を落とさない（2026-11-05 Codex の再現）
LONG = """ ☐ 確認
本番に反映しますか？
❯ 1. 進める
     説明1
     補足1
  2. 待つ
     説明2
     補足2
  3. 調べる
     説明3
     補足3
  4. 相談する
     説明4
     補足4
  5. Type something.
  6. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel"""
case("長い質問", screen=LONG)
check("長い質問でも、問いの取り消しにくい印を落とさない", prompt()["risky"], True)
case("説明に ⏺", screen=REAL_CHOICE.replace("試験の質問です。どれにしますか？", "本番に反映しますか？").replace("     赤い果物", "     ⏺ 赤い果物"))
check("説明の行に ⏺ があっても、問いの取り消しにくい印を落とさない", prompt()["risky"], True)
real_run = s.subprocess.run
s.subprocess.run = lambda *a, **k: type("P", (), {"stdout": REAL_CHOICE.replace("     赤い果物", "     赤い果物\u2028  2. 偽"), "returncode": 0})()
got = REAL_READ("w9:p1")
s.subprocess.run = real_run
check("read_prompt は特殊な改行文字を行の区切りにせず、� に置き換える", (got.count("\n"), "\ufffd" in got, "\u2028" in got), (REAL_CHOICE.count("\n"), True, False))


# ハブ経由の答え（/api/hub/answer）と画面のボタンは、同じ鍵で順番にする（確かめてから送るまでが重ならない）
async def hub_waits():
    s._answer_lock = asyncio.Lock()  # ほかの試験の event loop に結び付いていない鍵にする
    async with s._answer_lock:
        t = asyncio.create_task(s.hub_answer({"pane": "nope", "answer": "1"}))
        await asyncio.sleep(0.1)
        waited = not t.done()
    await t
    return waited, t.done()
check("ハブ経由の答えは、画面のボタンの返答の鍵が空くまで待つ", asyncio.run(hub_waits()), (True, True))

# ハブ経由の答えと画面のボタンは、答えた問いの記録を共有する。答えたあと画面が書き換わるまでの間に、同じ問いへもう一度送らない
# （2 つ目の番号が、次に出た承認の「今後は聞かずに許可」に届かないように。2026-11-05 Codex の再現: 順にどちらから送っても 2 回送れた）
s.hub_turns = lambda n=1: [("t2", "はい")]  # 利用者の最後の発言（本体は引数なしでも呼ぶ: hub_human_turn）
s.hub_heard = lambda turn: "proj の承認、許可しますか？"  # その直前に利用者が聞いたハブの返答
s.hub_via = lambda turn: ""  # その発言は画面から（台帳に無い発言は、本体が「機械から」とみなして断る）
s.jsonl_path = lambda a: None
s.start_watch = lambda *a, **k: None


def hub(ans):
    return json.loads(asyncio.run(s.hub_answer({"pane": "w9:p1", "answer": ans})).body)


for order in ("ハブ → ボタン", "ボタン → ハブ"):
    case(order, screen=PERMISSION)
    fp = prompt()["fp"]
    s.ANSWERED.clear(); s._answer_lock = asyncio.Lock(); state["sent"].clear()
    s.SEEN_PROMPT["w9:p1"] = (fp, "t1", "sess-1")  # ハブは前の発言で問いを見て、利用者に伝えた
    page = lambda: json.loads(asyncio.run(s.api_answer({"pane": "w9:p1", "key": "sess-1", "fp": fp, "choice": "allow"})).body)
    first, second = (hub("yes"), page()) if order == "ハブ → ボタン" else (page(), hub("yes"))
    check(f"{order}: 画面が古い問いのままでも、送るのは 1 回だけ", (first.get("ok"), second.get("reason"), state["sent"]), (True, "answered", ["1"]))
    s.SEEN_PROMPT["w9:p1"] = (fp, "t1", "sess-1")
    check(f"{order}: そのあとハブが断る（いいえ）も、同じ問いには送らない", (hub("no").get("reason"), state["sent"]), ("answered", ["1"]))
s.ANSWERED.clear()

# セッションの立て直し（key はセッション ID で見分ける）
AGENT_RE = {**AGENT, "session": "old-session"}
s.list_agents = lambda: [{**AGENT_RE, "status": "blocked"}]
case("立て直す前の承認")
fp = s._asked("w9:p1", "old-session")[1]["fp"]
AGENT_RE["session"] = "new-session"  # 同じ pane でセッションが立て直された（画面の文字は同じ）
state["sent"].clear(); s.ANSWERED.clear()
r = json.loads(asyncio.run(s.api_answer({"pane": "w9:p1", "key": "old-session", "fp": fp, "choice": "allow"})).body)
check("立て直されたセッションには、古い欄から送らない", (r["reason"], state["sent"]), ("gone", []))
r = json.loads(asyncio.run(s.api_answer({"pane": "w9:p1", "key": "hub", "fp": fp, "choice": "allow"})).body)
check("herdr のセッションを、ハブの鍵 \"hub\" で指しても送らない", (r["reason"], state["sent"]), ("gone", []))
AGENT_RE["session"] = None  # セッション ID が無い
check("セッション ID が無ければボタンを出さない", s._ask_of({**AGENT_RE, "status": "blocked"})["kind"], "unknown")
s.list_agents = lambda: [{**AGENT, "status": state["status"]}]

case("わからない画面", screen=UNKNOWN)
fp = prompt()["fp"]
check("わからない画面には何も送らない", [answer(fp=fp, choice=c)[1] for c in ("allow", "deny")], [[], []])

for name, st in (("信頼", {"trust": True}), ("読めない", {"trust": None}), ("止まっていない", {"status": "idle"})):
    case(name, **st)
    r, sent = answer(fp=fp, choice="allow")
    check(f"{name}のときは送らない", (r["ok"], sent), (False, []))

# 止まっているセッションすべての一覧（画面の上に並べるもの）
AGENTS = {"w1:p1": ("sess-a", "blocked", PERMISSION, False), "w2:p1": ("sess-b", "blocked", RISKY, False),
          "w3:p1": ("sess-c", "blocked", QUESTION, False), "w4:p1": ("sess-d", "blocked", "", True), "w5:p1": ("sess-e", "working", "", False),
          "w6:p1": ("sess-f", "blocked", REAL_CHOICE, False)}
s.list_agents = lambda: [{**AGENT, "pane": p, "session": k, "status": st, "title": f"t{p[1]}", "cwd": f"/x/proj{p[1]}"}
                         for p, (k, st, _, _) in AGENTS.items()]
s.read_prompt = lambda pane: AGENTS[pane][2]
s.pane_shows_trust = lambda pane: AGENTS[pane][3]
got = json.loads(s.api_prompts().body)
check("一覧は止まっているものだけ・順番どおり", [g["pane"] for g in got], ["w1:p1", "w2:p1", "w3:p1", "w4:p1", "w6:p1"])
check("一覧の中身（名前・key・種類・取り消しにくいか）",
      [(g["name"], g["key"], g["kind"], g.get("risky")) for g in got],
      [("proj1｜t1", "sess-a", "permission", False), ("proj2｜t2", "sess-b", "permission", True),
       ("proj3｜t3", "sess-c", "question", False), ("proj4｜t4", "sess-d", "trust", None), ("proj6｜t6", "sess-f", "question", False)])
check("一覧に、前の選択肢（choices）は付かない", ["choices" in g for g in got], [False] * 5)
check("一覧の指紋は、それぞれの問いのもの", [g.get("fp") == s.prompt_fp(AGENTS[g["pane"]][2]) for g in got[:3]], [True, True, True])

bad = [f"{n}: {g!r}（期待 {w!r}）" for n, ok, g, w in results if not ok]
print("\n".join(bad) or f"{len(results)} 例、すべて期待どおり")
sys.exit(1 if bad else 0)
