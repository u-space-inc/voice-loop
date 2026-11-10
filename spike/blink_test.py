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

# ---- 取り消しにくい語の判定は、点滅の向きで変わらない（この形では）
check("取り消しにくい印は、印ありでも印なしでも付く", (bool(s.RISKY.search(s.asked_part(RISKY_ON))), bool(s.RISKY.search(s.asked_part(RISKY_OFF)))), (True, True))

# ---- 画面のボタン: 画面に出したときと押したときで点滅の向きが違っても通す。問いが替わったら断る
state["screen"] = ON
shown = s._asked("w9:p1", "sess-1")[1]["fp"]
check("ボタン: 印なしのときに押しても通る", button(OFF, shown), ("ok", ["1"]))
check("ボタン: コマンドが替わっていたら送らない", button(OFF.replace("ls -la", "ls -lA"), shown), ("changed", []))
check("ボタン: 別の承認に替わっていたら送らない", button(RISKY_OFF, shown), ("changed", []))

bad = [f"{n}: {g!r}（期待 {w!r}）" for n, ok, g, w in results if not ok]
print("\n".join(bad) or f"{len(results)} 例、すべて期待どおり")
sys.exit(1 if bad else 0)
