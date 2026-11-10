"""止まっている画面の指紋が、実行を待つ道具の行の印（⏺）の点滅で変わらないことの試験。
Claude Code は、承認を待つ間、道具の見出しの行の頭の ⏺ を点滅させる（消えている間は空白）。指紋がそれで変わると、ハブが見た問いと
答えるときの問いが別物に見え、同じ許可を何度も求めることになる。印のあるなしは同じ問いとして扱い、それ以外の違いは別の問いとして断る。
相手は、止まっているセッションの画面に文字を出せる側（道具の説明・コマンドの文字を書ける）。印を外しても、コマンドや説明の文字が
1 字でも違えば別の指紋になること、問いが入れ替わったら断ることを確かめる。
Hermes の API と herdr には触れない（差し替える）。使い方: uv run spike/blink_test.py"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HUB="hermes", VL_HERMES_KEY="k1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server as s  # noqa: E402

# 2026-11-05 に本物の Claude Code で見た形（説明つきの Bash の承認。見出しの行が点滅する）。中身は架空
ON = """⏺ 一覧を見ます。
⏺ フォルダの中身を見る
 Bash command
   ls -la
   フォルダの中身を見る
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for ls commands in /Users/me/proj
   3. No, and tell Claude what to do differently (esc)
 Esc to cancel · Tab to amend"""
OFF = ON.replace("⏺ フォルダの中身を見る", "  フォルダの中身を見る")
RISKY_ON = ON.replace("ls -la", "rm -rf build").replace("ls commands", "rm commands").replace("フォルダの中身を見る", "build を消す")
RISKY_OFF = RISKY_ON.replace("⏺ build を消す", "  build を消す")

# 2026-11-10 に本物の Claude Code で取った形（複数行のコマンド。罫線と空行は read_prompt が除くので入れていない）。
# 見出しの印は字下げなしの行の頭にあり、消えている間は空白 2 つになる。コマンドの本文は、結果の欄（⎿）でも承認の枠（│）でも字下げされる
REAL_ON = """⏺ 書き込みます。
⏺ 試験の書き込み
  ⎿  $ tee /tmp/probe.txt <<'EOF2'
     line 1
     alpha
     line 3
     EOF2
 Bash command
 試験の書き込み
 │ tee /tmp/probe.txt <<'EOF2'
 │ line 1
 │ alpha
 │ line 3
 │ EOF2
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and always allow access to /tmp from this
      project
   3. No
 Esc to cancel · Tab to amend"""
REAL_OFF = REAL_ON.replace("⏺ 試験の書き込み", "  試験の書き込み")
# 相手が書ける違い: 本文の行の頭に印を足す（字下げされた行）。中身の違うコマンドなので、別の問いとして扱う
MARKED = REAL_ON.replace("     alpha", "     ⏺ alpha").replace(" │ alpha", " │ ⏺ alpha")
MARKED_OFF = MARKED.replace("⏺ 試験の書き込み", "  試験の書き込み")
# 長い承認（ほかに印の行が映っていない。取り消しにくい語は、最後の 15 行より上にだけある）
LONG_ON = "\n".join(["⏺ 確認する", " Bash command", " 確認する", " │ python3 - <<'PY'", ' │ os.system("rm -rf build")']
                    + [f" │ # 行 {i}" for i in range(1, 16)]
                    + [" │ PY", " Do you want to proceed?", " ❯ 1. Yes", "   2. Yes, and don't ask again for python3 commands in /x/proj", "   3. No", " Esc to cancel · Tab to amend"])
LONG_OFF = LONG_ON.replace("⏺ 確認する", "  確認する")

# 選択式の質問。問いの文は字下げなしで出る（2026-11-05 に本物の画面で確かめた形）。相手は、問いの文を印で始められる
ASK_A = """⏺ AskUserQuestion
 ☐ 名前
⏺ report.txt
上の 1 行全体を、先頭の記号と空白も含めてファイル名として採用しますか？
❯ 1. 採用する
  2. 採用しない
  3. Type something.
Enter to select · ↑/↓ to navigate · Esc to cancel"""
ASK_B = ASK_A.replace("⏺ report.txt", "report.txt")
# 選択肢が Yes と No の質問（承認ではない）
ASK_YN = ASK_A.replace("1. 採用する", "1. Yes").replace("2. 採用しない", "2. No")

TARGET = {"pane": "w9:p1", "title": "テスト", "cwd": "/x/proj", "kind": "claude", "session": "sess-1", "status": "blocked", "name": "", "hub": False}
state = {"screen": ON, "keys": []}
s.list_agents = lambda: [dict(TARGET)]
s.pane_shows_trust = lambda pane: False
s.read_prompt = lambda pane: state["screen"]
s.read_lines = lambda *a, **k: []
s.jsonl_path = lambda a: None
s.herdr = lambda *a, **k: (state["keys"].append(a[-1]), {"result": {}})[1] if a[:2] == ("agent", "send-keys") else {"result": {}}
s.hermes.ask_hermes = lambda text: "わかりました"

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


def begin(text):
    return s.hermes.hermes_turn_begin(text)


def asked_then(reply, utter):
    s.hermes.hermes_turn_end(s.hub_human_turn(), reply)
    return begin(utter)


def screen(text):
    state["screen"] = text
    return json.loads(s.hub_screen("w9:p1").body)


def hub_answer(text, **body):
    state["screen"] = text
    state["keys"].clear()
    r = json.loads(asyncio.run(s.hub_answer({"pane": "w9:p1", "answer": "yes", **body})).body)
    return r.get("reason", "ok"), list(state["keys"])


def button(text, fp):
    state["screen"] = text
    state["keys"].clear()
    if hasattr(s, "ANSWERED"):
        s.ANSWERED.clear()
    r = json.loads(asyncio.run(s.api_answer({"pane": "w9:p1", "key": "sess-1", "fp": fp, "choice": "allow"})).body)
    return r.get("reason", "ok"), list(state["keys"])


def reset():
    s.SEEN_PROMPT.clear()
    s.CONFIRMING.clear()


# ---- 指紋そのもの
fp = s.prompt_fp
check("印が消えている間も同じ指紋", fp(ON) == fp(OFF), True)
check("取り消しにくい問いでも同じ", fp(RISKY_ON) == fp(RISKY_OFF), True)
check("コマンドが 1 字違えば別の指紋（印あり）", fp(ON) == fp(ON.replace("ls -la", "ls -lA")), False)
check("コマンドが 1 字違えば別の指紋（印なしと比べても）", fp(ON) == fp(OFF.replace("ls -la", "ls -lA")), False)
check("説明が違えば別の指紋", fp(ON) == fp(OFF.replace("  フォルダの中身を見る", "  フォルダの中身を消す")), False)
check("別の承認は別の指紋", fp(ON) == fp(RISKY_OFF), False)
check("行の途中の ⏺ は文字として数える", fp(ON) == fp(ON.replace("   ls -la", "   ls ⏺ -la")), False)
check("印を別の文字に替えたものは別の指紋", fp(ON) == fp(ON.replace("⏺ フォルダの中身を見る", "● フォルダの中身を見る")), False)
check("行が増えれば別の指紋", fp(ON) == fp(ON.replace("   ls -la", "   ls -la\n⏺ rm -rf build")), False)
check("行の頭の印の後ろに足した文字は数える", fp(ON) == fp(ON.replace("⏺ フォルダの中身を見る", "⏺ ⏺ フォルダの中身を見る")), False)

check("本物の形でも、印が消えている間は同じ指紋", fp(REAL_ON) == fp(REAL_OFF), True)
check("本文の行の頭に足された印は数える（印あり同士）", fp(REAL_ON) == fp(MARKED), False)
check("本文の行の頭に足された印は数える（見出しの印が消えていても）", (fp(REAL_ON) == fp(MARKED_OFF), fp(REAL_OFF) == fp(MARKED), fp(REAL_OFF) == fp(MARKED_OFF)), (False, False, False))
check("承認の枠の中だけ印が違っても別の指紋", fp(REAL_ON) == fp(REAL_OFF.replace(" │ alpha", " │ ⏺ alpha")), False)
check("結果の欄の中だけ印が違っても別の指紋", fp(REAL_ON) == fp(REAL_OFF.replace("     alpha", "     ⏺ alpha")), False)
check("字下げが 1 つでもあれば、行の頭の印は数える", fp(REAL_ON) == fp(REAL_ON.replace(" Bash command", " ⏺ Bash command")), False)

check("質問の画面では、字下げなしの行の頭の印も数える", (fp(ASK_A) == fp(ASK_B), fp(ASK_YN) == fp(ASK_YN.replace("⏺ report.txt", "report.txt"))), (False, False))
check("質問の画面は、見出しの行の印が違っても別の指紋", fp(ASK_A) == fp(ASK_A.replace("⏺ AskUserQuestion", "  AskUserQuestion")), False)
check("承認でも質問でもない画面は、印も数える", fp("⏺ 考えています\n  何か入力してください") == fp("  考えています\n  何か入力してください"), False)

# ---- いま聞かれている部分: 本文に印を書かれても区切りにしない。印が消えている間も、狭くはしない
ap = s.asked_part
check("本文の印では区切らない（見出しから下が全部入る）", ap(MARKED).splitlines()[0], "⏺ 試験の書き込み")
check("印が消えている間は、1 つ前の印の行から（広くなる）", (ap(REAL_OFF).splitlines()[0], ap(REAL_ON) in ap(REAL_OFF).replace("  試験の書き込み", "⏺ 試験の書き込み", 1)), ("⏺ 書き込みます。", True))
check("本文に印があって見出しの印が消えていても、狭くならない", ap(MARKED_OFF).splitlines()[0], "⏺ 書き込みます。")
check("長い承認: 印ありなら、上のほうの取り消しにくい語を見つける", bool(s.RISKY.search(ap(LONG_ON))), True)
check("長い承認: 印が消えていても、同じ語を見つける（最後の 15 行だけにしない）", bool(s.RISKY.search(ap(LONG_OFF))), True)
check("長い承認は 26 行で、取り消しにくい語は最後の 15 行に無い", (len(LONG_ON.splitlines()), bool(s.RISKY.search("\n".join(LONG_OFF.splitlines()[-15:])))), (26, False))

# ---- ハブ経由の答え: 見たときと答えるときで点滅の向きが違っても、同じ問いとして通す
Q = "proj の「テスト」が、フォルダの中身を見る（ls）許可を求めています。許可しますか？"
for name, seen, now in (("印ありで見て、印なしのときに答える", ON, OFF), ("印なしで見て、印ありのときに答える", OFF, ON)):
    reset()
    begin("承認どうなってる？")
    screen(seen)
    asked_then(Q, "はい、許可して")
    check(f"{name} → 1 を送る", hub_answer(now), ("ok", ["1"]))
    s.hermes.hermes_turn_end(s.hub_human_turn(), "許可しました")

# ---- 問いが本当に変わったら断る（点滅の向きによらない）
for name, now in (("コマンドが 1 字違う（印なし）", OFF.replace("ls -la", "ls -lA")), ("別の承認に替わった（印なし）", RISKY_OFF),
                  ("別の承認に替わった（印あり）", RISKY_ON)):
    reset()
    begin("承認どうなってる？")
    screen(ON)
    asked_then(Q, "はい、許可して")
    check(f"{name} → 送らない", hub_answer(now), ("unseen", []))
    s.hermes.hermes_turn_end(s.hub_human_turn(), "確かめ直します")

# ---- 見たのと同じ発言の中では、点滅の向きが違っても答えない
reset()
begin("承認どうなってる？")
screen(ON)
check("同じ発言の中では答えない", hub_answer(OFF), ("same_turn", []))
s.hermes.hermes_turn_end(s.hub_human_turn(), Q)

# ---- 取り消しにくい操作の聞き返し: 聞き返しのあとで点滅の向きが変わっても、同じ問いとして通す
RQ = "proj の「テスト」が、build のフォルダを消す（rm -rf build）許可を求めています。許可しますか？"
reset()
begin("承認見て")
screen(RISKY_ON)
asked_then(RQ, "許可")
check("取り消しにくい操作は聞き返す（印なしのとき）", hub_answer(RISKY_OFF), ("confirm", []))
check("同じ発言の中の --confirmed は通らない", hub_answer(RISKY_ON, confirmed=True), ("confirm", []))
asked_then("proj で、本当に build のフォルダを消していいですか？", "はい、許可していい")
check("次の発言の --confirmed で通る（印ありに戻っていても）", hub_answer(RISKY_ON, confirmed=True), ("ok", ["1"]))
s.hermes.hermes_turn_end(s.hub_human_turn(), "許可しました")

# ---- 聞き返しのあとで問いが替わったら、--confirmed でも通さない
reset()
begin("承認見て")
screen(RISKY_ON)
asked_then(RQ, "許可")
hub_answer(RISKY_OFF)
asked_then("proj で、本当に build のフォルダを消していいですか？", "はい、許可していい")
check("聞き返しのあとでコマンドが替わったら送らない", hub_answer(RISKY_OFF.replace("rm -rf build", "rm -rf /"), confirmed=True), ("unseen", []))
s.hermes.hermes_turn_end(s.hub_human_turn(), "確かめ直します")

# ---- 本文の印だけが違う問いに替わったら断る（見出しの印の向きによらない）
for name, now in (("本文に印が足された（見出しの印あり）", MARKED), ("本文に印が足された（見出しの印なし）", MARKED_OFF)):
    reset()
    begin("承認どうなってる？")
    screen(REAL_OFF)
    asked_then("proj の「テスト」が、/tmp/probe.txt に書く（tee）許可を求めています。許可しますか？", "はい、許可して")
    check(f"{name} → 送らない", hub_answer(now), ("unseen", []))
    s.hermes.hermes_turn_end(s.hub_human_turn(), "確かめ直します")

# ---- 選択式の質問: 問いの文の頭の印だけが違う問いに替わったら、番号の答えを送らない
reset()
begin("質問どうなってる？")
screen(ASK_A)
asked_then("proj の「テスト」が、ファイル名をどうするか聞いています。1 番は採用する、2 番は採用しない。どちらにしますか？", "1番")
check("質問の文の頭の印が消えた問いに替わったら送らない", hub_answer(ASK_B, answer="1"), ("unseen", []))
s.hermes.hermes_turn_end(s.hub_human_turn(), "確かめ直します")
reset()
begin("質問どうなってる？")
screen(ASK_A)
asked_then("proj の「テスト」が、ファイル名をどうするか聞いています。1 番は採用する、2 番は採用しない。どちらにしますか？", "1番")
check("同じ質問のままなら、番号を送る", hub_answer(ASK_A, answer="1"), ("ok", ["1"]))
s.hermes.hermes_turn_end(s.hub_human_turn(), "答えました")

# ---- 長い承認: 印ありで見て、印なしのときに答えても、取り消しにくい操作の聞き返しを飛ばさない
LQ = "proj の「テスト」が、build のフォルダを消す（rm -rf build）Python の実行の許可を求めています。許可しますか？"
for name, seen, now in (("印ありで見て、印なしで答える", LONG_ON, LONG_OFF), ("印なしで見て、印なしで答える", LONG_OFF, LONG_OFF), ("印なしで見て、印ありで答える", LONG_OFF, LONG_ON)):
    reset()
    begin("承認見て")
    screen(seen)
    asked_then(LQ, "許可")
    check(f"長い承認（{name}）→ 聞き返す", hub_answer(now), ("confirm", []))
    s.hermes.hermes_turn_end(s.hub_human_turn(), "確かめ直します")
state["screen"] = LONG_OFF
check("長い承認: 画面に出す問いにも、印が消えている間、取り消しにくい印が付く", s._asked("w9:p1", "sess-1")[1]["risky"], True)

# ---- 取り消しにくい語の判定は、点滅の向きで変わらない（この形では）
check("取り消しにくい印は、印ありでも印なしでも付く", (bool(s.RISKY.search(s.asked_part(RISKY_ON))), bool(s.RISKY.search(s.asked_part(RISKY_OFF)))), (True, True))

# ---- 画面のボタン: 画面に出したときと押したときで点滅の向きが違っても通す。問いが替わったら断る
state["screen"] = ON
shown = s._asked("w9:p1", "sess-1")[1]["fp"]
check("ボタン: 印なしのときに押しても通る", button(OFF, shown), ("ok", ["1"]))
check("ボタン: コマンドが替わっていたら送らない", button(OFF.replace("ls -la", "ls -lA"), shown), ("changed", []))
check("ボタン: 別の承認に替わっていたら送らない", button(RISKY_OFF, shown), ("changed", []))
state["screen"] = REAL_ON
shown = s._asked("w9:p1", "sess-1")[1]["fp"]
check("ボタン: 本物の形で、印なしのときに押しても通る", button(REAL_OFF, shown), ("ok", ["1"]))
check("ボタン: 本文に印が足されていたら送らない（見出しの印あり）", button(MARKED, shown), ("changed", []))
check("ボタン: 本文に印が足されていたら送らない（見出しの印なし）", button(MARKED_OFF, shown), ("changed", []))

bad = [f"{n}: {g!r}（期待 {w!r}）" for n, ok, g, w in results if not ok]
print("\n".join(bad) or f"{len(results)} 例、すべて期待どおり")
sys.exit(1 if bad else 0)
