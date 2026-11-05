"""画面のボタンからの返答（/api/prompt・/api/answer）の試験。止まっている画面を作り物にし、送るキーと断る理由を確かめる。承認の許可・断ると、選択式の質問の番号（実画面の形と、相手の位置から書ける形）。
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
from question_screens import REAL_CHOICE, REAL_MULTI, REAL_PREVIEW, REAL_TWO  # noqa: E402  2026-11-05 に本物の Claude Code で取った画面

AGENT = {"pane": "w9:p1", "title": "テスト", "cwd": "/tmp/proj", "kind": "claude", "session": "sess-1",
         "status": "blocked", "name": "", "hub": False}
state = {"status": "blocked", "screen": PERMISSION, "trust": False, "sent": []}
s.list_agents = lambda: [{**AGENT, "status": state["status"]}]
s.pane_shows_trust = lambda pane: state["trust"]
s.read_prompt = lambda pane: state["screen"]
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

# 選択式の質問（問いが 1 つで 1 つを選ぶ）: 選択肢のボタンを出し、画面から読んだ番号だけを送る
case("本物の選択式の質問", screen=REAL_CHOICE)
p = prompt()
check("1 つを選ぶ質問は、見出し・問い・選択肢を読む", (p["kind"], p["header"], p["question"], [(c["n"], c["label"], c["desc"]) for c in p["choices"]]),
      ("question", "試験", "試験の質問です。どれにしますか？", [(1, "りんご", "赤い果物"), (2, "みかん", "橙の果物"), (3, "ぶどう", "紫の果物")]))
check("質問の中身は見出しの印から見せる（会話の上の部分は出さない）", (p["text"].splitlines()[0], "試験です" in p["text"]), (" ☐ 試験", False))
check("選ぶと、その番号だけを送る", answer(fp=p["fp"], choice="choose", n=2), ({"ok": True, "said": "2番「みかん」"}, ["2"]))
for name, n in (("Type something.", 4), ("Chat about this", 5), ("無い番号", 9), ("0", 0), ("文字の番号", "2"), ("真偽値", True), ("番号なし", None)):
    r, sent = answer(fp=p["fp"], choice="choose", **({} if n is None else {"n": n}))
    check(f"{name}は選ばない", (r.get("reason"), sent), ("bad_choice", []))
r, sent = answer(fp="0" * 40, choice="choose", n=1)
check("画面が変わったら選ばない", (r.get("reason"), sent), ("changed", []))
check("選ぶ質問にも、許可・断るは送らない", [(lambda r: (r[0].get("reason"), r[1]))(answer(fp=p["fp"], choice=c)) for c in ("allow", "deny")],
      [("not_permission", []), ("not_permission", [])])
answer(fp=p["fp"], choice="choose", n=1)
r, sent = answer(keep=True, fp=p["fp"], choice="choose", n=3)
check("同じ問いに、続けて別の番号は送らない", (r.get("reason"), sent), ("answered", []))
check("セッション ID が無ければ選択肢を出さない", "choices" in s._ask_of({**AGENT, "session": None, "status": "blocked"}), False)
case("取り消しにくい質問", screen=REAL_CHOICE.replace("試験の質問です。どれにしますか？", "本番に反映しますか？"))
p = prompt()
r, sent = answer(fp=p["fp"], choice="choose", n=1)
check("取り消しにくく見える質問は、もう一度押すまで選ばない", (p["risky"], r.get("reason"), sent), (True, "confirm", []))
check("もう一度押したら、その番号", answer(fp=p["fp"], choice="choose", n=1, confirmed=True)[1], ["1"])
case("本物の Yes／No の質問を選ぶ", screen=REAL_YESNO)
p = prompt()
check("本物の Yes／No の質問は、承認でなく選択肢のボタンで答える", (p["kind"], [c["label"] for c in p["choices"]], answer(fp=p["fp"], choice="choose", n=2)),
      ("question", ["Yes", "No"], ({"ok": True, "said": "2番「No」"}, ["2"])))
for name, screen in (("複数選択", REAL_MULTI), ("問いが 2 つ", REAL_TWO), ("preview 付き", REAL_PREVIEW),
                     ("見出しの印の無い Yes／No", YESNO_SHORT), ("見出しの印の無い質問", QUESTION)):
    case(name, screen=screen)
    p = prompt()
    r, sent = answer(fp=p["fp"], choice="choose", n=1)
    check(f"{name}は選択肢のボタンを出さず、番号も送らない", (p["kind"], "choices" in p, r.get("reason"), sent), ("question", False, "not_choice", []))
for name, screen in (("複数選択", REAL_MULTI), ("問いが 2 つ", REAL_TWO)):
    case(name, screen=screen)
    check(f"{name}も、中身は見出しの印から見せる", prompt()["text"].splitlines()[0].startswith("←  ☐"), True)

# 相手の位置から: セッションの中の Claude（乗せられているかもしれない）は、問い・選択肢・説明と、その上の会話の文を書ける
case("番号が重なる", screen=REAL_CHOICE.replace("     赤い果物", "     赤い果物\n  2. 偽の選択肢"))
check("選択肢の形の行で番号を重ねた画面は、選択肢を出さない", "choices" in prompt(), False)
case("説明に番号", screen=REAL_CHOICE.replace("     赤い果物", "     2. 説明に書いた番号"))
check("説明の行（5 文字下げ）の番号は選択肢にしない", [(c["n"], c["label"]) for c in prompt()["choices"]], [(1, "りんご"), (2, "みかん"), (3, "ぶどう")])
case("形の外の行", screen=REAL_CHOICE.replace("     橙の果物", "     橙の果物\nここに別の行"))
check("形の外の行が混じる画面は、選択肢を出さない", "choices" in prompt(), False)
case("質問の形の下に承認", screen=REAL_CHOICE + "\n" + PERMISSION)
p = prompt()
check("いちばん下が承認なら、上に書かれた質問の形は読まない", (p["kind"], "choices" in p, answer(fp=p["fp"], choice="choose", n=1)), ("permission", False, ({"ok": False, "reason": "not_choice"}, [])))
case("質問の形の下に入力待ち", screen=REAL_CHOICE + "\n" + UNKNOWN)
p = prompt()
check("いちばん下が操作案内でなければ、上に書かれた質問の形は読まない", ("choices" in p, answer(fp=p["fp"], choice="choose", n=1)[1]), (False, []))
case("Submit の付いた見出し", screen=REAL_CHOICE.replace(" ☐ 試験", " ☐ 試験  ✔ Submit"))
check("見出しに Submit があれば、選択肢を出さない", "choices" in prompt(), False)

# セッションの立て直し（ハブも、key はセッション ID で見分ける）
AGENT_HUB = {**AGENT, "hub": True, "cwd": str(s.HUB_DIR), "session": "old-session"}
s.list_agents = lambda: [{**AGENT_HUB, "status": "blocked"}]
case("ハブの承認")
fp = s._asked("w9:p1", "old-session")[1]["fp"]
AGENT_HUB["session"] = "new-session"  # 同じ場所でハブが立て直された（画面の文字は同じ）
state["sent"].clear(); s.ANSWERED.clear()
r = json.loads(asyncio.run(s.api_answer({"pane": "w9:p1", "key": "old-session", "fp": fp, "choice": "allow"})).body)
check("立て直されたハブには、古い欄から送らない", (r["reason"], state["sent"]), ("gone", []))
r = json.loads(asyncio.run(s.api_answer({"pane": "w9:p1", "key": "hub", "fp": fp, "choice": "allow"})).body)
check("ハブを \"hub\" で指しても送らない", (r["reason"], state["sent"]), ("gone", []))
AGENT_HUB["session"] = None  # セッション ID が無い
check("セッション ID が無ければボタンを出さない", s._ask_of({**AGENT_HUB, "status": "blocked"})["kind"], "unknown")
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
check("一覧の選択肢は、1 つを選ぶ質問にだけ付く", [[c["label"] for c in g.get("choices", [])] for g in got], [[], [], [], [], ["りんご", "みかん", "ぶどう"]])
check("一覧の指紋は、それぞれの問いのもの", [g.get("fp") == s.prompt_fp(AGENTS[g["pane"]][2]) for g in got[:3]], [True, True, True])

bad = [f"{n}: {g!r}（期待 {w!r}）" for n, ok, g, w in results if not ok]
print("\n".join(bad) or f"{len(results)} 例、すべて期待どおり")
sys.exit(1 if bad else 0)
