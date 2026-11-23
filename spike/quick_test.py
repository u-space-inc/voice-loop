"""S1（報告の声を鳴らすのは 1 か所）と S2（決まった問いに本体がその場で答える。scripts/quick.py）の試験。
本体の写しを TestClient で読み込み、Hermes の API・声・herdr は差し替える（常駐の 8790・本物の Hermes・コンテナ・herdr には触れない）。
確かめること:
- 形: 発言の全体が決まった形のときだけ受ける。承認・許可・はい・止めて・セッションの名前つきの問い・頼みごとは受けない（頭へ）
- 答え: 本体の状態から作り、問いの形にしない。「次」は続けて言うと次の件へ進み、時間がたつと最初から
- 相手の位置（守り）: 頭が承認の問いを出したあとに本体が答えると、次の発言の「直前に聞いた文」は空（その前の問いへの「はい」に使えない）。
  本体が答えた発言は Hermes に送らない。台帳と記録には載る（すぐ次の発言を進める）。機械からの発言は機械の印のまま
- 文脈: 次に Hermes に送る発言の前に、本体が答えたことを 1 回だけ添える（守りの台帳と記録の発言は利用者の言葉のまま）
- 鳴らす窓: 誰も話していなければすべて・最後に話しかけた窓だけ・車の機械がつながったら車・VL_RING=all ならすべて。
  報告の文字はすべての窓に届き、声を鳴らさない窓には quiet が付く
使い方: uv run spike/quick_test.py"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HERMES_HOME=f"{TMP}/hermes", VL_HERMES_KEY="k1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import quick  # noqa: E402
import server as s  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


# ---------- 形 ----------
TAKE = {"次は？": "next", "ヴィカ、次は？": "next", "ねえヴィカ次": "next", "次は何？": "next", "次ある？": "next",
        "どうなってる？": "status", "ねえ、いまどうなってる？": "status", "全体どうなってる？": "status", "状況は？": "status",
        "全体の状況を教えて": "status", "今なんか進行中のセッションありますか": "status", "今稼働中のセッションある": "status",
        "動いてるのある？": "status", "待ってるのある？": "waiting", "承認待ちある？": "waiting", "承認待ちは？": "waiting",
        "他のセッションで何か待機中になっているものはありますか?": "waiting", "止まってるセッションある？": "waiting"}
PASS = ["承認", "承認します", "許可して", "許可する", "はい", "いいえ", "1", "Aです", "待ってる", "止まってる", "止めて", "詳しく", "もう一回",
        "次はブレクサの件", "次のステップに進んで", "ブレクサどうなってる", "ブログの件どうなってる？", "他のセッションの完了状況は？",
        "進行中のセッションを止めて", "承認待ちのやつ許可して", "状況を教えてもらえますかその件の", "", "次" * 30]
check("形: 決まった問いは受ける（呼びかけ・句読点・丁寧な言い方の揺れも）", {t: quick.match(t, "ヴィカ") for t in TAKE}, TAKE)
check("形: 承認・許可・はい・止めて・名前つきの問い・頼みごとは受けない（頭へ）", [t for t in PASS if quick.match(t, "ヴィカ")], [])
check("形: 呼び名が設定と違っても、文の頭の呼びかけを外す", quick.match("ミカ、次は？", "ミカ"), "next")

# ---------- 答え ----------
SNAP = {"sessions": [{"pane": "w1:p1", "name": "blog（記事）", "status": "working"},
                     {"pane": "w1:p2", "name": "shop（受注）", "status": "blocked", "ask": "permission"},
                     {"pane": "w1:p3", "name": "lp（修正）", "status": "blocked", "ask": "question"},
                     {"pane": "w1:p4", "name": "idle（待機）", "status": "idle"}],
        "watching": 2, "inbox": [{"n": 31, "title": "見積もりの催促", "project": "shop"}], "inbox_problem": False}
check("答え: 待ち", quick.answer("waiting", SNAP), "承認・入力待ちは 2 件です。shop（受注）、lp（修正）。答えは画面の上に出ています。")
check("答え: 状況", quick.answer("status", SNAP),
      "作業中は 1 件です。blog（記事）。承認・入力待ちが 2 件あります。shop（受注）、lp（修正）。頼んだ件の見張り中は 2 件です。")
cur = {}
seq = [quick.answer("next", SNAP, cur, 100.0 + i) for i in range(4)]
check("答え: 「次」は承認・入力待ち → 判断待ちの順に 1 件ずつ進み、最後は「ほかにない」", seq,
      ["次は、shop（受注）が承認待ちです。このあと 2 件あります。", "次は、lp（修正）が入力待ちです。このあと 1 件あります。",
       "次は、判断待ちの「見積もりの催促」（shop）です。", "ほかに待っているものはありません（全部で 3 件でした）。"])
check("答え: 時間がたって（3 分）から「次」と言えば、最初から", quick.answer("next", SNAP, {"key": "i:31", "at": 0.0}, 1000.0),
      "次は、shop（受注）が承認待ちです。このあと 2 件あります。")
EMPTY = {"sessions": [], "watching": 0, "inbox": [], "inbox_problem": True}
check("答え: 何も無いとき", (quick.answer("waiting", EMPTY), quick.answer("status", EMPTY), quick.answer("next", EMPTY, {}, 1.0)),
      ("承認・入力待ちのセッションはありません。", "いま作業中のセッションはありません。", "いま、あなたを待っているものはありません。判断待ちの記録を読めませんでした。"))
MANY = {"sessions": [{"pane": f"w:{i}", "name": f"s{i}", "status": "working"} for i in range(5)]}
check("答え: 名前は 3 つまで、残りは数", quick.answer("status", MANY), "作業中は 5 件です。s0、s1、s2、ほか 2 件。")
outs = [quick.answer(k, SNAP, {}, 1.0) for k in quick.KINDS] + [quick.answer(k, EMPTY, {}, 1.0) for k in quick.KINDS]
check("答え: 問いの形にしない（？・か。で終わらない。守りは聞いた文が問いかを見る）", [o for o in outs if "?" in o or "？" in o or o.endswith("か。")], [])

# ---------- 鳴らす窓 ----------


class C:
    def __init__(self, win="", bound=False, place="", seen=0.0, connected=0.0):
        self.win, self.bound, self.place, self.seen, self.connected = win, bound, place, seen, connected


s.WIN_SPOKE.clear()
pc, phone, home = C("pcwindow01", seen=5), C("phonewin01", seen=4), C(bound=True, place="home", connected=1)
check("窓: 誰も話していなければ、すべての窓で鳴らす（取りこぼさない）", s.ring_targets([pc, phone, home]), [pc, phone, home])
s.WIN_SPOKE[s.win_of(phone)] = 10.0
check("窓: 最後に話しかけた窓だけ", s.ring_targets([pc, phone, home]), [phone])
s.WIN_SPOKE[s.win_of(home)] = 20.0
check("窓: 家の機械に話しかけたら家の機械", s.ring_targets([pc, phone, home]), [home])
car = C(bound=True, place="car", connected=30.0)
check("窓: 車の機械がつながったら車（乗った）", s.ring_targets([pc, phone, home, car]), [car])
s.WIN_SPOKE[s.win_of(phone)] = 40.0
check("窓: そのあと別の窓に話しかけたら、その窓", s.ring_targets([pc, phone, home, car]), [phone])
again = C("phonewin01", seen=9)
check("窓: 同じ窓の印でつなぎ直した接続が複数あれば、いちばん新しく様子を知らせた接続", s.ring_targets([phone, again, pc]), [again])
check("窓: 画面が家の機械と名乗っても、口で決まった機械の窓の印にはならない", s.win_of(C("dev:home", bound=False, place="home")), "dev:home")
keep = s.RING
s.RING = "all"
check("窓: VL_RING=all なら、いつもすべて", s.ring_targets([pc, phone, home, car]), [pc, phone, home, car])
s.RING = keep

# ---------- 本体の写しで（WebSocket） ----------
calls: list[str] = []
plan: list = []


def fake_ask_hermes(text):
    calls.append(text)
    return plan.pop(0) if plan else "わかりました"


async def nothing(*a, **k):
    return None


spoken: list[list[str]] = []


async def fake_speak_to(clients, text, cancel):
    """声は出さず、どの窓で鳴らしたかを覚える。本物と同じく、終わりの合図（done）は送る。"""
    spoken.append(sorted(getattr(c, "win", "") or "-" for c in clients))
    for cl in clients:
        await cl.send(type="done", sentences=0)


AGENTS = [{"pane": "w1:p2", "cwd": "/x/shop", "title": "受注", "status": "blocked", "kind": "claude", "session": "s2", "name": "", "hub": False},
          {"pane": "w1:p1", "cwd": "/x/blog", "title": "記事", "status": "working", "kind": "claude", "session": "s1", "name": "", "hub": False}]
s.hermes.ask_hermes = fake_ask_hermes
s.list_agents = lambda: [dict(a) for a in AGENTS]
s._ask_of = lambda a: {"kind": "permission"}
s.stepping = lambda a: False
s.speak_to = fake_speak_to
s.boot_ensure_hub = s.keep_gateway_token = s.watch_blocked = nothing
s.hermes.watch_hermes_skills = nothing
s.HUB.open_items = lambda: ([{"n": 7, "title": "請求書の確認", "project": "shop"}], False)
HP = s.hermes.HERMES_PANE
D = {"pane": "w1:p1", "cwd": "/x/blog", "project": "blog", "title": "記事", "text": "書いて", "offset": 0, "was_working": False, "gen": 1}


def until(w, kind):
    seen = []
    while not seen or seen[-1]["type"] != kind:
        seen.append(w.receive_json())
    return seen


def say(w, text):
    w.send_json({"type": "text", "text": text, "pane": HP, "speaker": 0})
    return until(w, "done") if text else []


def wait_until(fn, limit=5.0):
    end = time.time() + limit
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    with c.websocket_connect("ws://127.0.0.1:8790/ws") as a, c.websocket_connect("ws://127.0.0.1:8790/ws") as b:
        a.send_json({"type": "hello", "speaker": 0, "voice": True, "visible": True, "win": "aaaaaaaa01"})
        b.send_json({"type": "hello", "speaker": 0, "voice": True, "visible": True, "win": "bbbbbbbb02"})
        check("窓: 画面は hello で窓の印を名乗る（形の違う印は受けない）", wait_until(lambda: sorted(x.win for x in s.CLIENTS) == ["aaaaaaaa01", "bbbbbbbb02"]), True)
        b.send_json({"type": "hello", "speaker": 0, "voice": True, "visible": True, "win": "bad win!"})
        time.sleep(0.2)
        check("窓: 形の違う印は使わない（前の印のまま）", sorted(x.win for x in s.CLIENTS), ["aaaaaaaa01", "bbbbbbbb02"])

        # 守り: 頭が承認の問いを出した → 本体が決まった問いに答えた → 次の「はい」の直前に聞いた文は空
        plan.append("shop の承認、許可しますか？")
        say(a, "shop の様子を見て")
        check("前提: 頭の問いが、利用者の聞いた文になっている", s.hermes.HERMES_HEARD["text"], "shop の承認、許可しますか？")
        n_calls = len(calls)
        msgs = say(a, "次は？")
        reply = next((m for m in msgs if m["type"] == "reply"), {})
        check("S2: 決まった問いは本体が答える（Hermes に送らない）", (len(calls) - n_calls, reply.get("text")),
              (0, "次は、shop（受注）が承認待ちです。このあと 1 件あります。"))
        check("S2: 答えたあと、利用者の聞いた文は空", s.hermes.HERMES_HEARD["text"], "")
        row = list(s.hermes.HERMES_TURNS)[-1]
        check("S2: 台帳に発言と本体の答えが載る（すぐ次の発言を進める）", (row[1], row[2], row[3]),
              ("次は？", "次は、shop（受注）が承認待ちです。このあと 1 件あります。", "shop の承認、許可しますか？"))
        recs = [(d["role"], d["text"], d.get("by")) for d in s.hermes.hermes_records()][-2:]
        check("S2: 記録に発言と本体の答え（by=body）", recs, [("me", "次は？", None), ("ai", "次は、shop（受注）が承認待ちです。このあと 1 件あります。", "body")])
        hist = c.get("/api/history", params={"pane": HP}).json()["messages"]
        check("S2: 履歴では本体が答えた印（quick）", [m.get("quick") for m in hist[-2:]], [None, True])
        turn_yes = s.hermes.hermes_turn_begin("はい")  # このあと利用者が「はい」と言った（承認に答える守りが見る形）
        check("相手の位置: 本体が答えたあとの「はい」は、その前の頭の問いへの答えに使えない（直前に聞いた文が空）", s.HUB.heard(turn_yes), "")
        s.hermes.HERMES_TURNS.pop()
        say(a, "待ってるのある？")
        n_calls = len(calls)
        plan.append("blog は進んでいます")
        say(a, "ブログの件どうなってる？")
        check("文脈: 次に Hermes に送る発言の前に、本体が答えたこと（2 件）を添える",
              (len(calls) - n_calls, calls[-1].startswith("〔このあいだ、本体（voice-loop）が決まった問いにその場で答えました"),
               "「次は？」→" in calls[-1] and "「待ってるのある？」→" in calls[-1], calls[-1].endswith("ブログの件どうなってる？")), (1, True, True, True))
        check("文脈: 守りの台帳と記録の発言は、利用者の言葉のまま", (list(s.hermes.HERMES_TURNS)[-1][1], s.hermes.hermes_records()[-2]["text"]),
              ("ブログの件どうなってる？", "ブログの件どうなってる？"))
        plan.append("了解です")
        say(a, "ブログを公開して")
        check("文脈: 添えるのは 1 回だけ", calls[-1], "ブログを公開して")
        keep_q = s.QUICK
        s.QUICK = False
        say(a, "次は？")
        check("VL_QUICK=off なら、決まった問いも Hermes に送る", calls[-1], "次は？")
        s.QUICK = keep_q

        # S1: 報告。最後に話しかけた窓（a）だけで声、文字は両方。b には quiet
        spoken.clear()
        plan.append("blog の記事が書けました")
        c.portal.call(s.report, D, "done")
        ra, rb = until(a, "reply")[-1], until(b, "reply")[-1]
        check("S1: 報告の文字はすべての窓に届く", (ra.get("text"), rb.get("text")), ("blog の記事が書けました", "blog の記事が書けました"))
        check("S1: 声は最後に話しかけた窓だけ。ほかの窓は quiet", (spoken, ra.get("quiet"), rb.get("quiet")), ([["aaaaaaaa01"]], None, True))
        say(b, "次は？")
        spoken.clear()
        plan.append("続きです")
        c.portal.call(s.report, D, "done")
        until(a, "reply"), until(b, "reply")
        check("S1: 別の窓に話しかけたら、そちらで鳴らす", spoken, [["bbbbbbbb02"]])

    # 機械からの決まった問い: 台帳は機械の印（承認に答える・閉じるに使わない）のまま
    with c.websocket_connect("ws://127.0.0.1:8790/ws") as dv:
        dv.send_json({"type": "hello", "speaker": 0, "voice": True, "kind": "stackchan", "place": "home", "win": "devwin0001"})
        say(dv, "どうなってる？")
        row = list(s.hermes.HERMES_TURNS)[-1]
        check("S2: 機械からの決まった問いも、台帳は機械の印", (row[1], row[7] if len(row) > 7 else None), ("どうなってる？", "device"))

bad = [r for r in results if not r[1]]
for name, ok, got, want in results:
    print(("✓ " if ok else "✗ ") + name + ("" if ok else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{len(results) - len(bad)}/{len(results)} 件が期待どおり")
sys.exit(1 if bad else 0)
