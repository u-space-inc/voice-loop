"""「止める」を押した・話しかけ直したら、利用者は返事を聞いていない、の試験（承認に答える・閉じるの守り）。
2026-11-24: 顔の表示で、考えている・話しているときに顔を押すと止めるようにした（R12）。止めても頭（Hermes）との往復は最後まで進み、
届いた返事を本体が「利用者が聞いた文」にしていた。返事が「〇〇を閉じていいですか？」なら、聞いていない問いへの「はい」で閉じられうる。
受け付けの番号（発言を受け付けたとき・止めたときに進む。時計は使わない）で、受け付けた・頼んだあとに番号が進んだ返事は「聞いた文」にしない（a2a 30fd5b94）。
見ること:
  - 読んでいる途中で止めたら、直前に聞いた文と、台帳の最新の返事（閉じるの守りが問いとして読む欄）を空にする
  - 待つ間に止めたら、あとで届いた返事は、聞いた文にも台帳の返事の欄にも置かない（記録には残す）
  - 受け付けたあと、頭に送る前（順番待ち・添付の用意）に止めても同じ（本物の WebSocket の流れで、添付の用意の間に止める）
  - 返事を待つ間に次の発言を受け付けたら、前の返事は聞いていない（読まれない）
  - 止めたあとに受け付けた往復の返事は、ふつうに聞いた文になる
  - 時計が戻っても、順を取り違えない（時計を使わない）
  - 起動し直したら、台帳の返事の欄は記録から戻さない（閉じるのは聞き直してから）
  - 報告: 頼んだあとに止めた・話しかけたら、届いた報告は聞いた文にしない（どちらも無ければ聞いた文）。本物の _report で、鍵の順番待ちの間に止めても同じ
  - 画面の「停止」（WebSocket の stop）で、話し相手に止めたことが伝わる
本物の Hermes・herdr・コンテナには触れない（頭の往復は差し替える。VL_HERMES_URL は何も待ち受けていないポート）。
使い方: uv run spike/stop_test.py"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HERMES_HOME=f"{TMP}/hermes", VL_HERMES_KEY="k1",
                  VL_HERMES_URL="http://127.0.0.1:9", VL_WHISPER="http://127.0.0.1:9/v1/audio/transcriptions", VL_VOICE_URL="http://127.0.0.1:9")
os.environ.pop("VL_HUB", None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import server as s  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

hm = s.hermes
results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


def ai_rows(turn):
    p = Path(hm.HERMES_LOG)
    return [d["text"] for d in (json.loads(x) for x in p.read_text().splitlines() if x.strip()) if d.get("role") == "ai" and d.get("turn") == turn]


Q = "proj のセッションを閉じていいですか？"

# ---- 読んでいる途中で止める
hm.ask_hermes = lambda text: Q
asyncio.run(hm.hermes_exchange("proj を閉じて"))
t1 = s.hub_human_turn()
check("前提: 止めなければ、返事は聞いた文で、台帳の返事の欄にも載る", (hm.HERMES_HEARD["text"], s.hub_reply_to(t1)), (Q, Q))
s.HUB.stopped()
check("読んでいる途中で止めたら、聞いた文と台帳の最新の返事を空にする（閉じるの守りに聞き直してもらう）", (hm.HERMES_HEARD["text"], s.hub_reply_to(t1)), ("", ""))

# ---- 待つ間に止める（返事は止めたあとに届く）
gate, entered = threading.Event(), threading.Event()


def slow(text):
    entered.set()
    gate.wait(5)
    return Q


async def while_waiting(between, admitted=None):
    """往復を始め、頭が返事を作っている間に between() を呼び、返事を届ける。"""
    gate.clear()
    entered.clear()
    task = asyncio.create_task(hm.hermes_exchange("proj を閉じて", admitted=admitted))
    await asyncio.to_thread(entered.wait, 5)
    between()
    gate.set()
    return await task


hm.ask_hermes = slow
reply, why, saved = asyncio.run(while_waiting(s.HUB.stopped))
t2 = s.hub_human_turn()
check("待つ間に止めたら、あとで届いた返事は、聞いた文にも台帳の返事の欄にも置かない", (reply, why, hm.HERMES_HEARD["text"], s.hub_reply_to(t2)), (Q, "", "", ""))
check("…記録には残す（画面の履歴では読める）", ai_rows(t2), [Q])

# ---- 受け付けたあと、頭に送る前に止める（受け付けの番号は、受けたその場で取る）
adm = s.HUB.admit()
s.HUB.stopped()
hm.ask_hermes = lambda text: Q
asyncio.run(hm.hermes_exchange("proj を閉じて", admitted=adm))
check("受け付けたあと、頭に送る前に止めたら、返事は聞いた文にしない", (hm.HERMES_HEARD["text"], s.hub_reply_to(s.hub_human_turn())), ("", ""))

# ---- 返事を待つ間に次の発言を受け付けた
hm.ask_hermes = slow
adm = s.HUB.admit()
asyncio.run(while_waiting(s.HUB.admit, admitted=adm))
check("返事を待つ間に次の発言を受け付けたら、前の返事は聞いていない（読まれない）", (hm.HERMES_HEARD["text"], s.hub_reply_to(s.hub_human_turn())), ("", ""))

# ---- 時計が戻る（時計は使わない）
real_time = time.time
try:
    clock = iter([1000.0] + [999.0] * 1000)
    time.time = lambda: next(clock)
    reply, why, saved = asyncio.run(while_waiting(s.HUB.stopped))
finally:
    time.time = real_time
check("時計が戻っても、止めたあとに届いた返事は聞いた文にしない", (hm.HERMES_HEARD["text"], s.hub_reply_to(s.hub_human_turn())), ("", ""))

# ---- 止めたあとに受け付けた往復
hm.ask_hermes = lambda text: Q
adm = s.HUB.admit()
asyncio.run(hm.hermes_exchange("もう一度、proj を閉じて", admitted=adm))
t3 = s.hub_human_turn()
check("画面からの発言の返事は、画面に送り終えるまでは聞いた文にしない", (hm.HERMES_HEARD["text"], s.hub_reply_to(t3)), ("", ""))
s.HUB.delivered(adm)  # 画面に送り終えた（本体の画面の turn が呼ぶ）
check("止めたあとに受け付けた往復の返事は、ふつうに聞いた文になる", (hm.HERMES_HEARD["text"], s.hub_reply_to(t3)), (Q, Q))

# ---- 画面に送り終える前に、次の発言を受け付けた・止めた
for name, between in [("次の発言を受け付けた", s.HUB.admit), ("止めた", s.HUB.stopped)]:
    adm = s.HUB.admit()
    asyncio.run(hm.hermes_exchange("proj を閉じて", admitted=adm))
    between()
    s.HUB.delivered(adm)  # 遅れて送り終えても
    check(f"画面に送り終える前に{name}ら、あとで送り終えても聞いた文にしない", (hm.HERMES_HEARD["text"], s.hub_reply_to(s.hub_human_turn())), ("", ""))

# ---- 承認に答える・閉じるの直前に見る「その発言のあとに止めていないか」
turn = hm.hermes_turn_begin("はい")
check("発言のすぐあとは、いまの発言", s.HUB.current(turn), True)
s.HUB.stopped()
check("止めたら、その前の発言はもういまの発言ではない（答えを送らない・閉じない）", s.HUB.current(turn), False)
turn = hm.hermes_turn_begin("はい")
s.HUB.admit()
check("次の発言を受け付けても、前の発言はいまの発言ではない", s.HUB.current(turn), False)
check("台帳に無い発言は、いまの発言ではない（断る側）", s.HUB.current("hzzzzzzzzzzzz"), False)

# ---- 起動し直し
hm.HERMES_TURNS.clear()
hm._load_hermes_turns()
check("起動し直したら、台帳の返事の欄は記録から戻さない（聞いた返事も。閉じるのは聞き直してから）",
      (s.hub_reply_to(t1), s.hub_reply_to(t2), s.hub_reply_to(t3)), ("", "", ""))
check("起動し直す前の発言は、いまの発言ではない（答えを送らない・閉じない）", s.HUB.current(t3), False)

# ---- 報告
for name, between, want in [("止めた", s.HUB.stopped, ""), ("話しかけた", s.HUB.admit, ""), ("どちらも無い", lambda: None, "proj の報告です。閉じていいですか？")]:
    hm.HERMES_HEARD["text"] = ""
    mark = s.HUB.heard_mark()  # 頼んだその場で控える（本体の _report の始め）
    s.HUB.report_begin("proj｜t が完了しました", "proj｜t")
    between()
    asyncio.run(s.HUB.report_end("done", "proj の報告です。閉じていいですか？", "proj｜t", True, mark=mark))
    check(f"報告: 画面に送り終えるまでは聞いた文にしない（頼んだあとに{name}）", hm.HERMES_HEARD["text"], "")
    s.HUB.report_delivered(mark)  # 鳴らす画面に送り終えた（本体の _report が呼ぶ）
    check(f"報告を頼んだあとに{name}: 画面に送り終えても、届いた報告を聞いた文に{'しない' if not want else 'する'}", hm.HERMES_HEARD["text"], want)


# ---- 本物の報告の流れ（_report）: 頼んだその場で番号を控える。順番待ち（頭のやり取りの鍵）の間に止めても、報告は聞いた文にしない
REPORT_D = {"pane": "w9:p1", "cwd": "/x/proj", "project": "proj", "title": "テスト", "text": "", "offset": 0, "was_working": False, "gen": 1}


async def report_while_locked(between):
    hm.HERMES_HEARD["text"] = ""
    hm.ask_hermes = lambda text: "proj の報告です。閉じていいですか？"
    async with s.pane_lock(hm.HERMES_PANE):  # ほかのやり取りが鍵を持っている間に、報告を頼む
        task = asyncio.create_task(s._report(dict(REPORT_D), "done"))
        await asyncio.sleep(0.2)
        between()
    await task
    return hm.HERMES_HEARD["text"]


async def both_reports():  # 鍵は 1 つのイベントループに結び付くので、同じ実行の中で続けて回す
    return await report_while_locked(s.HUB.stopped), await report_while_locked(lambda: None)


stopped_heard, plain_heard = asyncio.run(both_reports())
check("本物の報告の流れ: 鍵の順番待ちの間に止めたら、届いた報告は聞いた文にしない", stopped_heard, "")
check("本物の報告の流れ: 開いている画面が無ければ、止めなくても、届いた報告は聞いた文にしない（あとで読んだなら ↩ 返信か、頭が聞き直す）", plain_heard, "")

# ---- 本物の WebSocket の流れ
async def nothing(*a, **k):
    return None


s.boot_ensure_hub = s.keep_gateway_token = s.watch_blocked = s.watch_delegation = s.hermes.watch_hermes_skills = nothing
s.list_agents = lambda: []
prep_in, prep_go = threading.Event(), threading.Event()


async def slow_attachment(ids, talker, pane=""):
    prep_in.set()
    await asyncio.to_thread(prep_go.wait, 5)
    return "", ""


s.attachment_lines = slow_attachment
hm.ask_hermes = lambda text: Q


def receive_until(w, kind):
    for _ in range(50):
        m = json.loads(w.receive_text())
        if m.get("type") == kind:
            return m
    return None


with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as w:
        # 画面が無いときの報告（上の本物の報告の流れ）のあと、最初につないだ画面の「はい」は、その報告への答えにならない（a2a 30fd5b94 の 3 回目）
        hm.ask_hermes = lambda text: "わかりました"
        w.send_text(json.dumps({"type": "text", "text": "画面を開いて最初のはい", "pane": hm.HERMES_PANE, "speaker": 0}))
        receive_until(w, "reply")
        first = next((r for r in list(hm.HERMES_TURNS) if r[1] == "画面を開いて最初のはい"), None)
        check("本物の流れ: 画面が無いときに届いた報告は、あとで画面を開いて最初に言った発言の「聞いた文」にならない", first[3] if first else None, "")
        hm.HERMES_HEARD["text"] = Q
        before = hm.HERMES_GEN["n"]
        w.send_text(json.dumps({"type": "stop"}))
        got = receive_until(w, "stopped")
        check("画面の「停止」で、止めたことが話し相手に伝わる（聞いた文を空にし、受け付けの番号を進める）",
              (got is not None, hm.HERMES_HEARD["text"], hm.HERMES_GEN["n"] > before), (True, "", True))
        # 止めずに画面に届いた返事・報告は、聞いた文になる（送り終えた知らせが本体から頭に届く）
        hm.ask_hermes = lambda text: Q
        w.send_text(json.dumps({"type": "text", "text": "ふつうに proj を閉じて", "pane": hm.HERMES_PANE, "speaker": 0}))
        got = receive_until(w, "reply")
        time.sleep(0.2)
        check("本物の流れ: 止めずに画面に届いた返事は、聞いた文で、台帳の返事の欄にも入る",
              (got and got.get("text"), hm.HERMES_HEARD["text"], s.hub_reply_to(s.hub_human_turn())), (Q, Q, Q))
        hm.HERMES_HEARD["text"] = ""
        hm.ask_hermes = lambda text: "proj の報告です。閉じていいですか？"
        c.portal.call(s._report, dict(REPORT_D), "done")
        check("本物の流れ: 開いている画面に届いた報告は、聞いた文になる", hm.HERMES_HEARD["text"], "proj の報告です。閉じていいですか？")
        hm.ask_hermes = lambda text: Q
        # 受け付けたあと、添付の用意の間に止める（a2a 30fd5b94 の 1 の再現）
        w.send_text(json.dumps({"type": "text", "text": "添付つきで proj を閉じて", "pane": hm.HERMES_PANE, "speaker": 0, "attachments": ["a1"]}))
        prep_in.wait(5)
        w.send_text(json.dumps({"type": "stop"}))
        receive_until(w, "stopped")
        prep_go.set()
        for _ in range(100):  # 往復が最後まで進み、返事が記録に載るまで
            turn = next((r[0] for r in list(hm.HERMES_TURNS) if r[1] == "添付つきで proj を閉じて"), None)
            if turn and ai_rows(turn):
                break
            time.sleep(0.05)
        check("本物の流れ: 受け付けたあと、添付の用意の間に止めたら、あとで届いた返事は聞いた文にしない（記録には残る）",
              (hm.HERMES_HEARD["text"], s.hub_reply_to(turn), ai_rows(turn)), ("", "", [Q]))
        # 声の発言の聞き取りの間に止めたら、聞き取った発言は送らない（聞き取りの間も停止を読む。codex-review bcd26ac）
        trans_in, trans_go = threading.Event(), threading.Event()

        def slow_transcribe(audio, filename):
            trans_in.set()
            trans_go.wait(5)
            return "聞き取りの間に止めた発言"

        real_transcribe = s.transcribe
        s.transcribe = slow_transcribe
        try:
            w.send_text(json.dumps({"type": "turn", "pane": hm.HERMES_PANE, "speaker": 0, "filename": "a.webm"}))
            w.send_bytes(b"0" * 4000)
            check("（前提）聞き取りの途中", trans_in.wait(5), True)
            w.send_text(json.dumps({"type": "stop"}))
            got = receive_until(w, "stopped")
            check("聞き取りの間も「停止」を読む（聞き取りが終わる前に止めたと返す）", (got is not None, trans_go.is_set()), (True, False))
            trans_go.set()
            note = receive_until(w, "status")
            while note and note.get("text") == "聞き取り中…":
                note = receive_until(w, "status")
        finally:
            s.transcribe = real_transcribe
        time.sleep(0.2)
        check("聞き取りの間に止めたら、聞き取った発言は送らない（ハブにも台帳にも載らない）",
              (note and note.get("text"), any(r[1] == "聞き取りの間に止めた発言" for r in list(hm.HERMES_TURNS))), ("止めたので、聞き取った発言は送っていません", False))
        # 報告の読み上げも「停止」で止める（codex-review bcd26ac: 報告の合成・送信は、画面の停止と別の合図で続いていた）
        speech, real_speak = {}, s.speak_to

        async def held_speak(ring, reply, ev, **k):
            speech["ev"] = ev
            try:
                await asyncio.wait_for(ev.wait(), 5)  # 停止で止まるまで読み続けている
            except asyncio.TimeoutError:
                pass
            speech["stopped"] = ev.is_set()

        s.speak_to = held_speak
        try:
            hm.ask_hermes = lambda text: "proj の報告です。"
            fut = c.portal.start_task_soon(s._report, dict(REPORT_D), "done")
            for _ in range(100):
                if "ev" in speech:
                    break
                time.sleep(0.05)
            w.send_text(json.dumps({"type": "stop"}))
            receive_until(w, "stopped")
            fut.result(timeout=10)
        finally:
            s.speak_to = real_speak
        check("報告の読み上げの途中で「停止」を押したら、報告の読み上げも止める", speech.get("stopped"), True)
        # 声の発言の頭が届いた時点で、前のハブの発言はいまの発言でなくなる（聞き取りを待たない）
        prev = hm.hermes_turn_begin("頭の前のハブの発言")
        w.send_text(json.dumps({"type": "turn", "pane": hm.HERMES_PANE, "speaker": 0, "filename": "a.webm"}))
        for _ in range(100):
            if not s.HUB.current(prev):
                break
            time.sleep(0.02)
        check("声の発言の頭が届いた時点で、前のハブの発言はいまの発言でなくなる（聞き取りを待たない）", s.HUB.current(prev), False)
        # 声の発言の聞き取りの間に届いた文字の発言は、声の発言のあとに並べる（順を守る）
        trans_in.clear()
        trans_go.clear()
        s.transcribe = lambda audio, filename: (trans_in.set(), trans_go.wait(5), "先の声の発言")[2]
        try:
            hm.ask_hermes = lambda text: "わかりました"
            w.send_text(json.dumps({"type": "turn", "pane": hm.HERMES_PANE, "speaker": 0, "filename": "a.webm"}))
            w.send_bytes(b"0" * 4000)
            trans_in.wait(5)
            w.send_text(json.dumps({"type": "text", "text": "あとの文字の発言", "pane": hm.HERMES_PANE, "speaker": 0}))
            time.sleep(0.3)
            check("聞き取りの間に届いた文字の発言は、まだ送らない（声の発言を待つ）", any(r[1] == "あとの文字の発言" for r in list(hm.HERMES_TURNS)), False)
            trans_go.set()
            for _ in range(100):
                order = [r[1] for r in list(hm.HERMES_TURNS) if r[1] in ("先の声の発言", "あとの文字の発言")]
                if len(order) == 2:
                    break
                time.sleep(0.05)
        finally:
            s.transcribe = real_transcribe
        check("声の発言のあとに届いた文字の発言は、声の発言のあとに送る（順を守る）", order, ["先の声の発言", "あとの文字の発言"])
        # 別のセッションに話しかけ直したら、前のハブの発言はもういまの発言ではない
        hub_turn = hm.hermes_turn_begin("ハブへの前の発言")
        w.send_text(json.dumps({"type": "text", "text": "別のセッションへ", "pane": "w9:p1", "speaker": 0}))
        receive_until(w, "error")  # 相手のセッションは無い（一覧が空）
        check("別のセッションに話しかけ直したら、前のハブの発言はいまの発言ではない（前の許可で答えない・閉じない）", s.HUB.current(hub_turn), False)
        # 聞き取った文を画面に送る間に止めたら、その発言は送らない（a2a 5a3533e2 の再現）
        real_send2 = s.Client.send
        t_hold, t_holding, t_block = threading.Event(), threading.Event(), {"on": True}

        async def held_transcript(self, **msg):
            if msg.get("type") == "transcript" and t_block["on"]:
                t_block["on"] = False
                t_holding.set()
                await asyncio.to_thread(t_hold.wait, 5)
            return await real_send2(self, **msg)

        s.Client.send = held_transcript  # 画面の流れは、つないだときの send を控えるので、差し替えてから新しくつなぐ
        s.transcribe = lambda audio, filename: "文を送る間に止めた発言"
        try:
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as w3:
                w3.send_text(json.dumps({"type": "turn", "pane": hm.HERMES_PANE, "speaker": 0, "filename": "a.webm"}))
                w3.send_bytes(b"0" * 4000)
                check("（前提）聞き取った文を画面に送る途中で止めている", t_holding.wait(5), True)
                w3.send_text(json.dumps({"type": "stop"}))
                receive_until(w3, "stopped")
                t_hold.set()
                time.sleep(0.5)
        finally:
            s.Client.send = real_send2
            s.transcribe = real_transcribe
        check("聞き取った文を画面に送る間に止めたら、その発言は送らない", any(r[1] == "文を送る間に止めた発言" for r in list(hm.HERMES_TURNS)), False)
        # 画面に送る途中で、次の発言を受け付けた（a2a 30fd5b94 の 2 回目の再現: 送れていない問いが、次の「はい」の行に「聞いた文」として写った）
        real_send = s.Client.send
        hold, holding, block = threading.Event(), threading.Event(), {"on": True}

        async def held_send(self, **msg):
            if msg.get("type") == "reply" and block["on"]:
                block["on"] = False
                holding.set()
                await asyncio.to_thread(hold.wait, 5)
            return await real_send(self, **msg)

        s.Client.send = held_send  # 画面の流れは、つないだときの send を控えるので、差し替えてから新しくつなぐ
        try:
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as w2:
                hm.ask_hermes = lambda text: Q
                w2.send_text(json.dumps({"type": "text", "text": "送る途中の問い", "pane": hm.HERMES_PANE, "speaker": 0}))
                check("（前提）問いの返事を、画面に送る途中で止めている", holding.wait(5), True)
                hm.ask_hermes = lambda text: "わかりました"
                w2.send_text(json.dumps({"type": "text", "text": "はい", "pane": hm.HERMES_PANE, "speaker": 0}))
                for _ in range(100):
                    yes = next((r for r in list(hm.HERMES_TURNS) if r[1] == "はい" and ai_rows(r[0])), None)
                    if yes:
                        break
                    time.sleep(0.05)
                hold.set()
        finally:
            s.Client.send = real_send
        asked_row = next((r for r in list(hm.HERMES_TURNS) if r[1] == "送る途中の問い"), None)
        check("本物の流れ: 画面に送る途中で次の「はい」を受け付けたら、送れていない問いは「はい」の聞いた文にも、台帳の返事の欄にも入らない",
              (yes[3] if yes else None, asked_row[2] if asked_row else None), ("", ""))

bad = [r for r in results if not r[1]]
for name, ok, got, want in results:
    print(("ok  " if ok else "NG  ") + name + ("" if ok else f"  got={got!r} want={want!r}"))
print(f"\n{len(results) - len(bad)}/{len(results)} 件が期待どおり")
sys.exit(1 if bad else 0)
