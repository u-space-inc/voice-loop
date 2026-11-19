"""選択肢の問い（AskUserQuestion）に、画面から 1 問ずつ答える本体の口の試験（/api/prompts の step・/api/answer の choice=step）。
herdr・本物のセッションには触れない（一覧・画面・キーと文字の送信を、偽物の画面 FakeTUI に差し替える）。
確かめること:
- いまの 1 問（か確認の画面）を出す。確認の画面（操作案内が無い）も問いとして出す。画面を読めなければ出さない
- 1 回押すごとに、見せたのと同じ画面（指紋）にだけ送る。画面が変わっていれば送らない。同じ画面に重ねて送らない
- 取り消しにくい問いは、印の付け外しのほかは、もう一度押してから
- 通し: 押すたびに出し直したいまの 1 問で、最後まで答えが届く
- 途中で思った形にならなければ止まり、理由を返す。キー・文字を送る herdr の呼び出しには時間切れを付ける
- 相手の位置（コンテナの中の Hermes）からは、1 問ずつの押し方では答えられない。コンテナに新しい口は開けていない
使い方: uv run spike/answer_form_test.py"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state")  # 本物の設定フォルダを読まない
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server as s  # noqa: E402
from ask_tui import REAL_SHOP_INPUT, REAL_TWO_INPUT, FakeTUI  # noqa: E402

s.ask_form.WAIT, s.ask_form.STEP = 0.3, 0.01
AGENT = {"pane": "w9:p1", "title": "テスト", "cwd": "/tmp/proj", "kind": "claude", "session": "sess-1", "status": "blocked", "name": "", "hub": False}
st = {"tui": FakeTUI(REAL_TWO_INPUT), "status": "blocked"}
TIMEOUTS = []


def screen():
    t = st["tui"].screen()
    return "\n".join(l for l in t.split("\n") if l.strip() and not set(l.strip()) <= set("─"))


def fake_herdr(*a, **k):
    if a[:2] in (("agent", "send-keys"), ("pane", "send-text")):
        TIMEOUTS.append(k.get("timeout"))
    if a[:2] == ("agent", "send-keys"):
        st["tui"].keys(a[-1])
    elif a[:2] == ("pane", "send-text"):
        st["tui"].text(a[-1])
        return {"raw": "", "code": 0}  # 本物の send-text は、うまくいくと何も出さない（JSON が無い）
    return {"result": {}}


REAL_READ_DIALOG = s.read_dialog
s.list_agents = lambda: [{**AGENT, "status": st["status"]}]
s.pane_shows_trust = lambda pane: False
s.read_prompt = lambda pane: screen()
s.read_dialog = lambda pane, *_: st["tui"].screen()
s.herdr = fake_herdr
results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


def setup(inp=REAL_TWO_INPUT, quirks=()):
    st.update(tui=FakeTUI(inp, quirks), status="blocked")
    s.ANSWERED.clear()


def prompt():
    return json.loads(s.api_prompts().body)[0]


def press(q=None, **body):
    q = q or prompt()
    return json.loads(asyncio.run(s.api_answer({"pane": "w9:p1", "key": "sess-1", "fp": q["fp"], "sfp": q.get("sfp"), "choice": "step", **body})).body)


# ---------- いまの 1 問を出す ----------
setup()
q = prompt()
check("一覧: いまの 1 問（見出し・問い・選択肢・自由記入の欄）と画面の指紋を出す",
      (q["kind"], q["step"]["stage"], q["step"]["question"], [o["label"] for o in q["step"]["options"]], q["step"]["free"], len(q["sfp"])),
      ("question", "question", "朝はどれ？", ["パン", "ごはん"], 3, 40))
for k in ("2", "1", "tab"):
    st["tui"].keys(k)
q = prompt()
check("一覧: 確認の画面（操作案内が無い）も問いとして出す", (q["kind"], q["step"]["stage"], q["step"]["answers"]), ("question", "review", [["朝はどれ？", "ごはん"], ["好きな色は？（複数可）", "赤"]]))
setup()
s.read_dialog = lambda pane, *_: (_ for _ in ()).throw(RuntimeError("画面が空だった"))
check("一覧: 画面を読めなければ、いまの 1 問を出さない", "step" in prompt(), False)
s.read_dialog = lambda pane, *_: st["tui"].screen()

# ---------- 1 回押すごとに送る ----------
setup()
rs = [press(act="pick", n=2), press(act="toggle", n=1), press(act="toggle", n=3), press(act="next"), press(act="submit")]
check("通し: 押すたびに出し直した画面で 1 手ずつ送り、最後まで届く",
      ([r.get("ok") for r in rs], [r.get("said") for r in rs][:2], st["tui"].result, st["tui"].log),
      ([True] * 5, ["2番「ごはん」", "1番「赤」の印を付けた"], ["ごはん", "赤, 緑"], ["2", "1", "3", "tab", "1"]))
setup()
s.STEPPING.clear()
press(act="pick", n=2)
press(act="toggle", n=1)
press(act="next")
st["status"] = "done"  # 確認の画面を、herdr が止まっていると見ないことがある（小さいペインの実画面で見た）
q = prompt()
r = press(q, act="submit")
check("答えている途中のセッションは、herdr が止まっていると見ていなくても、確認の画面を出して送れる",
      (q["step"]["stage"], r.get("ok"), st["tui"].result), ("review", True, ["ごはん", "赤"]))
s.STEPPING.clear()
setup()
st["status"] = "done"
check("答えていないセッションは、herdr が止まっていると見ていなければ読まない", json.loads(s.api_prompts().body), [])
setup()
q = prompt()
st["tui"].keys("1")  # 見せたあとで、端末で答えた
r = press(q, act="pick", n=2)
check("画面が変わっていたら送らない（端末で答えた・次の問いに進んだ）", (r.get("reason"), st["tui"].log), ("changed", ["1"]))
setup()
q = prompt()
r1 = press(q, act="pick", n=1)
check("1 問ずつの答えが届いたあとも、ハブ・承認のボタンが比べる指紋（prompt_fp）を残し、続く 1 問ずつの答えは sfp だけで見る（codex-review）",
      (s.answered("w9:p1", q["sfp"]), s.answered("w9:p1", q["fp"]), s.answered("w9:p1", q["fp"], by="step")), (True, True, False))
st["tui"] = FakeTUI(REAL_TWO_INPUT)  # 同じ画面に戻った（重ねた押し）
r2 = press(q, act="pick", n=1)
check("同じ画面に重ねて送らない", (r1.get("ok"), r2.get("reason"), st["tui"].log), (True, "answered", []))
setup()
s.read_prompt = lambda pane: "（画面の下の一部。問いが変わっても同じ）\nEnter to select · Esc to cancel"  # prompt_fp が次の問いでも変わらない形
q = prompt()
s.SEEN_PROMPT["w9:p1"] = (q["fp"], "t1", "sess-1")  # ハブはこの問いを見て、利用者に伝えていた
r = press(q, act="pick", n=1)
q2 = prompt()
check("1 問ずつ答えたら、ハブが前に見た問いの記録を消し、次の問いでも prompt_fp が同じならハブ・承認のボタンを止める（codex-review）",
      (r.get("ok"), "w9:p1" in s.SEEN_PROMPT, q2["fp"] == q["fp"], s.answered("w9:p1", q2["fp"])), (True, False, True, True))
r = press(q2, act="toggle", n=1)
check("続く 1 問ずつの答えは、前の答えが残した prompt_fp では止めない（次の問いの指紋が同じでも）", r.get("ok"), True)
s.read_prompt = lambda pane: screen()
setup()
q = prompt()
r = press(q, act="pick", n=9)
check("1 問ずつの答えが途中で止まったら、ハブ・承認のボタンが比べる指紋（prompt_fp）でも、同じ画面に重ねて送らない（codex-review）",
      (r.get("reason"), s.answered("w9:p1", q["sfp"]), s.answered("w9:p1", q["fp"])), ("stuck", True, True))
setup()
for body, why in ((dict(act="jump"), "bad_choice"), (dict(act="pick", n=9), "stuck"), (dict(act="toggle", n=1), "stuck"), (dict(act="free", text=""), "stuck")):
    s.ANSWERED.clear()
    r = press(**body)
    check(f"送らない: {body}", (r.get("reason"), st["tui"].log), (why, []))
risky = {"questions": [{"question": "本番に反映しますか？", "header": "反映", "options": [{"label": "反映する"}, {"label": "やめる"}]},
                       {"question": "色は？", "header": "色", "multiSelect": True, "options": [{"label": "赤"}, {"label": "青"}]}]}
setup(risky)
q = prompt()
r1 = press(q, act="pick", n=1)
r2 = press(q, act="pick", n=1, confirmed=True)
r3 = press(act="toggle", n=1)
check("取り消しにくい問いは、もう一度押してから（印の付け外しは 1 回で）", (q["risky"], r1.get("reason"), r2.get("ok"), r3.get("ok")), (True, "confirm", True, True))
setup(quirks=["no_typing"])
r = press(act="free", text="3番の朝ごはん")
check("途中で思った形にならなければ止まり、理由を返す（文字を打たない）", (r.get("reason"), bool(r.get("why")), st["tui"].log), ("stuck", True, ["down", "down"]))
setup(REAL_SHOP_INPUT)
r = press(act="free", text="駅前の店")
check("自由記入を送る（↓ で欄へ → 文字 → Enter）", (r.get("ok"), st["tui"].log), (True, ["down", "down", "text:駅前の店", "enter"]))
setup(REAL_SHOP_INPUT)
real_h = s.herdr
s.herdr = lambda *a, **k: {"raw": "error: no such pane", "code": 1} if a[:2] == ("pane", "send-text") else real_h(*a, **k)
r = press(act="free", text="駅前の店")
s.herdr = real_h
check("文字を送る herdr が失敗で終わったら止まり、Enter を送らない", (r.get("reason"), st["tui"].log), ("stuck", ["down", "down"]))
check("キー・文字を送る herdr の呼び出しには、5 秒以下の時間切れを付ける", bool(TIMEOUTS) and all(t is not None and 0 < t <= 5 for t in TIMEOUTS), True)
setup()
real = s.read_dialog
s.read_dialog = lambda pane, *_: (_ for _ in ()).throw(__import__("subprocess").TimeoutExpired("herdr", 5)) if st["tui"].log else real(pane)
r = press(act="pick", n=2)
s.read_dialog = real
check("送ったあとの読み取りが失敗したら、送れたとせず「結果が分からない」と返す", (r.get("ok"), r.get("reason"), "結果が分からない" in (r.get("why") or "")), (False, "stuck", True))

setup()
q = prompt()
other = FakeTUI({"questions": [{"question": "全部消しますか？", "header": "消す", "options": [{"label": "全削除"}, {"label": "やめる"}]}]})
calls = {"n": 0}


def swap(pane, *_):  # 本体が照らす読み取り（1 回目）は見せた画面、送る前の読み取り（2 回目から）は別の問いの画面
    calls["n"] += 1
    return shown if calls["n"] == 1 else other.screen()


shown = st["tui"].screen()  # 利用者に見せた画面
s.read_dialog = swap
st["tui"] = other  # キーは別の問いのほうへ届く
s.read_prompt = lambda pane: "\n".join(l for l in shown.split("\n") if l.strip() and not set(l.strip()) <= set("─"))
r = press(q, act="pick", n=1)
s.read_dialog = lambda pane, *_: st["tui"].screen()
s.read_prompt = lambda pane: screen()
check("照らしたあとで別の問いの画面に変わったら、キーを送らない（見せた画面の指紋を、送る前の読み取りでも照らす）", (r.get("reason"), other.log, other.result), ("stuck", [], None))

# ---------- 相手の位置 ----------
setup()
q = prompt()
r = json.loads(asyncio.run(s.hub_answer({"pane": "w9:p1", "sfp": q["sfp"], "act": "pick", "n": 1, "answers": [{"pick": 1}],
                                         "origin": {"platform": "slack", "chat": "D0TESTCHAT1", "thread": ""}})).body)
check("コンテナの口（/api/hub/answer）は、1 問ずつの押し方（act・n・sfp）を受けない（Slack の会話からと名乗っても、キーを送らない。"
      "声の会話の「はい・番号」は、本体が利用者の発言を自分の記録で確かめる今までの守りで、hermes_hub_test が確かめる）", (r.get("ok"), st["tui"].log), (False, []))
check("口: コンテナから使える口の一覧に、問いを読む・1 問ずつ答える口は足していない",
      any("question" in path or "step" in path for _, path in __import__("origin_guard").DOCKER_ALLOWED), False)

# ---------- 画面の読み取り ----------
import subprocess as _sp  # noqa: E402
_run = s.subprocess.run
outs = []
for rc, stdout in ((1, ""), (0, ""), (0, "\n\n"), (0, "  📂 voice-loop (main) | 🧠 7%\n  ⏵⏵ auto mode on\n")):
    s.subprocess.run = lambda *a, rc=rc, stdout=stdout, **k: _sp.CompletedProcess(a[0], rc, stdout, "")
    try:
        REAL_READ_DIALOG("w9:p1")
        outs.append("読めた")
    except RuntimeError:
        outs.append("例外")
s.subprocess.run = _run
check("画面の読み取りが失敗で終わった・空（状態表示の行だけも）なら、読めなかったとみなす（問いが消えたと取り違えない）", outs, ["例外"] * 4)

ok = sum(r[1] for r in results)
for name, passed, got_, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got_!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
