"""選択肢の問い（AskUserQuestion）に、問いの形で答える本体の口の試験（/api/prompts の form・/api/answer の choice=form・/api/hub/question・
/api/hub/answer の answers）。herdr・本物のセッションには触れない（一覧・画面・キーと文字の送信を、偽物の画面 FakeTUI に差し替える）。
確かめること:
- 問いの形を出すのは、記録に答えを待つ問いがあり、画面に最初の問いが出ていて、まだ答えていないときだけ（端末で答え始めていたら出さない）
- 画面のボタン: 見せたのと同じ問い（指紋と記録の id）にだけ・答えの形を確かめて・取り消しにくい問いはもう一度押してから・同じ問いに重ねて送らない
- Slack（Hermes を通る口）: Slack の会話から（hubctl が付ける origin）だけ・1 時間の数の上限・答えるたび（途中で止まったときも）画面の知らせとプッシュ通知・
  自動通知への応答中は送らない・相手の位置（コンテナ）からも、見本つきの問いへの自由記入など形の外の答えは送れない
- 途中で止まったら、それ以上は送らず、理由を返す
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
st = {"tui": FakeTUI(REAL_TWO_INPUT), "path": None, "status": "blocked"}
pushes, notices_sent, watches = [], [], []


def transcript(tid, inp, answered=False):
    p = Path(tempfile.mkdtemp()) / "sess-1.jsonl"
    rows = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": "AskUserQuestion", "input": inp}]}}]
    if answered:
        rows.append({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}})
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    return p


def screen():
    t = st["tui"].screen()
    return "\n".join(l for l in t.split("\n") if l.strip() and not set(l.strip()) <= set("─"))


def fake_herdr(*a, **k):
    if a[:2] == ("agent", "send-keys"):
        st["tui"].keys(a[-1])
    elif a[:2] == ("pane", "send-text"):
        st["tui"].text(a[-1])
    return {"result": {}}


s.list_agents = lambda: [{**AGENT, "status": st["status"]}]
s.pane_shows_trust = lambda pane: False
s.read_prompt = lambda pane: screen()
s.read_dialog = lambda pane: st["tui"].screen()
s.jsonl_path = lambda a: st["path"]
s.herdr = fake_herdr


async def fake_push(title, body, tag, urgency="normal", only=None):
    pushes.append((title, body))
    return "ok"
s.push_all = fake_push
s.start_watch = lambda d, kind="send": watches.append(kind)
results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


def setup(inp=REAL_TWO_INPUT, tid="t1", quirks=(), answered=False):
    st.update(tui=FakeTUI(inp, quirks), path=transcript(tid, inp, answered), status="blocked")
    s.ANSWERED.clear()
    s._PENDING.clear()
    s._slack_forms.clear()


def prompts():
    return json.loads(s.api_prompts().body)


def screen_answer(**body):
    return json.loads(asyncio.run(s.api_answer({"pane": "w9:p1", "key": "sess-1", "choice": "form", **body})).body)


def hub_answer(origin=True, **body):
    b = {"pane": "w9:p1", **body, **({"origin": {"platform": "slack", "chat": "D1", "thread": "1.2"}} if origin else {})}
    return json.loads(asyncio.run(s.hub_answer(b)).body)


# ---------- 問いの形を出す ----------
setup()
q = prompts()[0]
check("一覧: 記録の問いを問いの形で出す（記録の id・問いごとの形）", (q["kind"], q["ask"], [(x["multi"], x["other"]) for x in q["form"]["questions"]]),
      ("question", "t1", [(False, True), (True, False)]))
setup(answered=True)
check("一覧: 記録の問いに答えがあれば出さない", "form" in prompts()[0], False)
setup()
st["tui"].keys("2")  # 端末で 1 つ目に答えた
check("一覧: 端末で答え始めていたら出さない（続きは端末で）", "form" in prompts()[0], False)
MULTI1 = {"questions": [REAL_TWO_INPUT["questions"][1]]}
setup(MULTI1)
st["tui"].keys("1")  # 端末で、1 つ目（最初）の問いの中で印を付けた（次の問いへは進んでいない）
check("一覧: 最初の問いの中で答え始めていても出さない（印）", ("form" in prompts()[0], st["tui"].i), (False, 0))
setup({"questions": [REAL_TWO_INPUT["questions"][0]]})
st["tui"].keys("3")  # 端末で、自由記入の欄に入った
check("一覧: 最初の問いの中で答え始めていても出さない（自由記入の欄）", "form" in prompts()[0], False)
setup()
st["tui"] = FakeTUI(REAL_SHOP_INPUT)  # 記録と違う問いが画面に出ている
check("一覧: 画面の問いが記録と違えば出さない", "form" in prompts()[0], False)

# ---------- 画面のボタン ----------
setup()
q = prompts()[0]
ok_two = [{"pick": 2}, {"picks": [1, 3]}]
r = screen_answer(fp=q["fp"], ask=q["ask"], answers=ok_two)
check("画面: 問いの形で答えると、1 手ずつ送って届く", (r, st["tui"].result, st["tui"].log),
      ({"ok": True, "said": "［朝］ごはん／［色］赤, 緑", "project": "proj", "title": "テスト"}, ["ごはん", "赤, 緑"], ["2", "1", "3", "tab", "1"]))
setup()
q = prompts()[0]
for name, body, why in (("指紋が違う", dict(fp="0" * 40, ask=q["ask"], answers=ok_two), "changed"),
                        ("記録の id が違う", dict(fp=q["fp"], ask="t9", answers=ok_two), "changed"),
                        ("答えの形が違う", dict(fp=q["fp"], ask=q["ask"], answers=[{"pick": 2}]), "bad_form"),
                        ("複数選ぶ問いに 1 つ", dict(fp=q["fp"], ask=q["ask"], answers=[{"pick": 2}, {"pick": 1}]), "bad_form"),
                        ("自由記入できない問いに自由記入", dict(fp=q["fp"], ask=q["ask"], answers=[{"pick": 2}, {"other": "黄"}]), "bad_form")):
    r = screen_answer(**body)
    check(f"画面: {name}なら送らない", (r.get("reason"), st["tui"].log), (why, []))
r = screen_answer(fp=q["fp"], ask=q["ask"], key="sess-2", answers=ok_two)
check("画面: 別のセッション（key が違う）なら送らない", (r.get("reason"), st["tui"].log), ("gone", []))
risky = {"questions": [{"question": "本番に反映しますか？", "header": "反映", "options": [{"label": "反映する"}, {"label": "やめる"}]}]}
setup(risky)
q = prompts()[0]
r1 = screen_answer(fp=q["fp"], ask=q["ask"], answers=[{"pick": 1}])
r2 = screen_answer(fp=q["fp"], ask=q["ask"], answers=[{"pick": 1}], confirmed=True)
check("画面: 取り消しにくい問いは、もう一度押してから", (q["risky"], r1.get("reason"), r2["ok"], st["tui"].result), (True, "confirm", True, ["反映する"]))
setup()
q = prompts()[0]
st["tui"].qs = st["tui"].qs  # 同じ問い
s.ANSWERED["w9:p1"] = (q["fp"], __import__("time").time())
r = screen_answer(fp=q["fp"], ask=q["ask"], answers=ok_two)
check("画面: 答えたばかりの同じ問いに重ねて送らない", (r.get("reason"), st["tui"].log), ("answered", []))
setup(quirks=["review_wrong"])
q = prompts()[0]
r = screen_answer(fp=q["fp"], ask=q["ask"], answers=ok_two)
check("画面: 途中で思った形にならなければ止まり、理由を返す（確認の画面で 1 を押さない）", (r.get("ok"), r.get("reason"), bool(r.get("why")), st["tui"].log[-1], st["tui"].result),
      (False, "stuck", True, "tab", None))
r = screen_answer(fp=q["fp"], ask=q["ask"], answers=ok_two)
check("画面: 途中で止まった問いには、続けて送らない", r.get("reason"), "answered")

# ---------- Slack（Hermes を通る口） ----------
setup()
hq = json.loads(s.hub_question("w9:p1").body)
check("hub question: 問いの形と指紋・記録の id", (hq["kind"], hq["ask"], hq["fp"] == prompts()[0]["fp"], len(hq["form"]["questions"])), ("question", "t1", True, 2))
r = hub_answer(origin=False, fp=hq["fp"], ask=hq["ask"], answers=ok_two)
check("Slack: Slack の会話から（origin）でなければ送らない", (r.get("reason"), st["tui"].log), ("not_slack", []))
s.REPORTING = True
r = hub_answer(fp=hq["fp"], ask=hq["ask"], answers=ok_two)
s.REPORTING = False
check("Slack: 自動通知への応答中は送らない", (r.get("reason"), st["tui"].log), ("reporting", []))
pushes.clear()
watches.clear()
r = hub_answer(fp=hq["fp"], ask=hq["ask"], answers=ok_two)
nt = s.notices()["rows"]
check("Slack: 送って届き、画面の知らせとプッシュ通知で知らせ、結果の見張りを付ける",
      (r.get("ok"), st["tui"].result, (nt[-1]["title"] if nt else None), (nt[-1]["text"] if nt else None), len(pushes), watches),
      (True, ["ごはん", "赤, 緑"], "Slack から答えました: proj", "［朝］ごはん／［色］赤, 緑", 1, ["answer"]))
setup(REAL_SHOP_INPUT, quirks=["no_typing"])
hq = json.loads(s.hub_question("w9:p1").body)
r = hub_answer(fp=hq["fp"], ask=hq["ask"], answers=[{"other": "3番の店"}, {"pick": 1}])
check("Slack: 途中で止まったときも知らせる（文字は打たない）", (r.get("reason"), (s.notices()["rows"] or [{}])[-1].get("title"), any(x.startswith("text:") for x in st["tui"].log)),
      ("stuck", "Slack から答える途中で止まりました: proj", False))
setup(REAL_SHOP_INPUT)
hq = json.loads(s.hub_question("w9:p1").body)
r = hub_answer(fp=hq["fp"], ask=hq["ask"], answers=[{"pick": 1}, {"other": "丸"}])
check("Slack（相手の位置）: 見本つきの問いへの自由記入など、形の外の答えは送れない", (r.get("reason"), st["tui"].log), ("bad_form", []))
setup()
s._slack_forms[:] = [__import__("time").time()] * s.SLACK_FORM_PER_HOUR
hq = json.loads(s.hub_question("w9:p1").body)
r = hub_answer(fp=hq["fp"], ask=hq["ask"], answers=ok_two)
check("Slack: 1 時間の数の上限を超えたら送らない", (r.get("reason"), r.get("max"), st["tui"].log), ("too_many", s.SLACK_FORM_PER_HOUR, []))
setup()
hq = json.loads(s.hub_question("w9:p1").body)
s.ANSWERED["w9:p1"] = (hq["fp"], __import__("time").time())
r = hub_answer(fp=hq["fp"], ask=hq["ask"], answers=ok_two)
check("Slack: 答えたばかりの同じ問いに重ねて送らない（画面のボタンの答えと共有）", (r.get("reason"), st["tui"].log), ("answered", []))
setup()
st["status"] = "working"
r = hub_answer(fp="x", ask="t1", answers=ok_two)
check("Slack: 止まっていなければ送らない", r.get("reason"), "not_blocked")
check("口: hub/question はコンテナから使える口の一覧にある", ("GET", "/api/hub/question") in __import__("origin_guard").DOCKER_ALLOWED, True)

ok = sum(r[1] for r in results)
for name, passed, got_, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got_!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
