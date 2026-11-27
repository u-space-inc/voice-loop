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
  - 報告は、止めても止めなくても、画面に届いても「聞いた文」にしない（答えるのは「↩ 返信」。前に聞いた問いへの答えにも使わせない）
  - 画面の「停止」（WebSocket の stop）で、話し相手に止めたことが伝わる。報告の読み上げも止まる
  - 声の発言は、頭が届いたときに受け付ける（聞き取りの間に届いた前の往復の返事は、聞いた文にしない）
  - 聞き取りの間に止めたら、発言は相手に届くが、その発言はもういまの発言ではない（承認に答える・閉じるに使えない。例外 D-007）
  - 別のセッションに話しかけ直したら、前のハブの発言はいまの発言ではない
本物の Hermes・herdr・コンテナには触れない（頭の往復は差し替える。VL_HERMES_URL は何も待ち受けていないポート）。
使い方: uv run spike/stop_test.py"""
import asyncio
import functools
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


async def plain_narrate(text, cancel):  # 本物の語り手（LLM）を呼ばない（CI には鍵が無く、手元でも外に出さない）
    yield text


s.narrate = plain_narrate
from fastapi.testclient import TestClient  # noqa: E402

hm = s.hermes
results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


WAITED = ("送っていません", "回答を読めませんでした", "送信できませんでした")  # 試験が待つ知らせ（読み上げの失敗などのほかのエラーでは止まらない）


def read_notice(ws, pred, n0=40, tries=5):
    """名乗ったあとの控えた知らせ（別の仕事で送る）を待つ。止めた返事を区切りに読み、目的の知らせが無ければ、少し待って止め直して読み続ける
    （名乗りへの返事は、そのあとの停止への返事より遅れて届くことがある。codex-review 635eea0）。読んだ (種類, 文) の一覧を返す。"""
    got = []
    for k in range(tries):
        ws.send_text(json.dumps({"type": "stop", "n": n0 + k}))
        for _ in range(30):
            m = json.loads(ws.receive_text())
            got.append((m.get("type"), m.get("text", "")))
            if m.get("type") == "stopped":
                break
        if any(pred(t, x) for t, x in got):
            break
        time.sleep(0.2)
    return got


def begin_and_wait(ws, payload, limit=5.0):
    """合図（begin）を送り、本体が受けた（受け付けの番号が進んだ）のを待つ（別の画面の停止と順が入れ替わらないように。codex-review bb7be2c の P3）。"""
    g0 = hm.HERMES_GEN["n"]
    ws.send_text(json.dumps(payload))
    end = time.time() + limit
    while time.time() < end and hm.HERMES_GEN["n"] == g0:
        time.sleep(0.02)
    return hm.HERMES_GEN["n"] != g0


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

# ---- 受け付けたその場で、直前に聞いた文をその発言に引き渡す（あとから受け付けた発言は、前の発言より前の問いを使わない。
#      声の聞き取りの間に別の画面から言った「はい」など。codex-review 9c14f08）
hm.ask_hermes = lambda text: "わかりました"
hm.HERMES_HEARD["text"] = Q
s.HUB.admit()  # 声の発言の頭（聞き取りの途中）
adm = s.HUB.admit()  # その間に、別の画面から「はい」
asyncio.run(hm.hermes_exchange("はい（別の画面から）", admitted=adm))
heard_b = list(hm.HERMES_TURNS)[-1][3]
hm.HERMES_HEARD["text"] = Q
adm = s.HUB.admit()
asyncio.run(hm.hermes_exchange("はい（問いのすぐあと）", admitted=adm))
heard_c = list(hm.HERMES_TURNS)[-1][3]
check("受け付けたその場で聞いた文を引き渡す: 前の発言のあとに受け付けた「はい」は前の問いを使わず、問いのすぐあとの「はい」は使う", (heard_b, heard_c), ("", Q))

# ---- 報告ができたら、画面に送る途中の返事も聞いた文にしない（報告と前後して届くので、どちらを最後に聞いたか分からない。codex-review 9c14f08）
hm.ask_hermes = lambda text: Q
adm = s.HUB.admit()
asyncio.run(hm.hermes_exchange("proj を閉じて", admitted=adm))  # 返事は、画面に送る途中
asyncio.run(s.HUB.report_end("done", "別件の報告です。", "other｜t", True))
s.HUB.delivered(adm)  # 報告のあとに、遅れて送り終えた
check("報告ができたら、画面に送る途中の返事は、あとで送り終えても聞いた文にしない", (hm.HERMES_HEARD["text"], s.hub_reply_to(s.hub_human_turn())), ("", ""))

# ---- 承認に答える・閉じるの直前に見る「その発言のあとに止めていないか」
turn = hm.hermes_turn_begin("はい")
check("発言のすぐあとは、いまの発言", s.HUB.current(turn), True)
s.HUB.stopped()
check("止めたら、その前の発言はもういまの発言ではない（答えを送らない・閉じない）", s.HUB.current(turn), False)
turn = hm.hermes_turn_begin("はい")
s.HUB.admit()
check("次の発言を受け付けても、前の発言はいまの発言ではない", s.HUB.current(turn), False)
check("台帳に無い発言は、いまの発言ではない（断る側）", s.HUB.current("hzzzzzzzzzzzz"), False)

# ---- 本体がその場で答えた・断った発言も、受け付けたその場の番号で台帳に載せる（順番待ちの間に止めたら、もういまの発言ではない。
#      止めたあとに断る答え（Esc）も送らせない。a2a 3752b7a5）
async def held_accept(run):
    s.ACCEPT_LOCKS.pop(hm.HERMES_PANE, None)  # 鍵はイベントループに結び付くので、回すたびに作り直す
    lock = s.ACCEPT_LOCKS.setdefault(hm.HERMES_PANE, asyncio.Lock())
    async with lock:  # 前の発言の順番待ち
        adm = s.HUB.admit()
        task = asyncio.create_task(run(adm))
        await asyncio.sleep(0.05)
        s.HUB.stopped()
    await task
    return s.HUB.current(s.hub_human_turn())


talker = s.TALKERS[hm.HERMES_PANE]
real_snap, real_answer = s.quick_snapshot, s.quick.answer
s.quick_snapshot, s.quick.answer = (lambda: {}), (lambda kind, snap, cur, now: "いまは 3 件です")
try:
    quick_cur = asyncio.run(held_accept(lambda adm: s.hub_quick(talker, "status", "いまどう？", hm.HERMES_PANE, None, "", adm)))
finally:
    s.quick_snapshot, s.quick.answer = real_snap, real_answer
refused_cur = asyncio.run(held_accept(lambda adm: s.hub_accepted(talker, "添付つきのはい", hm.HERMES_PANE, None, None, admitted=adm)))
check("本体がその場で答えた・断った発言も、順番待ちの間に止めたら、もういまの発言ではない（止めたあとに答えを送らない）", (quick_cur, refused_cur), (False, False))
s.ACCEPT_LOCKS.clear()  # 鍵はこのイベントループに結び付いたので、あとの本物の流れのために捨てる

# ---- 起動し直し
hm.HERMES_TURNS.clear()
hm._load_hermes_turns()
check("起動し直したら、台帳の返事の欄は記録から戻さない（聞いた返事も。閉じるのは聞き直してから）",
      (s.hub_reply_to(t1), s.hub_reply_to(t2), s.hub_reply_to(t3)), ("", "", ""))
check("起動し直す前の発言は、いまの発言ではない（答えを送らない・閉じない）", s.HUB.current(t3), False)

# ---- 報告: 聞いた文にしない（前に聞いた問いも消す。報告のあとの「はい」を、前の問いへの答えにも報告の問いへの答えにも使わせない）
for name, between in [("止めた", s.HUB.stopped), ("話しかけた", s.HUB.admit), ("どちらも無い", lambda: None)]:
    hm.HERMES_HEARD["text"] = Q
    s.HUB.report_begin("proj｜t が完了しました", "proj｜t")
    between()
    asyncio.run(s.HUB.report_end("done", "proj の報告です。閉じていいですか？", "proj｜t", True))
    check(f"報告（頼んだあとに{name}）: 届いた報告は聞いた文にせず、前に聞いた問いも消す", hm.HERMES_HEARD["text"], "")


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
s.PANE_LOCKS.clear()  # 鍵はこの実行のイベントループに結び付いたので、あとの本物の流れ（別のループ）のために捨てる
s.ACCEPT_LOCKS.clear()
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
        got = receive_until(w, "reply")
        check("本物の流れ: 開いている画面に届いた報告も、聞いた文にしない（答えるのは ↩ 返信）",
              (bool(got and got.get("report")), hm.HERMES_HEARD["text"]), (True, ""))
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
        # 声の発言は、頭が届いたときに受け付ける。聞き取りの間に届いた前の往復の返事は、聞いた文にしない（codex-review bcd26ac）
        trans_in, trans_go = threading.Event(), threading.Event()

        def slow_transcribe(audio, filename):
            trans_in.set()
            trans_go.wait(5)
            return "聞き取りの間に止めた発言"

        real_transcribe = s.transcribe
        s.transcribe = slow_transcribe
        try:
            hm.ask_hermes = lambda text: Q
            adm = s.HUB.admit()
            c.portal.call(functools.partial(hm.hermes_exchange, "前の発言", admitted=adm))  # 返事はできたが、まだ画面に送り終えていない
            w.send_text(json.dumps({"type": "turn", "pane": hm.HERMES_PANE, "speaker": 0, "filename": "a.webm"}))
            w.send_bytes(b"0" * 4000)
            check("（前提）聞き取りの途中", trans_in.wait(5), True)
            s.HUB.delivered(adm)  # 聞き取りの間に、前の往復の返事を送り終えた
            check("声の発言の頭が届いたあとに送り終えた前の返事は、聞いた文にしない（頭で受け付けている）", hm.HERMES_HEARD["text"], "")
            # 聞き取りの間に止める。聞き取りは受信の中で待つので、止めたことは聞き取った発言を送ったあとに読む（例外 D-007）
            hm.ask_hermes = lambda text: "わかりました"
            w.send_text(json.dumps({"type": "stop"}))
            trans_go.set()
            order = []
            for _ in range(50):
                m = json.loads(w.receive_text())
                if m.get("type") in ("transcript", "stopped"):
                    order.append(m["type"])
                if m.get("type") == "stopped":
                    break
        finally:
            s.transcribe = real_transcribe
        for _ in range(100):  # ハブの仕事が台帳に載るまで
            said_row = next((r for r in list(hm.HERMES_TURNS) if r[1] == "聞き取りの間に止めた発言"), None)
            if said_row:
                break
            time.sleep(0.05)
        check("聞き取りの間に止めたら、発言は届く（例外 D-007）が、止めたのでその発言はもういまの発言ではない（承認に答える・閉じるに使えない）",
              (order, said_row is not None, s.HUB.current(said_row[0]) if said_row else None), (["transcript", "stopped"], True, False))
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
        # 別のセッションに話しかけ直したら、前のハブの発言はもういまの発言ではない
        hub_turn = hm.hermes_turn_begin("ハブへの前の発言")
        hm.hermes_turn_end(hub_turn, Q)  # ハブの問いを聞いた
        w.send_text(json.dumps({"type": "text", "text": "別のセッションへ", "pane": "w9:p1", "speaker": 0}))
        receive_until(w, "error")  # 相手のセッションは無い（一覧が空）
        check("別のセッションに話しかけ直したら、前のハブの発言はいまの発言ではない（前の許可で答えない・閉じない）", s.HUB.current(hub_turn), False)
        check("別のセッションに話したら、ハブの問いは次の「はい」の聞いた文にも、閉じるの守りが読む返事の欄にも残さない（a2a 3752b7a5）",
              (hm.HERMES_HEARD["text"], s.hub_reply_to(hub_turn)), ("", ""))
        # 声の発言元は、発言の頭が届いた時点で決める（頭と声の間に「画面に戻る」の名乗りが届いても、顔で録った発言は機械から。a2a 3752b7a5）
        real_transcribe = s.transcribe
        s.transcribe = lambda audio, filename: "顔で録ったはい"
        try:
            w.send_text(json.dumps({"type": "hello", "speaker": 0, "kind": "stackchan", "place": "home"}))
            w.send_text(json.dumps({"type": "turn", "pane": hm.HERMES_PANE, "speaker": 0, "filename": "a.webm"}))
            w.send_text(json.dumps({"type": "hello", "speaker": 0}))  # 頭のあと、声の前に「画面に戻る」
            w.send_bytes(b"0" * 4000)
            receive_until(w, "transcript")
            for _ in range(100):
                face_row = next((r for r in list(hm.HERMES_TURNS) if r[1] == "顔で録ったはい"), None)
                if face_row:
                    break
                time.sleep(0.05)
        finally:
            s.transcribe = real_transcribe
        check("声の発言元は頭の時点で決める（頭のあとに画面と名乗り直しても、顔で録った発言は機械から）", s.hub_via(face_row[0]) if face_row else None, "device")
        w.send_text(json.dumps({"type": "hello", "speaker": 0}))
        # ハブ以外への声の発言は、頭が届いたときに前の問いを消す（聞き取りが空・失敗でも。a2a 3752b7a5 の 2 回目）
        hub_turn = hm.hermes_turn_begin("ハブへの前の発言（声）")
        hm.hermes_turn_end(hub_turn, Q)
        s.transcribe = lambda audio, filename: ""
        try:
            w.send_text(json.dumps({"type": "turn", "pane": "w9:p1", "speaker": 0, "filename": "a.webm"}))
            w.send_bytes(b"0" * 4000)
            empty = None
            for _ in range(60):  # 聞き取れなかった知らせまで読む（前の発言の読み上げの失敗などのエラーでは止まらない）
                m = json.loads(w.receive_text())
                if m.get("type") == "error" and m.get("text") == "聞き取れませんでした":
                    empty = m
                    break
        finally:
            s.transcribe = real_transcribe
        check("ハブ以外への声の発言は、聞き取れなくても、頭が届いたときにハブの前の問いを聞いた文と返事の欄から外す",
              (empty and empty.get("text"), hm.HERMES_HEARD["text"], s.hub_reply_to(hub_turn)), ("聞き取れませんでした", "", ""))
        # 声の発言の頭のあと、声の本体の前に止めたら、あとから届いた声は聞き取らずに捨てる（「止めました」のあとに送らない。codex-review 7793a44）
        heard_calls = []
        s.transcribe = lambda audio, filename: (heard_calls.append(1), "止めたあとに届いた声")[1]
        try:
            w.send_text(json.dumps({"type": "turn", "pane": hm.HERMES_PANE, "speaker": 0, "filename": "a.webm"}))
            w.send_text(json.dumps({"type": "stop"}))
            first = receive_until(w, "stopped")
            w.send_bytes(b"0" * 4000)
            second = next((m for m in (json.loads(w.receive_text()) for _ in range(50)) if m.get("type") in ("stopped", "transcript")), None)  # 捨てたことを画面に返す（画面を「聞き取り中…」のままにしない）
        finally:
            s.transcribe = real_transcribe
        time.sleep(0.2)
        # 録音を始めたときに画面が受け取っていた返事の番号が、聞いた文の返事と合うときだけ、その問いを声の発言に引き渡す
        # （録り終えた「はい」を本体が受け付ける前に届いた問いへの答えにしない。a2a 3752b7a5 の 4 回目）
        def reply_of(text):
            for _ in range(50):
                m = json.loads(w.receive_text())
                if m.get("type") == "reply" and m.get("text") == text:
                    return m
            return None

        def voice_row(said, hid):
            s.transcribe = lambda audio, filename: said
            w.send_text(json.dumps({"type": "turn", "pane": hm.HERMES_PANE, "speaker": 0, "filename": "a.webm", "hid": hid}))
            w.send_bytes(b"0" * 4000)
            reply_of("わかりました")  # この発言への返事が届くまで（台帳に載り終える）
            return next((r for r in list(hm.HERMES_TURNS) if r[1] == said), None)

        try:
            hm.ask_hermes = lambda text: Q
            w.send_text(json.dumps({"type": "text", "text": "proj はどうする？", "pane": hm.HERMES_PANE, "speaker": 0}))
            hid1 = (reply_of(Q) or {}).get("hid")
            hm.ask_hermes = lambda text: "わかりました"
            after = voice_row("はい（問いを受け取ったあとに録音）", hid1)
            hm.ask_hermes = lambda text: Q
            w.send_text(json.dumps({"type": "text", "text": "もう一度、proj はどうする？", "pane": hm.HERMES_PANE, "speaker": 0}))
            hid2 = (reply_of(Q) or {}).get("hid")
            hm.ask_hermes = lambda text: "わかりました"
            before = voice_row("はい（問いが届く前に録音）", hid1)  # 録音を始めたのは、2 つ目の問いが届く前
        finally:
            s.transcribe = real_transcribe
        check("声の発言: 録音を始めたときに受け取っていた返事が問いなら引き渡し、録音を始めたあとに届いた問いは引き渡さない",
              (type(hid1) is int and type(hid2) is int and hid1 != hid2, after[3] if after else None, before[3] if before else None), (True, Q, ""))
        # 文字の発言も、送るを押した時点で受け取っていた返事の番号で照らす。番号の無い発言（更新前の画面・機械）には聞いた文を渡さない
        # （添付を送る間に届いた問いへの答えにしない。a2a 3752b7a5 の 5 回目）
        texts = {}
        for label, extra_of in [("合う", lambda h: {"hid": h}), ("古い", lambda h: {"hid": h - 1}), ("無い", lambda h: {})]:
            hm.ask_hermes = lambda text: Q
            w.send_text(json.dumps({"type": "text", "text": f"問いをください（{label}）", "pane": hm.HERMES_PANE, "speaker": 0}))
            h = (reply_of(Q) or {}).get("hid", 0)
            hm.ask_hermes = lambda text: "わかりました"
            said = f"はい（文字・番号が{label}）"
            w.send_text(json.dumps({"type": "text", "text": said, "pane": hm.HERMES_PANE, "speaker": 0, **extra_of(h)}))
            reply_of("わかりました")
            row = next((r for r in list(hm.HERMES_TURNS) if r[1] == said), None)
            texts[label] = row[3] if row else None
        check("文字の発言: 送るを押した時点の返事の番号が合えば問いを引き渡し、古い・無い（更新前の画面）なら引き渡さない",
              (texts["合う"], texts["古い"], texts["無い"]), (Q, "", ""))
        # ハブ以外への指示を送り始める前に止めたら、送っていないと画面に知らせる（「送りました」のまま黙って消さない。codex-review e460a4f）
        gate2 = threading.Event()

        async def held_attach(ids, talker, pane=""):  # 添付の用意で止めておく（送り始める前）
            await asyncio.to_thread(gate2.wait, 5)
            return "", ""

        real_attach = s.attachment_lines
        s.attachment_lines = held_attach
        s.transcribe = lambda audio, filename: "セッションへの声の指示"
        seen = []
        try:
            w.send_text(json.dumps({"type": "begin", "b": 950, "pane": "w9:p1", "hid": -1}))  # いまの画面と同じく、録音を始めた合図を先に送る
            w.send_text(json.dumps({"type": "turn", "pane": "w9:p1", "speaker": 0, "filename": "a.webm", "begin": 950}))
            w.send_bytes(b"0" * 4000)
            w.send_text(json.dumps({"type": "stop"}))
            w.send_text(json.dumps({"type": "stop"}))  # 2 つ目の止めたまで読む（知らせが来なくても待ち続けない）
            n_stopped = 0
            for _ in range(50):
                m = json.loads(w.receive_text())
                seen.append((m.get("type"), m.get("text", "")))
                n_stopped += m.get("type") == "stopped"
                if n_stopped == 2:
                    break
        finally:
            gate2.set()
            s.attachment_lines = real_attach
            s.transcribe = real_transcribe
        check("ハブ以外への声の指示を送り始める前に止めたら、送っていないと画面に知らせる",
              (("transcript", "セッションへの声の指示") in seen, any(t in ("error", "notice") and "送っていません" in x for t, x in seen)), (True, True))
        # 送るスレッドの空きを待つ間に止めたら、セッションに送らず、送っていないと知らせる（送ったかの印は、スレッドの中で送る直前に立てる。codex-review 6096e1d）
        prompts, gate3, entered3 = [], threading.Event(), threading.Event()
        real_if, real_herdr, real_agents, real_jsonl = s._if_still, s.herdr, s.list_agents, s.jsonl_path

        def waiting_if_still(still, fn, *a):
            entered3.set()
            gate3.wait(5)  # スレッドの空きを待っている間
            return real_if(still, fn, *a)

        def fake_herdr(*a, **k):
            if a[:2] == ("agent", "prompt"):
                prompts.append(a[3])
            return {"result": {"agent": {"agent_status": "idle"}}}

        marks, real_mark = [], s.topics.mark_direct
        s._if_still, s.herdr, s.jsonl_path = waiting_if_still, fake_herdr, (lambda a: None)
        s.topics.mark_direct = lambda session: marks.append(session)  # 直接送った印（前の件のリンクを外す）
        s.list_agents = lambda: [{"pane": "w9:p1", "cwd": "/x/proj", "agent": "claude", "session": "s1"}]
        seen3 = []
        try:
            w.send_text(json.dumps({"type": "text", "text": "セッションへの文字の指示", "pane": "w9:p1", "speaker": 0}))
            check("（前提）送るスレッドの手前で待っている", entered3.wait(5), True)
            w.send_text(json.dumps({"type": "stop"}))
            w.send_text(json.dumps({"type": "stop"}))
            n_stopped = 0
            for _ in range(50):
                m = json.loads(w.receive_text())
                seen3.append((m.get("type"), m.get("text", "")))
                n_stopped += m.get("type") == "stopped"
                if n_stopped == 2:
                    break
            gate3.set()
            time.sleep(0.3)
            marks_cancelled = list(marks)
            s._if_still = real_if  # 取り消さずに送るとき
            w.send_text(json.dumps({"type": "text", "text": "セッションへのふつうの指示", "pane": "w9:p1", "speaker": 0}))
            for _ in range(100):
                if prompts:
                    break
                time.sleep(0.05)
            time.sleep(0.2)
        finally:
            gate3.set()
            s._if_still, s.herdr, s.list_agents, s.jsonl_path, s.topics.mark_direct = real_if, real_herdr, real_agents, real_jsonl, real_mark
        check("ハブ以外への指示を、送るスレッドの空きを待つ間に止めたら、送らずに送っていないと知らせ、件のリンクの印も付けない",
              (prompts[:-1], any(t in ("error", "notice") and "送っていません" in x for t, x in seen3), marks_cancelled), ([], True, []))
        check("取り消さずに送った直接の指示には、送ると決めたときに件のリンクの印を付ける", (prompts, marks), (["セッションへのふつうの指示"], ["s1"]))
        # 別の画面の「停止」でも、送る前のハブ以外への指示を取り消し、指示を出した画面に知らせる（a2a 38ef4720 の 3 回目）
        prompts6, gate6, entered6 = [], threading.Event(), threading.Event()

        def waiting_if_still6(still, fn, *a):
            entered6.set()
            gate6.wait(5)
            return real_if(still, fn, *a)

        def fake_herdr6(*a, **k):
            if a[:2] == ("agent", "prompt"):
                prompts6.append(a[3])
            return {"result": {"agent": {"agent_status": "idle"}}}

        s._if_still, s.herdr, s.jsonl_path = waiting_if_still6, fake_herdr6, (lambda a: None)
        s.list_agents = lambda: [{"pane": "w9:p1", "cwd": "/x/proj", "agent": "claude", "session": "s1"}]
        seen6 = []
        try:
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wb:
                w.send_text(json.dumps({"type": "text", "text": "別の画面で止める指示", "pane": "w9:p1", "speaker": 0}))
                check("（前提）送るスレッドの手前で待っている（別の画面の停止の前）", entered6.wait(5), True)
                wb.send_text(json.dumps({"type": "stop"}))
                receive_until(wb, "stopped")
            for _ in range(60):  # 知らせが来なければ、送ったあとの「回答を読めませんでした」で終わる（待ち続けない）
                m = json.loads(w.receive_text())
                seen6.append((m.get("type"), m.get("text", "")))
                if m.get("type") in ("error", "notice") and any(k in m.get("text", "") for k in WAITED):
                    break
            gate6.set()
            time.sleep(0.3)
        finally:
            gate6.set()
            s._if_still, s.herdr, s.list_agents, s.jsonl_path = real_if, real_herdr, real_agents, real_jsonl
        check("別の画面で止めても、送る前のハブ以外への指示は送らず、指示を出した画面に送っていないと知らせる",
              (prompts6, any(t in ("error", "notice") and "送っていません" in x for t, x in seen6)), ([], True))
        # 発言を始めた合図（begin）のあと、発言が届くまでに別の画面で止めたら、その発言では承認に答えない・閉じない（↩ 返信でも）。
        # ハブ以外への指示なら送らずに知らせる。止めていなければ、ふつうに受け付ける（codex-review c72fbbe）
        def row_of(text):
            for _ in range(100):
                r = next((x for x in list(hm.HERMES_TURNS) if x[1] == text), None)
                if r:
                    return r
                time.sleep(0.05)
            return None

        hm.ask_hermes = lambda text: "わかりました"
        w.send_text(json.dumps({"type": "begin", "b": 901}))
        w.send_text(json.dumps({"type": "text", "text": "はい（またがない）", "pane": hm.HERMES_PANE, "speaker": 0, "hid": -1, "begin": 901}))
        plain_row = row_of("はい（またがない）")
        plain_cur = s.HUB.current(plain_row[0]) if plain_row else None
        check("（前提）合図を本体が受けた（902）", begin_and_wait(w, {"type": "begin", "b": 902}), True)
        with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wb:
            wb.send_text(json.dumps({"type": "stop", "n": 1}))
            receive_until(wb, "stopped")
        w.send_text(json.dumps({"type": "text", "text": "はい（またいだ）", "pane": hm.HERMES_PANE, "speaker": 0, "hid": -1, "begin": 902}))
        crossed_row = row_of("はい（またいだ）")
        check("発言を始めたあと届くまでに別の画面で止めたら、その発言はハブに届くが、もういまの発言ではない（止めていなければいまの発言）",
              (plain_cur, crossed_row is not None, s.HUB.current(crossed_row[0]) if crossed_row else None), (True, True, False))
        # 停止をまたいだ発言は、その発言だけを使えなくする（ハブ全体を止め直して、ほかの画面の停止のあとの発言まで失効させない。codex-review bb7be2c）
        check("（前提）合図を本体が受けた（905）", begin_and_wait(w, {"type": "begin", "b": 905}), True)
        with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wb:
            wb.send_text(json.dumps({"type": "stop", "n": 1}))
            receive_until(wb, "stopped")
        with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wc:
            check("（前提）停止のあとの別の画面の合図を本体が受けた", begin_and_wait(wc, {"type": "begin", "b": 1}), True)
            wc.send_text(json.dumps({"type": "text", "text": "はい（停止のあとの別の画面）", "pane": hm.HERMES_PANE, "speaker": 0, "hid": -1, "begin": 1}))
            later_row = row_of("はい（停止のあとの別の画面）")
            w.send_text(json.dumps({"type": "text", "text": "はい（またいだ古い発言）", "pane": hm.HERMES_PANE, "speaker": 0, "hid": -1, "begin": 905}))
            old_row = row_of("はい（またいだ古い発言）")
            check("停止をまたいだ古い発言は使えず、停止のあとの別の画面の発言は失効させない",
                  (s.HUB.current(old_row[0]) if old_row else None, s.HUB.current(later_row[0]) if later_row else None), (False, True))
        prompts10 = []
        s.herdr = lambda *a, **k: (prompts10.append(a[3]) if a[:2] == ("agent", "prompt") else None, {"result": {"agent": {"agent_status": "idle"}}})[1]
        s.list_agents = lambda: [{"pane": "w9:p1", "cwd": "/x/proj", "agent": "claude", "session": "s1"}]
        s.jsonl_path = lambda a: None
        seen10 = []
        try:
            check("（前提）合図を本体が受けた（903）", begin_and_wait(w, {"type": "begin", "b": 903}), True)
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wb:
                wb.send_text(json.dumps({"type": "stop", "n": 1}))
                receive_until(wb, "stopped")
            w.send_text(json.dumps({"type": "text", "text": "またいだ直接の指示", "pane": "w9:p1", "speaker": 0, "hid": -1, "begin": 903}))
            for _ in range(60):  # 知らせが来なければ、送ったあとの「回答を読めませんでした」で終わる
                m = json.loads(w.receive_text())
                seen10.append((m.get("type"), m.get("text", "")))
                if m.get("type") in ("error", "notice") and any(k in m.get("text", "") for k in WAITED):
                    break
            time.sleep(0.3)
        finally:
            s.herdr, s.list_agents, s.jsonl_path = real_herdr, real_agents, real_jsonl
        check("発言を始めたあと届くまでに別の画面で止めたら、ハブ以外への指示は送らず、送っていないと知らせる",
              (prompts10, any(t in ("error", "notice") and "送っていません" in x for t, x in seen10)), ([], True))
        # 合図（begin）の無い更新前の画面の「↩ 返信」は、停止をまたいだか分からないので、承認に使わせない（合図があれば、ふつうに受け付ける。a2a 4135f4c2）
        legacy = {}
        for label, extra in [("合図あり", {"begin": 904}), ("合図なし", {})]:
            if extra:
                w.send_text(json.dumps({"type": "begin", "b": 904}))
            before_turn = s.hub_human_turn()
            w.send_text(json.dumps({"type": "text", "text": f"はい（返信・{label}）", "pane": hm.HERMES_PANE, "speaker": 0, "hid": -1, "reply_at": 12345.5, **extra}))
            for _ in range(100):  # 返信先の報告は無いので本体が断り、断った発言として台帳に載る
                if s.hub_human_turn() != before_turn:
                    break
                time.sleep(0.05)
            legacy[label] = s.HUB.current(s.hub_human_turn())
        check("合図の無い更新前の画面の ↩ 返信は、もういまの発言ではない（承認に使わせない）。合図があればいまの発言", (legacy["合図あり"], legacy["合図なし"]), (True, False))
        # 発言は合図（begin）を受けたときに受け付ける。別の画面で発言を始めた（添付を送っている）間に、この画面で言った「はい」は、前の問いを使わない
        # （別の画面の宛先がハブでも、ハブ以外でも）。ひとりなら、ふつうに使う（a2a 4135f4c2 の 2 回目）
        hm.ask_hermes = lambda text: "わかりました"

        def heard_after(other_pane, label):
            t0 = hm.hermes_turn_begin(f"問いを聞いた発言（{label}）")
            hm.hermes_turn_end(t0, Q)
            hm.HEARD_ID["n"] = 777  # 画面が受け取った問いの番号
            if other_pane is not None:
                with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wb:
                    begin_and_wait(wb, {"type": "begin", "b": 1, "pane": other_pane, "hid": -1})  # 別の画面で送るを押した（添付を送っている）
            b = 920 + len(label)
            w.send_text(json.dumps({"type": "begin", "b": b, "pane": hm.HERMES_PANE, "hid": 777}))
            w.send_text(json.dumps({"type": "text", "text": f"はい（{label}）", "pane": hm.HERMES_PANE, "speaker": 0, "hid": 777, "begin": b}))
            r = row_of(f"はい（{label}）")
            return r[3] if r else None

        alone = heard_after(None, "ひとり")
        after_other = heard_after("w9:p1", "別の画面がハブ以外に")
        after_hub = heard_after(hm.HERMES_PANE, "別の画面がハブに")
        check("別の画面で発言を始めたあとの「はい」は、前の問いを使わない（ひとりなら使う）", (alone, after_other, after_hub), (Q, "", ""))
        # 声の聞き取りで受信が止まっていた間に届いていた合図は、止まり始めた時点のものとして扱う（その間の別の画面の停止で、あとの直接の指示を送らない）
        def wait_idle(limit=15.0):  # 前の試験の直接の指示が送り終わり、セッションの鍵が空くまで（あとの試験の知らせ・送りと混ざらないように）
            end = time.time() + limit
            while time.time() < end:
                lock = s.PANE_LOCKS.get("w9:p1")
                if not s.DIRECT_PENDING and not (lock and lock.locked()):
                    return True
                time.sleep(0.05)
            return False

        check("（前提）前の試験の直接の指示が片づいている", wait_idle(), True)
        prompts12, tr_in12, tr_go12 = [], threading.Event(), threading.Event()

        def slow_transcribe12(audio, filename):
            tr_in12.set()
            tr_go12.wait(5)
            return "聞き取りの間の前の声"

        real_fix = s.fix_heard

        def slow_fix12(said):  # 聞き取りのあとの辞書の補正に時間がかかる（受信に戻るのが遅れる）
            time.sleep(0.8)
            return real_fix(said)

        s.transcribe, s.jsonl_path, s.fix_heard = slow_transcribe12, (lambda a: None), slow_fix12
        s.herdr = lambda *a, **k: (prompts12.append(a[3]) if a[:2] == ("agent", "prompt") else None, {"result": {"agent": {"agent_status": "idle"}}})[1]
        s.list_agents = lambda: [{"pane": "w9:p1", "cwd": "/x/proj", "agent": "claude", "session": "s1"}]
        seen12 = []
        try:
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa:  # 新しい接続で（前の試験の読み残しと混ざらないように）
                wa.send_text(json.dumps({"type": "turn", "pane": hm.HERMES_PANE, "speaker": 0, "filename": "a.webm", "hid": -1}))
                wa.send_bytes(b"0" * 4000)
                check("（前提）前の声を聞き取っている", tr_in12.wait(5), True)
                wa.send_text(json.dumps({"type": "begin", "b": 940, "pane": "w9:p1", "hid": -1}))  # 聞き取りの間に送るを押した（本体はまだ読めない）
                wa.send_text(json.dumps({"type": "text", "text": "聞き取りの間に押した指示", "pane": "w9:p1", "speaker": 0, "hid": -1, "begin": 940}))
                with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wb:
                    wb.send_text(json.dumps({"type": "stop", "n": 1}))
                    receive_until(wb, "stopped")
                tr_go12.set()
                for _ in range(60):  # 取り消したなら知らせ、送ったなら返事を待ったあとの「回答を読めませんでした」が届く
                    m = json.loads(wa.receive_text())
                    seen12.append((m.get("type"), m.get("text", "")))
                    if m.get("type") in ("error", "notice") and any(k in m.get("text", "") for k in WAITED):
                        break
                wait_idle()
        finally:
            tr_go12.set()
            s.transcribe, s.herdr, s.list_agents, s.jsonl_path, s.fix_heard = real_transcribe, real_herdr, real_agents, real_jsonl, real_fix
        check("聞き取りの間に送るを押した直接の指示は、その間の別の画面の停止で送らずに知らせる（補正に時間がかかっても）", (prompts12, any("送っていません" in x for t, x in seen12)), ([], True))
        # 声が続いて届いていても、聞き取りの間に届いていた合図は、最初に止まり始めた時点のものとして扱う（あとの声の処理で上書きしない）。
        # 合図の無い更新前の画面の文字の指示も、聞き取りで受信が止まっていた間に届いていたなら、その間の停止で送らない（a2a 4135f4c2 の 4 回目）
        for label in ("声が続いたあとの文字の指示", "更新前の画面の文字の指示"):
            check(f"（前提）前の試験の直接の指示が片づいている（{label}）", wait_idle(), True)
            calls15, gate15, in15 = {"n": 0}, threading.Event(), threading.Event()

            def first_slow(audio, filename, calls15=calls15, gate15=gate15, in15=in15):
                calls15["n"] += 1
                if calls15["n"] == 1:
                    in15.set()
                    gate15.wait(5)
                return f"続いた声 {calls15['n']}"

            prompts15 = []
            s.transcribe, s.jsonl_path = first_slow, (lambda a: None)
            s.herdr = lambda *a, **k: (prompts15.append(a[3]) if a[:2] == ("agent", "prompt") else None, {"result": {"agent": {"agent_status": "idle"}}})[1]
            s.list_agents = lambda: [{"pane": "w9:p1", "cwd": "/x/proj", "agent": "claude", "session": "s1"}]
            seen15 = []
            try:
                with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa:
                    wa.send_text(json.dumps({"type": "begin", "b": 960, "pane": hm.HERMES_PANE, "hid": -1}))
                    wa.send_text(json.dumps({"type": "turn", "pane": hm.HERMES_PANE, "speaker": 0, "filename": "a.webm", "hid": -1, "begin": 960}))
                    wa.send_bytes(b"0" * 4000)
                    check(f"（前提）1 つ目の声を聞き取っている（{label}）", in15.wait(5), True)
                    if label.startswith("声が続いた"):
                        wa.send_text(json.dumps({"type": "begin", "b": 961, "pane": hm.HERMES_PANE, "hid": -1}))
                        wa.send_text(json.dumps({"type": "turn", "pane": hm.HERMES_PANE, "speaker": 0, "filename": "a.webm", "hid": -1, "begin": 961}))
                        wa.send_bytes(b"0" * 4000)
                        wa.send_text(json.dumps({"type": "begin", "b": 962, "pane": "w9:p1", "hid": -1}))
                        wa.send_text(json.dumps({"type": "text", "text": label, "pane": "w9:p1", "speaker": 0, "hid": -1, "begin": 962}))
                    else:
                        wa.send_text(json.dumps({"type": "text", "text": label, "pane": "w9:p1", "speaker": 0}))  # 合図も番号も無い（更新前の画面）
                    with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wb:
                        wb.send_text(json.dumps({"type": "stop", "n": 1}))
                        receive_until(wb, "stopped")
                    gate15.set()
                    for _ in range(80):
                        m = json.loads(wa.receive_text())
                        seen15.append((m.get("type"), m.get("text", "")))
                        if m.get("type") in ("error", "notice") and any(k in m.get("text", "") for k in WAITED):
                            break
                    wait_idle()
            finally:
                gate15.set()
                s.transcribe, s.herdr, s.list_agents, s.jsonl_path = real_transcribe, real_herdr, real_agents, real_jsonl
            check(f"聞き取りの間の別の画面の停止で、{label}は送らずに知らせる", (prompts15, any("送っていません" in x for t, x in seen15)), ([], True))
        # 「送りました」と出たハブ以外への指示は、セッションの鍵を待つ間に画面が切れても、取り消さずに送る（黙って消さない。a2a 6604a50a の 2 回目）
        check("（前提）前の試験の直接の指示が片づいている（切れても送る）", wait_idle(), True)
        prompts16, lock_go16, lock_in16 = [], threading.Event(), threading.Event()

        async def hold_lock16():
            async with s.pane_lock("w9:p1"):  # 前の送りがセッションの鍵を持っている
                lock_in16.set()
                await asyncio.to_thread(lock_go16.wait, 5)

        s.herdr = lambda *a, **k: (prompts16.append(a[3]) if a[:2] == ("agent", "prompt") else None, {"result": {"agent": {"agent_status": "idle"}}})[1]
        s.list_agents = lambda: [{"pane": "w9:p1", "cwd": "/x/proj", "agent": "claude", "session": "s1"}]
        s.jsonl_path = lambda a: None
        try:
            held16 = c.portal.start_task_soon(hold_lock16)
            check("（前提）セッションの鍵がふさがっている", lock_in16.wait(5), True)
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa:
                wa.send_text(json.dumps({"type": "begin", "b": 970, "pane": "w9:p1", "hid": -1}))
                wa.send_text(json.dumps({"type": "text", "text": "鍵を待つ間に切れる指示", "pane": "w9:p1", "speaker": 0, "hid": -1, "begin": 970}))
                sent_shown = None
                for _ in range(30):
                    m = json.loads(wa.receive_text())
                    if m.get("type") == "status" and "送りました" in m.get("text", ""):
                        sent_shown = m
                        break
            time.sleep(0.3)  # 画面 A が切れた
            lock_go16.set()
            held16.result(timeout=10)
            for _ in range(100):
                if prompts16:
                    break
                time.sleep(0.05)
            wait_idle()
        finally:
            lock_go16.set()
            s.herdr, s.list_agents, s.jsonl_path = real_herdr, real_agents, real_jsonl
        check("「送りました」と出たハブ以外への指示は、鍵を待つ間に画面が切れても送る", (sent_shown is not None, prompts16), (True, ["鍵を待つ間に切れる指示"]))
        # 指示の行方の知らせ（送れなかった・送らなかった）は、窓の印で届ける。切れてつなぎ直した画面にも届き、どの画面もつながっていなければ
        # 控えて、次にその窓が名乗ったときに届ける（a2a 6604a50a の 3 回目。D-009 の場合も同じ）
        for label, reconnect_before in (("つなぎ直してから断られた", True), ("切れている間に断られた", False)):
            check(f"（前提）前の試験の直接の指示が片づいている（{label}）", wait_idle(), True)
            lock_go17, lock_in17 = threading.Event(), threading.Event()

            async def hold_lock17(lock_go17=lock_go17, lock_in17=lock_in17):
                async with s.pane_lock("w9:p1"):
                    lock_in17.set()
                    await asyncio.to_thread(lock_go17.wait, 5)

            win17 = ("winreconnect01" if reconnect_before else "winqueued0001")
            s.herdr = lambda *a, **k: {"error": {"code": "agent_busy", "message": "受け付けません"}} if a[:2] == ("agent", "prompt") else {"result": {}}
            s.list_agents = lambda: [{"pane": "w9:p1", "cwd": "/x/proj", "agent": "claude", "session": "s1"}]
            s.jsonl_path = lambda a: None
            got17 = []
            try:
                held17 = c.portal.start_task_soon(hold_lock17)
                check(f"（前提）セッションの鍵がふさがっている（{label}）", lock_in17.wait(5), True)
                with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa:
                    wa.send_text(json.dumps({"type": "hello", "speaker": 0, "win": win17}))
                    wa.send_text(json.dumps({"type": "begin", "b": 980, "pane": "w9:p1", "hid": -1}))
                    wa.send_text(json.dumps({"type": "text", "text": f"断られる指示（{label}）", "pane": "w9:p1", "speaker": 0, "hid": -1, "begin": 980}))
                    for _ in range(30):
                        m = json.loads(wa.receive_text())
                        if m.get("type") == "status" and "送りました" in m.get("text", ""):
                            break
                time.sleep(0.3)  # 画面 A が切れた
                if reconnect_before:
                    with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa2:
                        wa2.send_text(json.dumps({"type": "hello", "speaker": 0, "win": win17}))  # 同じ窓でつなぎ直した
                        time.sleep(0.3)
                        lock_go17.set()
                        held17.result(timeout=10)
                        wait_idle()  # 断られるまで（ここで止めると、断られる前に取り消してしまう）
                        wa2.send_text(json.dumps({"type": "stop", "n": 5}))  # 読み終わりの目印
                        for _ in range(30):
                            m = json.loads(wa2.receive_text())
                            got17.append((m.get("type"), m.get("text", "")))
                            if m.get("type") == "stopped":
                                break
                else:
                    lock_go17.set()
                    held17.result(timeout=10)
                    wait_idle()  # 切れている間に断られた
                    with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa2:
                        wa2.send_text(json.dumps({"type": "hello", "speaker": 0, "win": win17}))  # あとでつないだ
                        got17 += read_notice(wa2, lambda t, x: t == "error" and "送信できませんでした" in x, 80)
            finally:
                lock_go17.set()
                s.herdr, s.list_agents, s.jsonl_path = real_herdr, real_agents, real_jsonl
            check(f"指示の行方の知らせは窓の印で届ける（{label}）", any(t == "error" and "送信できませんでした" in x for t, x in got17), True)
        # 切れてつなぎ直した画面にも、別の画面の停止で取り消した指示の知らせが届く（もとの D-009 の場合。窓の印で届ける）
        check("（前提）前の試験の直接の指示が片づいている（つなぎ直した画面への取り消しの知らせ）", wait_idle(), True)
        prompts18, gate18, entered18 = [], threading.Event(), threading.Event()

        def waiting_if_still18(still, fn, *a):
            entered18.set()
            gate18.wait(5)
            return real_if(still, fn, *a)

        s._if_still, s.jsonl_path = waiting_if_still18, (lambda a: None)
        s.herdr = lambda *a, **k: (prompts18.append(a[3]) if a[:2] == ("agent", "prompt") else None, {"result": {"agent": {"agent_status": "idle"}}})[1]
        s.list_agents = lambda: [{"pane": "w9:p1", "cwd": "/x/proj", "agent": "claude", "session": "s1"}]
        got18 = []
        try:
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa:
                wa.send_text(json.dumps({"type": "hello", "speaker": 0, "win": "winreconnect02"}))
                wa.send_text(json.dumps({"type": "begin", "b": 990, "pane": "w9:p1", "hid": -1}))
                wa.send_text(json.dumps({"type": "text", "text": "つなぎ直した画面で取り消す指示", "pane": "w9:p1", "speaker": 0, "hid": -1, "begin": 990}))
                check("（前提）送るスレッドの手前で待っている（つなぎ直す前）", entered18.wait(5), True)
            time.sleep(0.3)
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa2:
                wa2.send_text(json.dumps({"type": "hello", "speaker": 0, "win": "winreconnect02"}))
                time.sleep(0.2)
                with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wb:
                    wb.send_text(json.dumps({"type": "stop", "n": 7}))
                    receive_until(wb, "stopped")
                gate18.set()
                wa2.send_text(json.dumps({"type": "stop", "n": 8}))  # 読み終わりの目印
                for _ in range(30):
                    m = json.loads(wa2.receive_text())
                    got18.append((m.get("type"), m.get("text", "")))
                    if m.get("type") == "stopped":
                        break
        finally:
            gate18.set()
            s._if_still, s.herdr, s.list_agents, s.jsonl_path = real_if, real_herdr, real_agents, real_jsonl
        check("切れてつなぎ直した画面にも、別の画面の停止で取り消した指示の知らせが届く", (prompts18, any("送っていません" in x for t, x in got18)), ([], True))
        # 控えた知らせを、名乗った直後にまた切れて送れなかったら、控えに戻す（次に名乗ったときに届ける。a2a 6604a50a の 4 回目）
        s.UNSENT_NOTICES["winrequeue01"] = [{"type": "error", "text": "送信できませんでした: 控えに戻す試験"}]
        real_send19, failed19 = s.Client.send, {"n": 0}

        async def failing_send19(self, **msg):
            if msg.get("text", "").endswith("控えに戻す試験") and not failed19["n"]:
                failed19["n"] += 1
                raise RuntimeError("名乗った直後に切れた")
            return await real_send19(self, **msg)

        s.Client.send = failing_send19  # 画面の流れは、つないだときの send を控えるので、差し替えてから新しくつなぐ
        try:
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa:
                wa.send_text(json.dumps({"type": "hello", "speaker": 0, "win": "winrequeue01"}))
                time.sleep(0.3)
        finally:
            s.Client.send = real_send19
        got19 = []
        with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa2:
            wa2.send_text(json.dumps({"type": "hello", "speaker": 0, "win": "winrequeue01"}))
            got19 = read_notice(wa2, lambda t, x: x.endswith("控えに戻す試験"), 60)
        check("控えた知らせを名乗った直後の切断で送れなかったら控えに戻し、次に名乗ったときに届ける",
              (failed19["n"], any(x.endswith("控えに戻す試験") for t, x in got19)), (1, True))
        # 本体が送る前に断った（相手のセッションが見つからない）知らせも、窓の印で届ける（断る前に画面が切れても、つなぎ直した画面に。a2a 6604a50a の 5 回目）
        check("（前提）前の試験の直接の指示が片づいている（断りの知らせ）", wait_idle(), True)
        att_go20, att_in20 = threading.Event(), threading.Event()

        async def held_attach20(ids, talker, pane=""):  # 添付の用意で止めておく（断る前に画面が切れる）
            att_in20.set()
            await asyncio.to_thread(att_go20.wait, 5)
            return "", ""

        real_attach20 = s.attachment_lines
        s.attachment_lines = held_attach20
        s.list_agents = lambda: []  # 相手のセッションが見つからない
        got20 = []
        try:
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa:
                wa.send_text(json.dumps({"type": "hello", "speaker": 0, "win": "winrefused001"}))
                wa.send_text(json.dumps({"type": "begin", "b": 995, "pane": "w9:gone", "hid": -1}))
                wa.send_text(json.dumps({"type": "text", "text": "いなくなったセッションへの指示", "pane": "w9:gone", "speaker": 0, "hid": -1, "begin": 995}))
                check("（前提）添付の用意で止めている（断る前）", att_in20.wait(5), True)
            time.sleep(0.3)  # 画面が切れた
            att_go20.set()
            time.sleep(0.5)  # 本体が断る（画面はつながっていないので控える）
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa2:
                wa2.send_text(json.dumps({"type": "hello", "speaker": 0, "win": "winrefused001"}))
                got20 = read_notice(wa2, lambda t, x: "見つかりません" in x, 70)
        finally:
            att_go20.set()
            s.attachment_lines, s.list_agents = real_attach20, real_agents
        check("本体が送る前に断った知らせも、切れてつなぎ直した画面に届ける", any("見つかりません" in x for t, x in got20), True)
        # 停止の回数は、名乗りの返事と、どこかの画面の停止のたびに全部の画面へ届く。画面が最新の停止を知らずに始めた発言は（通信が遅れて
        # 停止のあとに届いても）停止をまたいだものとして扱い、知っていれば、ふつうに受け付ける（codex-review 6b2ddfe の P1）
        def recv_type(ws, kind, limit=30):
            for _ in range(limit):
                m = json.loads(ws.receive_text())
                if m.get("type") == kind:
                    return m
            return None

        hm.ask_hermes = lambda text: "わかりました"
        with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa:
            wa.send_text(json.dumps({"type": "hello", "speaker": 0, "win": "winstopgen001"}))
            g_hello = (recv_type(wa, "stopgen") or {}).get("n")
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wb:
                wb.send_text(json.dumps({"type": "stop", "n": 11}))
                receive_until(wb, "stopped")
            g_after = (recv_type(wa, "stopgen") or {}).get("n")
            wa.send_text(json.dumps({"type": "begin", "b": 1, "pane": hm.HERMES_PANE, "hid": -1, "sg": g_hello}))  # 停止を知る前に始めた（遅れて届いた）
            wa.send_text(json.dumps({"type": "text", "text": "はい（停止を知る前に始めた）", "pane": hm.HERMES_PANE, "speaker": 0, "hid": -1, "begin": 1}))
            stale = row_of("はい（停止を知る前に始めた）")
            stale_cur = s.HUB.current(stale[0]) if stale else None
            wa.send_text(json.dumps({"type": "begin", "b": 2, "pane": hm.HERMES_PANE, "hid": -1, "sg": g_after}))  # 停止を知ってから始めた
            wa.send_text(json.dumps({"type": "text", "text": "はい（停止を知ってから始めた）", "pane": hm.HERMES_PANE, "speaker": 0, "hid": -1, "begin": 2}))
            fresh = row_of("はい（停止を知ってから始めた）")
            fresh_cur = s.HUB.current(fresh[0]) if fresh else None
        check("停止の回数は名乗りの返事と別の画面の停止で届き、最新の停止を知らずに始めた発言は使えず、知ってから始めた発言は使える",
              (type(g_hello) is int, g_after == (g_hello or 0) + 1, stale_cur, fresh_cur), (True, True, False, True))
        # 話し始めるときの再生だけの停止（scope=talk）では、画面に届いていた問いを聞いた文から消さない（その問いへの声の「はい」を聞き直しにしない。codex-review 6b2ddfe）
        with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa:
            wa.send_text(json.dumps({"type": "hello", "speaker": 0, "win": "wintalkstop01"}))
            g0 = (recv_type(wa, "stopgen") or {}).get("n")
            hm.ask_hermes = lambda text: Q
            wa.send_text(json.dumps({"type": "begin", "b": 1, "pane": hm.HERMES_PANE, "hid": -1, "sg": g0}))
            wa.send_text(json.dumps({"type": "text", "text": "問いを聞く（再生だけの停止）", "pane": hm.HERMES_PANE, "speaker": 0, "hid": -1, "begin": 1}))
            q_reply = None
            for _ in range(40):
                m = json.loads(wa.receive_text())
                if m.get("type") == "reply" and m.get("text") == Q:
                    q_reply = m
                    break
            time.sleep(0.2)
            hm.ask_hermes = lambda text: "わかりました"
            wa.send_text(json.dumps({"type": "stop", "scope": "talk"}))  # 読み上げの途中で話し始めた
            recv_type(wa, "stopped")
            hid_q = (q_reply or {}).get("hid", -1)
            wa.send_text(json.dumps({"type": "begin", "b": 2, "pane": hm.HERMES_PANE, "hid": hid_q, "sg": g0}))
            wa.send_text(json.dumps({"type": "text", "text": "はい（再生だけ止めて答えた）", "pane": hm.HERMES_PANE, "speaker": 0, "hid": hid_q, "begin": 2}))
            talk_row = row_of("はい（再生だけ止めて答えた）")
        check("話し始めるときの再生だけの停止では、届いていた問いを聞いた文から消さない", talk_row[3] if talk_row else None, Q)
        # 添付の用意の間に別の画面で止めたら、「送っていません」のあとに「送りました」と出さない（codex-review 6b2ddfe）
        check("（前提）前の試験の直接の指示が片づいている（添付の用意の間の停止）", wait_idle(), True)
        att_go21, att_in21 = threading.Event(), threading.Event()

        async def held_attach21(ids, talker, pane=""):
            att_in21.set()
            await asyncio.to_thread(att_go21.wait, 5)
            return "", ""

        real_attach21 = s.attachment_lines
        s.attachment_lines = held_attach21
        s.list_agents = lambda: [{"pane": "w9:p1", "cwd": "/x/proj", "agent": "claude", "session": "s1"}]
        seen21 = []
        try:
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa:
                wa.send_text(json.dumps({"type": "hello", "speaker": 0, "win": "winattstop01"}))
                g1 = (recv_type(wa, "stopgen") or {}).get("n")
                wa.send_text(json.dumps({"type": "begin", "b": 1, "pane": "w9:p1", "hid": -1, "sg": g1}))
                wa.send_text(json.dumps({"type": "text", "text": "添付の用意の間に止める指示", "pane": "w9:p1", "speaker": 0, "hid": -1, "begin": 1}))
                check("（前提）添付の用意で止めている", att_in21.wait(5), True)
                with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wb:
                    wb.send_text(json.dumps({"type": "stop", "n": 12}))
                    receive_until(wb, "stopped")
                att_go21.set()
                time.sleep(0.5)
                wa.send_text(json.dumps({"type": "stop", "n": 13}))  # 読み終わりの目印
                for _ in range(40):
                    m = json.loads(wa.receive_text())
                    seen21.append((m.get("type"), m.get("text", "")))
                    if m.get("type") == "stopped":
                        break
        finally:
            att_go21.set()
            s.attachment_lines, s.list_agents = real_attach21, real_agents
        i21 = next((i for i, (t, x) in enumerate(seen21) if "送っていません" in x), None)
        check("添付の用意の間に止めたら、「送っていません」のあとに「送りました」と出さない",
              (i21 is not None, i21 is not None and any("エージェントに送りました" in x for t, x in seen21[i21:])), (True, False))
        # 承認に答える・閉じるは、いまの発言の画面が同期の合図を返すまで待つ。返事より前にその画面が送った停止（聞き取りの間に押した
        # 停止など）は、返事を読むまでに本体が読み終えているので、止めた発言では答えない（codex-review 62a9436 の P1）
        check("（前提）前の試験の直接の指示が片づいている（同期）", wait_idle(), True)
        hm.ask_hermes = lambda text: "わかりました"
        limit0, s.SETTLE_LIMIT = s.SETTLE_LIMIT, 0.3
        try:
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa:
                wa.send_text(json.dumps({"type": "hello", "speaker": 0, "win": "winsync00001", "sync": True}))
                g2 = (recv_type(wa, "stopgen") or {}).get("n")
                wa.send_text(json.dumps({"type": "begin", "b": 1, "pane": hm.HERMES_PANE, "hid": -1, "sg": g2}))
                wa.send_text(json.dumps({"type": "text", "text": "はい（同期の前に止める）", "pane": hm.HERMES_PANE, "speaker": 0, "hid": -1, "begin": 1}))
                sync = {}
                for _ in range(40):  # 同期の合図は、仕事にした直後（返事より先）に届く。返事が先なら届いていない
                    m = json.loads(wa.receive_text())
                    if m.get("type") == "sync" or m.get("type") == "reply":
                        sync = m if m.get("type") == "sync" else {}
                        break
                row = row_of("はい（同期の前に止める）")
                before = (asyncio.run(s.hub_settled()), s.HUB.current(row[0]) if row else None)
                wa.send_text(json.dumps({"type": "stop", "n": 21}))  # 同期の合図を受け取る前に押した停止（返事より前に届く）
                wa.send_text(json.dumps({"type": "sync", "n": sync.get("n")}))
                recv_type(wa, "stopped")
                end = time.time() + 3
                while time.time() < end and sync.get("n") in s.SETTLING:
                    time.sleep(0.02)
                after = (asyncio.run(s.hub_settled()), s.HUB.current(row[0]) if row else None)
            check("同期の合図を返すまでは答えず、返したときには、その前に押した停止を読み終えている（その発言はもういまの発言ではない）",
                  (type(sync.get("n")) is int, before, after), (True, (False, True), (True, False)))
            # 同期の合図を返す前に切れた発言は、送られた停止が届かずに消えたかもしれないので、答え・閉じるに使わない（codex-review 5fabc67 の P1）
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa:
                wa.send_text(json.dumps({"type": "hello", "speaker": 0, "win": "winsync00002", "sync": True}))
                g3 = (recv_type(wa, "stopgen") or {}).get("n")
                wa.send_text(json.dumps({"type": "begin", "b": 1, "pane": hm.HERMES_PANE, "hid": -1, "sg": g3}))
                wa.send_text(json.dumps({"type": "text", "text": "はい（同期の前に切れる）", "pane": hm.HERMES_PANE, "speaker": 0, "hid": -1, "begin": 1}))
                sync2 = {}
                for _ in range(40):
                    m = json.loads(wa.receive_text())
                    if m.get("type") in ("sync", "reply"):
                        sync2 = m if m.get("type") == "sync" else {}
                        break
            end = time.time() + 3
            while time.time() < end and not (s.SETTLING.get(sync2.get("n")) or [threading.Event()])[0].is_set():
                time.sleep(0.02)
            check("同期の合図を返す前に切れた発言では、答え・閉じるを断る", (type(sync2.get("n")) is int, asyncio.run(s.hub_settled())), (True, False))
        finally:
            s.SETTLE_LIMIT = limit0
        # 録音中（受け付けたあと、往復にする前）に報告が届いたら、録音を始めたときに控えた前の問いは、その発言に引き渡さない（codex-review 2eccfb5 の P1）
        hm.HERMES_HEARD["text"] = Q
        hm.HEARD_ID["n"] = 9001  # 画面が受け取っていた返事の番号（試験の中だけの値）
        g_rep = hm.hermes_admit(hm.HEARD_ID["n"])
        held_before = hm.HEARD_AT.get(g_rep)
        asyncio.run(hm.TALKER.report_end("done", "別件の報告です", "proj", True))
        check("報告をまたいだ発言には、受け付けたときに控えた前の問いを引き渡さない", (held_before, hm.HEARD_AT.get(g_rep, "")), (Q, ""))
        # 名乗りへの返事（停止の回数・控えた知らせ）の送りが詰まっても、受信は待たない（そのあと届いた停止を読む。codex-review f25d488 の P1）
        class SlowSock:
            def __init__(self, inbox):
                self.inbox, self.texts, self.released, self.during, self.blocking = list(inbox), [], asyncio.Event(), [], asyncio.Event()

            async def accept(self):
                pass

            async def send_text(self, t):
                m = json.loads(t)
                self.texts.append(m)
                if m.get("type") == "stopgen" and not self.during:
                    self.blocking.set()
                    try:  # 名乗りへの返事の送りが詰まっている（上限 1 秒）。詰まっている間に、本体が停止を読んだか
                        await asyncio.wait_for(self.released.wait(), 1)
                    except asyncio.TimeoutError:
                        pass
                    self.during.append(s.STOP_GEN["n"] != g_slow)

            async def send_bytes(self, b):
                pass

            async def receive(self):
                while self.inbox:
                    m = self.inbox.pop(0)
                    if not callable(m):
                        return {"type": "websocket.receive", "text": json.dumps(m)}
                    await m()
                return {"type": "websocket.disconnect"}

        g_slow, read_slow = s.STOP_GEN["n"], []

        async def stop_read():
            end = time.monotonic() + 2
            while s.STOP_GEN["n"] == g_slow and time.monotonic() < end:
                await asyncio.sleep(0.02)
            slow.released.set()
            end = time.monotonic() + 2
            while not slow.during and time.monotonic() < end:  # 結果を控えるまで切らない
                await asyncio.sleep(0.02)

        async def until_blocked():  # 名乗りへの送りが詰まってから、停止を届ける
            try:
                await asyncio.wait_for(slow.blocking.wait(), 2)
            except asyncio.TimeoutError:
                pass

        slow = SlowSock([{"type": "hello", "speaker": 0, "win": "winslow00001", "sync": True}, until_blocked, {"type": "stop", "n": 31}, stop_read])
        asyncio.run(s.ws(slow))
        s.PANE_LOCKS.clear()
        s.ACCEPT_LOCKS.clear()
        check("名乗りへの返事の送りが詰まっていても、そのあと届いた停止を読む", (slow.blocking.is_set(), slow.during[:1]), (True, [True]))
        # 停止の確定は、操作の送り始め（確かめてから herdr を起動するまで）と同じ鍵の中で行う。送り始めの途中なら、停止はそれを待ってから番号を進める
        # （「止めました」と出たあとに、止めた発言での操作を始めない。codex-review 635eea0 の P1）
        g_lock = s.STOP_GEN["n"]
        s.COMMIT_LOCK.acquire()  # 送り始めの途中
        try:
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wl:
                wl.send_text(json.dumps({"type": "stop", "n": 91}))
                time.sleep(0.4)
                during_lock = s.STOP_GEN["n"] != g_lock
                s.COMMIT_LOCK.release()
                receive_until(wl, "stopped")
                after_lock = s.STOP_GEN["n"] != g_lock
        finally:
            if s.COMMIT_LOCK.locked():
                s.COMMIT_LOCK.release()
        check("送り始めの途中の停止は、送り始めが終わってから番号を進める", (during_lock, after_lock), (False, True))
        # 次の発言の受け付け（受け付けの番号を進める）も、送り始めの途中なら、それを待ってから進める（codex-review f0b77a7 の P1）
        h_lock = hm.HERMES_GEN["n"]
        s.COMMIT_LOCK.acquire()  # 送り始めの途中
        try:
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wl:
                wl.send_text(json.dumps({"type": "begin", "b": 1, "pane": hm.HERMES_PANE, "hid": -1}))
                time.sleep(0.4)
                adm_during = hm.HERMES_GEN["n"] != h_lock
                s.COMMIT_LOCK.release()
                end = time.time() + 3
                while time.time() < end and hm.HERMES_GEN["n"] == h_lock:
                    time.sleep(0.02)
                adm_after = hm.HERMES_GEN["n"] != h_lock
        finally:
            if s.COMMIT_LOCK.locked():
                s.COMMIT_LOCK.release()
        check("送り始めの途中に受け付けた次の発言は、送り始めが終わってから受け付けの番号を進める", (adm_during, adm_after), (False, True))
        # 本物の herdr の道: 確かめと起動を、鍵の中で行い、起動したら放す（本物の herdr は起動しない。起動の道具を差し替える）
        started = []

        class FakeProc:
            returncode = 0

            def communicate(self, timeout=None):
                started.append(("wait", s.COMMIT_LOCK.locked(), len(s.HELD_OPS)))  # 返るのを待つ間に鍵を持っているか・送り途中の印があるか
                return '{"result": {}}', ""

        real_popen, real_run = s.subprocess.Popen, s.subprocess.run
        s.subprocess.Popen = lambda *a, **k: (started.append(s.COMMIT_LOCK.locked()), FakeProc())[1]
        s.subprocess.run = lambda *a, **k: (started.append("run"), type("P", (), {"stdout": '{"result": {}}', "stderr": "", "returncode": 0})())[1]
        try:
            r_ok = s._if_still(lambda: True, s._HERDR, "agent", "send-keys", "w9:p1", "1")
            r_no = s._if_still(lambda: False, s._HERDR, "agent", "send-keys", "w9:p1", "1")
            r_ask = s._if_still(lambda: True, functools.partial(s._HERDR, timeout=610), "agent", "prompt", "w9:p1", "x")
            # 鍵が使われていたら: 承認のキー・閉じるは待たずに断る。ハブ以外への指示は空くのを待つ（codex-review e749b6a の P1）
            s.COMMIT_LOCK.acquire()
            threading.Timer(0.3, s.COMMIT_LOCK.release).start()
            r_busy = s._if_still(lambda: True, s._HERDR, "agent", "send-keys", "w9:p1", "1")
            r_wait = s._if_still(lambda: True, functools.partial(s._HERDR, timeout=610), "agent", "prompt", "w9:p1", "x")
        finally:
            s.subprocess.Popen, s.subprocess.run = real_popen, real_run
        check("本物の herdr は、鍵の中で確かめて起動したら鍵を放し、承認のキー・閉じるは返るまで送り途中の印を持つ（確かめが False なら起動しない。"
              "鍵が使われていたら、承認のキー・閉じるは断り、指示は待つ。codex-review b7bf623・e749b6a）",
              (started, "result" in r_ok, r_no, "result" in r_ask, r_busy, "result" in (r_wait or {}), s.COMMIT_LOCK.locked(), len(s.HELD_OPS)),
              ([True, ("wait", False, 1), True, ("wait", False, 1), True, ("wait", False, 1)], True, None, True, s.BUSY, True, False, 0))
        # 停止は、送り途中の承認のキー・閉じるが返ってから「止めました」と返す。その間も、本体はほかの画面の相手を続ける（codex-review e749b6a の P2）
        held_op = threading.Event()
        s.HELD_OPS.add(held_op)
        t0 = time.time()
        threading.Timer(0.6, held_op.set).start()
        try:
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wl:
                wl.send_text(json.dumps({"type": "stop", "n": 92}))
                time.sleep(0.1)
                with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wo:
                    wo.send_text(json.dumps({"type": "hello", "speaker": 0, "win": "winother0001"}))
                    recv_type(wo, "stopgen")
                    other_at = time.time() - t0
                receive_until(wl, "stopped")
                stop_at = time.time() - t0
        finally:
            held_op.set()
            s.HELD_OPS.discard(held_op)
        check("停止は送り途中の承認のキー・閉じるが返ってから返し、その間もほかの画面の相手を続ける", (other_at < 0.55, stop_at >= 0.55), (True, True))
        # 送り途中の操作が上限までに返らなければ、「止めました」ではなく、止めたか確かめられないと返す（codex-review 57c6291 の P1）
        stuck_op = threading.Event()
        s.HELD_OPS.add(stuck_op)
        held0 = (s.HELD_TIMEOUT, s.HELD_MARGIN)
        s.HELD_TIMEOUT, s.HELD_MARGIN = 0.1, 0.1
        try:
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wl:
                wl.send_text(json.dumps({"type": "stop", "n": 93}))
                unsure_msg = recv_type(wl, "stopped") or {}
        finally:
            s.HELD_TIMEOUT, s.HELD_MARGIN = held0
            stuck_op.set()
            s.HELD_OPS.discard(stuck_op)
        check("送り途中の操作が上限までに返らなければ、止めたか確かめられないと返す", unsure_msg.get("unsure"), True)
        # herdr の待ちを打ち切ったら、送り途中の印は「確かめられない」で立てる（停止は「止めました」と言わない。codex-review d9db7c8 の P1）
        seen_ev = []

        class SlowProc:
            returncode = -9
            calls = 0

            def communicate(self, timeout=None):
                SlowProc.calls += 1
                if SlowProc.calls == 1:
                    seen_ev.extend(s.HELD_OPS)
                    raise s.subprocess.TimeoutExpired("herdr", timeout or 0)
                return "", ""

            def kill(self):
                pass

        real_popen2 = s.subprocess.Popen
        s.subprocess.Popen = lambda *a, **k: SlowProc()
        try:
            r_slow = s._if_still(lambda: True, s._HERDR, "agent", "send-keys", "w9:p1", "1")
        finally:
            s.subprocess.Popen = real_popen2
        unsure_until = s.UNSURE_UNTIL["t"] > time.monotonic()
        # 打ち切ったあと（印が一覧から外れたあと）に来た停止にも、しばらくは確かめられないと返す（codex-review 9d641dc の P1）
        with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wl:
            wl.send_text(json.dumps({"type": "stop", "n": 94}))
            late_msg = recv_type(wl, "stopped") or {}
        s.UNSURE_UNTIL["t"] = 0.0
        with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wl:
            wl.send_text(json.dumps({"type": "stop", "n": 95}))
            clear_msg = recv_type(wl, "stopped") or {}
        # herdr の返事が、結果（result）でもはっきりした拒否（error）でもない（中身の分からない返事）なら、確かめられないで立てる（codex-review e7ab276 の P1）
        odd_ev = []

        class OddProc:
            returncode = 0

            def communicate(self, timeout=None):
                odd_ev.extend(s.HELD_OPS)
                return "中身の分からない返事", ""

        class RejectProc:
            returncode = 1

            def communicate(self, timeout=None):
                odd_ev.extend(s.HELD_OPS)
                return '{"error": {"code": "agent_blocked"}}', ""

        class StalledProc:
            returncode = 1

            def communicate(self, timeout=None):
                odd_ev.extend(s.HELD_OPS)
                return '{"error": {"code": "agent_prompt_stalled"}}', ""

        class TimeoutErrProc:
            returncode = 1

            def communicate(self, timeout=None):
                odd_ev.extend(s.HELD_OPS)
                return '{"error": {"code": "timeout", "message": "応答を待ちきれませんでした"}}', ""

        s.UNSURE_UNTIL["t"] = 0.0
        real_popen3 = s.subprocess.Popen
        try:
            s.subprocess.Popen = lambda *a, **k: OddProc()
            r_odd = s._if_still(lambda: True, s._HERDR, "agent", "send-keys", "w9:p1", "1")
            odd_unsure = s.UNSURE_UNTIL["t"] > time.monotonic()
            s.UNSURE_UNTIL["t"] = 0.0
            s.subprocess.Popen = lambda *a, **k: RejectProc()
            s._if_still(lambda: True, s._HERDR, "agent", "send-keys", "w9:p1", "1")
            reject_unsure = s.UNSURE_UNTIL["t"] > time.monotonic()
            s.subprocess.Popen = lambda *a, **k: StalledProc()  # 送ったが 5 秒で working を見なかった（受け取りは済んでいる）
            s._if_still(lambda: True, s._HERDR, "agent", "send-keys", "w9:p1", "1")
            stalled_unsure = s.UNSURE_UNTIL["t"] > time.monotonic()
            s.subprocess.Popen = lambda *a, **k: TimeoutErrProc()  # herdr が応答待ちを打ち切った error（届いたかは分からない。codex-review 711ce71 の P1）
            s._if_still(lambda: True, s._HERDR, "agent", "send-keys", "w9:p1", "1")
            errtimeout_unsure = s.UNSURE_UNTIL["t"] > time.monotonic()
        finally:
            s.subprocess.Popen = real_popen3
            s.UNSURE_UNTIL["t"] = 0.0
        check("herdr の返事の中身が分からない・応答待ちを打ち切った error なら確かめられないで立て、実行していないと分かる拒否なら確かめられたとする",
              ("raw" in r_odd, [getattr(e, "ok", None) for e in odd_ev], odd_unsure, reject_unsure, stalled_unsure, errtimeout_unsure),
              (True, [False, True, True, False], True, False, False, True))
        # 鍵が使われている間に停止が来ても、イベントループは止めない（ほかの画面の相手を続ける。停止は鍵が空いてから確定する。codex-review 711ce71）
        g_busy = s.STOP_GEN["n"]
        s.COMMIT_LOCK.acquire()
        t_busy = time.time()
        busy_release = threading.Timer(0.8, s.COMMIT_LOCK.release)  # 時間で放す（イベントループが止まる作りでも、試験が止まらずに落ちるように）
        busy_release.start()
        try:
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wl:
                wl.send_text(json.dumps({"type": "stop", "n": 98}))
                time.sleep(0.1)
                with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wo:
                    wo.send_text(json.dumps({"type": "hello", "speaker": 0, "win": "winbusy00001"}))
                    recv_type(wo, "stopgen")
                    busy_other = time.time() - t_busy
                    busy_during = s.STOP_GEN["n"] != g_busy
                receive_until(wl, "stopped")
                busy_after = s.STOP_GEN["n"] != g_busy
        finally:
            busy_release.join()
        # 本体が読んだが確定していない停止があれば、承認のキー・閉じる・指示の送りを始めない。確定待ちの次の発言の受け付けがあれば、
        # 承認のキー・閉じるは始めない（指示の送りは始める）。codex-review bee122f の P1
        pend_started = []

        class PendProc:
            returncode = 0

            def communicate(self, timeout=None):
                return '{"result": {}}', ""

        real_popen4 = s.subprocess.Popen
        s.subprocess.Popen = lambda *a, **k: (pend_started.append(a[0][1:3]), PendProc())[1]
        try:
            s.PENDING["stop"] = 1
            # 指示の送りは、確定待ちの停止が確定するのを待ってから確かめる（停止の側が取り消して知らせる。黙って送らずに終えない。codex-review a1fa82c）
            dropped_by_stop = {"v": False}

            def finish_stop():
                time.sleep(0.3)
                dropped_by_stop["v"] = True  # 停止の確定で、送る前の指示が取り消された
                s.PENDING["stop"] = 0

            threading.Thread(target=finish_stop).start()
            t_direct = time.time()
            p_direct = s._if_still(lambda: not dropped_by_stop["v"], functools.partial(s._HERDR, timeout=30), "agent", "prompt", "w9:p1", "x")
            direct_waited = time.time() - t_direct >= 0.25
            # 確定待ちの停止が上限までに確定しなければ、送らずに、送れなかったと返す（codex-review 070146f）
            s.PENDING["stop"], wait0 = 1, s.PENDING_WAIT
            s.PENDING_WAIT = 0.2
            try:
                p_expired = s._if_still(lambda: True, functools.partial(s._HERDR, timeout=30), "agent", "prompt", "w9:p1", "x")
            finally:
                s.PENDING_WAIT = wait0
            p_stop = (s._if_still(lambda: True, s._HERDR, "agent", "send-keys", "w9:p1", "1"), p_direct, direct_waited,
                      (p_expired or {}).get("error", {}).get("code"), s.COMMIT_LOCK.locked())
            s.PENDING["stop"], s.PENDING["admit"] = 0, 1
            p_admit = (s._if_still(lambda: True, s._HERDR, "agent", "send-keys", "w9:p1", "1"),
                       "result" in (s._if_still(lambda: True, functools.partial(s._HERDR, timeout=30), "agent", "prompt", "w9:p1", "x") or {}))
        finally:
            s.PENDING["stop"] = s.PENDING["admit"] = 0
            s.subprocess.Popen = real_popen4
        check("確定待ちの停止があれば承認のキー・閉じるは始めず、指示の送りは停止の確定を待ってから確かめる。確定待ちの受け付けがあれば承認のキー・閉じるだけ始めない",
              (p_stop, p_admit, pend_started), ((s.BUSY, None, True, "stop_pending", False), (s.BUSY, True), [["agent", "prompt"]]))
        # 停止は、読んだその場で確定待ちに数え、鍵を取って確定したら外す
        s.COMMIT_LOCK.acquire()
        rel = threading.Timer(0.5, s.COMMIT_LOCK.release)
        rel.start()
        try:
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wl:
                wl.send_text(json.dumps({"type": "stop", "n": 99}))
                time.sleep(0.2)
                pend_during = s.PENDING["stop"]
                receive_until(wl, "stopped")
                pend_after = s.PENDING["stop"]
        finally:
            rel.join()
        check("停止は、読んだその場で確定待ちに数え、確定したら外す", (pend_during, pend_after), (1, 0))
        # 鍵を待つ間に取り消されても、あとで取れた鍵はその場で放す（全部の画面の受け付けと停止を詰まらせない。codex-review bee122f）

        async def cancel_case():
            s.COMMIT_LOCK.acquire()

            async def enter():
                async with s.invalidating("admit"):
                    pass

            t = asyncio.create_task(enter())
            await asyncio.sleep(0.1)
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            s.COMMIT_LOCK.release()
            await asyncio.sleep(0.3)
            return s.COMMIT_LOCK.locked(), s.PENDING["admit"]

        cancel_got = asyncio.run(cancel_case())
        if s.COMMIT_LOCK.locked():  # 取り残した（直しを外したとき）。あとの試験を詰まらせない
            s.COMMIT_LOCK.release()
        s.PENDING["admit"] = 0
        check("鍵を待つ間に取り消されても、あとで取れた鍵は放し、確定待ちからも外す", cancel_got, (False, 0))
        # 鍵は、空いていればその場で取り、使われていれば専用の実行枠で待つ（返事待ちの仕事が既定の実行枠を使い切っても、停止と受け付けを並ばせない。codex-review a1fa82c）

        async def pool_case():
            loop = asyncio.get_running_loop()
            used = []
            real_rie = loop.run_in_executor

            def spy(ex, fn, *a):
                used.append(ex)
                return real_rie(ex, fn, *a)

            loop.run_in_executor = spy
            await s._acquire_commit()  # 空いている
            s.COMMIT_LOCK.release()
            s.COMMIT_LOCK.acquire()  # 使われている
            threading.Timer(0.2, s.COMMIT_LOCK.release).start()
            await s._acquire_commit()
            s.COMMIT_LOCK.release()
            return [u is s.COMMIT_POOL for u in used]

        check("鍵は、空いていればその場で取り、使われていれば専用の実行枠で待つ", asyncio.run(pool_case()), [True])
        check("鍵が使われている間に停止が来ても、ほかの画面の相手を続け、停止は鍵が空いてから確定する",
              (busy_other < 0.5, busy_during, busy_after), (True, False, True))
        # 止めたあとに届いた声を捨てたときは、最後の停止と同じ返事（番号・確かめられない）をする（番号の無い「止めました」で確かめの頼みを上書きしない。codex-review e7ab276）
        s.UNSURE_UNTIL["t"] = time.monotonic() + 30
        try:
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wv:
                wv.send_text(json.dumps({"type": "turn", "pane": hm.HERMES_PANE, "speaker": 0, "filename": "rec.webm", "hid": -1}))
                wv.send_text(json.dumps({"type": "stop", "n": 97}))
                first_stop = recv_type(wv, "stopped") or {}
                wv.send_bytes(b"\x00" * 16)
                dropped_stop = recv_type(wv, "stopped") or {}
        finally:
            s.UNSURE_UNTIL["t"] = 0.0
        check("止めたあとに届いた声を捨てたときは、最後の停止と同じ返事をする",
              ((first_stop.get("n"), first_stop.get("unsure")), (dropped_stop.get("n"), dropped_stop.get("unsure"))), ((97, True), (97, True)))
        check("herdr の待ちを打ち切ったら、送り途中の印は確かめられないで立て、そのあとしばらくの停止にも確かめられないと返す",
              (r_slow.get("timeout"), len(seen_ev), seen_ev[0].is_set() if seen_ev else None, s.wait_inflight(seen_ev), len(s.HELD_OPS),
               unsure_until, late_msg.get("unsure"), clear_msg.get("unsure")),
              (True, 1, True, False, 0, True, True, None))
        # 指示を送るのは、送る（受け取ったら返る。返事を待たない）と、返事を待つに分ける（codex-review 57c6291 の P1）
        calls_ask = []
        real_herdr_ask, real_jsonl_ask = s.herdr, s.jsonl_path
        s.herdr = lambda *a, **k: (calls_ask.append(a), {"result": {"agent": {"agent_status": "idle"}}})[1]
        s.jsonl_path = lambda a: None
        real_read_screen = s.read_screen
        s.read_screen = lambda pane, n: "返事"
        try:
            got_ask = asyncio.run(s.ask({"pane": "w9:p1", "cwd": "/x/proj", "agent": "claude", "session": "s1"}, "送る指示"))
        finally:
            s.herdr, s.jsonl_path, s.read_screen = real_herdr_ask, real_jsonl_ask, real_read_screen
        # 最初の待ちで承認待ち（blocked）を見たら、その結果を使う（待ち直さない。codex-review 711ce71）
        calls_blk = []
        s.herdr = lambda *a, **k: (calls_blk.append(a), {"result": {"agent": {"agent_status": "blocked" if a[:2] == ("agent", "prompt") else "idle"}}})[1]
        s.jsonl_path = lambda a: None
        real_read_prompt = s.read_prompt
        s.read_prompt = lambda pane: "承認待ちの画面"
        try:
            got_blk = asyncio.run(s.ask({"pane": "w9:p1", "cwd": "/x/proj", "agent": "claude", "session": "s1"}, "送る指示"))
        finally:
            s.herdr, s.jsonl_path, s.read_prompt = real_herdr_ask, real_jsonl_ask, real_read_prompt
        check("指示を送って見届ける間に承認待ちを見たら、その結果を使う（待ち直さない）", (got_blk, len(calls_blk)), (("blocked", "承認待ちの画面"), 1))
        # 送ったが 5 秒で working を見なかった（agent_prompt_stalled。受け取りは済んでいる）なら、終わりを待つ
        calls_st = []
        s.herdr = lambda *a, **k: (calls_st.append(a[:2]), {"error": {"code": "agent_prompt_stalled"}} if a[:2] == ("agent", "prompt") else {"result": {"agent": {"agent_status": "idle"}}})[1]
        s.jsonl_path = lambda a: None
        real_read_screen2 = s.read_screen
        s.read_screen = lambda pane, n: "返事"
        try:
            got_st = asyncio.run(s.ask({"pane": "w9:p1", "cwd": "/x/proj", "agent": "claude", "session": "s1"}, "送る指示"))
        finally:
            s.herdr, s.jsonl_path, s.read_screen = real_herdr_ask, real_jsonl_ask, real_read_screen2
        check("送ったが working を見なかったら、受け取りは済んでいるので終わりを待つ", (got_st[0], calls_st), ("done", [("agent", "prompt"), ("agent", "wait")]))
        check("指示を送るのは、送った時点から working を見届けて（5 秒まで）、それから終わりを待つ", (calls_ask, got_ask[0]),
              ([("agent", "prompt", "w9:p1", "送る指示", "--wait", "--until", "working", "--until", "blocked", "--timeout", "10000"),
                ("agent", "wait", "w9:p1", "--timeout", "600000")], "done"))
        # 合図の無い発言が受け付けの鍵を待つ間に止めたら、その指示は送らずに知らせ、ハブへの「はい」もいまの発言にしない。合図の無い次の発言は、
        # 鍵を待つ前に前の指示を取り消す（codex-review 31c9e1c）
        check("（前提）前の試験の直接の指示が片づいている（受け付けの鍵の待ち）", wait_idle(), True)
        real_admit31 = s.admit_async
        gate31, slow31 = threading.Event(), {"on": False}

        async def slow_admit31(seen=None):
            if slow31["on"]:
                await asyncio.to_thread(gate31.wait, 5)
            return await real_admit31(seen)

        prompts31 = []
        real_herdr31, real_attach31 = s.herdr, s.attachment_lines
        s.admit_async = slow_admit31
        s.herdr = lambda *a, **k: (prompts31.append(a[3]) if a[:2] == ("agent", "prompt") else None, {"result": {"agent": {"agent_status": "idle"}}})[1]
        s.list_agents = lambda: [{"pane": "w9:p1", "cwd": "/x/proj", "agent": "claude", "session": "s1"}]
        got31, hub_cur31, dropped_before31, voice_cur31 = [], None, None, None
        try:
            slow31["on"] = True
            gate31.clear()
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa:
                wa.send_text(json.dumps({"type": "hello", "speaker": 0, "win": "winlegacy0001"}))
                wa.send_text(json.dumps({"type": "text", "text": "鍵を待つ間に止める指示", "pane": "w9:p1", "speaker": 0}))  # 合図の無い文字の指示
                time.sleep(0.2)
                with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wb:
                    wb.send_text(json.dumps({"type": "stop", "n": 101}))
                    receive_until(wb, "stopped")
                gate31.set()
                time.sleep(0.3)
                wait_idle()  # 送る仕事が終わるまで待つ（送ってしまう作りなら、ここで送られる。知らせを読む停止で取り消させない）
                got31 = read_notice(wa, lambda t, x: "送っていません" in x, 110)
            check("（前提）鍵を待つ間に止めた指示が片づいた", wait_idle(), True)
            gate31.clear()
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa:
                wa.send_text(json.dumps({"type": "text", "text": "はい（鍵を待つ間に止める）", "pane": hm.HERMES_PANE, "speaker": 0}))
                time.sleep(0.2)
                with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wb:
                    wb.send_text(json.dumps({"type": "stop", "n": 102}))
                    receive_until(wb, "stopped")
                gate31.set()
                row31 = row_of("はい（鍵を待つ間に止める）")
                hub_cur31 = s.HUB.current(row31[0]) if row31 else None
            # 声も同じ（合図の無い声の頭が受け付けの鍵を待つ間に止めたら、その声の「はい」はいまの発言にしない）
            gate31.clear()
            real_transcribe31 = s.transcribe
            s.transcribe = lambda audio, filename: "はい（声で鍵を待つ間に止める）"
            try:
                with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa:
                    wa.send_text(json.dumps({"type": "turn", "pane": hm.HERMES_PANE, "speaker": 0, "filename": "a.webm"}))
                    wa.send_bytes(b"0" * 4000)
                    time.sleep(0.2)
                    with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wb:
                        wb.send_text(json.dumps({"type": "stop", "n": 103}))
                        receive_until(wb, "stopped")
                    gate31.set()
                    # 声の本体は、受け付けの鍵を待つ間の停止のあとに読む。聞き取らずに捨てて知らせる（codex-review fdc6b04。いまの発言にもならない）
                    m31 = receive_until(wa, "error") or {}
                    voice_cur31 = "捨てた" if "捨てました" in m31.get("text", "") else m31.get("text")
            finally:
                s.transcribe = real_transcribe31
            check("（前提）前の試験の直接の指示が片づいている（前の指示の取り消し）", wait_idle(), True)
            slow31["on"] = False
            att_go31, att_in31 = threading.Event(), threading.Event()

            async def held_attach31(ids, talker, pane=""):
                att_in31.set()
                await asyncio.to_thread(att_go31.wait, 5)
                return "", ""

            s.attachment_lines = held_attach31
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa:
                wa.send_text(json.dumps({"type": "hello", "speaker": 0, "win": "winlegacy0002"}))
                wa.send_text(json.dumps({"type": "begin", "b": 1, "pane": "w9:p1", "hid": -1}))
                wa.send_text(json.dumps({"type": "text", "text": "前の指示（添付を用意中）", "pane": "w9:p1", "speaker": 0, "hid": -1, "begin": 1}))
                check("（前提）前の指示が添付の用意で止まっている", att_in31.wait(5), True)
                entry31 = next(iter(s.DIRECT_PENDING.values()), None)
                slow31["on"] = True
                gate31.clear()
                wa.send_text(json.dumps({"type": "text", "text": "次の合図の無い発言", "pane": "w9:p1", "speaker": 0}))
                time.sleep(0.2)
                dropped_before31 = entry31["dropped"] if entry31 else None
                gate31.set()
                att_go31.set()
                time.sleep(0.3)
        finally:
            gate31.set()
            s.admit_async, s.herdr, s.attachment_lines, s.list_agents = real_admit31, real_herdr31, real_attach31, real_agents
        check("合図の無い発言が受け付けの鍵を待つ間に止めたら、指示は送らずに知らせ、ハブへの「はい」もいまの発言にしない。次の合図の無い発言は、鍵を待つ前に前の指示を取り消す",
              (any("鍵を待つ間に止める指示" in x for x in prompts31), any("送っていません" in x for t, x in got31), hub_cur31, voice_cur31, dropped_before31),
              (False, True, False, "捨てた", True))
        # 指示の送りの、確定待ちの停止の上限は、鍵を取る前から数える（ほかの操作が鍵を長く持っていても、上限で送らずに返る。codex-review 31c9e1c）
        dl_started = []
        real_popen5 = s.subprocess.Popen
        s.subprocess.Popen = lambda *a, **k: (dl_started.append(1), None)[1]
        s.COMMIT_LOCK.acquire()
        rel5 = threading.Timer(1.0, s.COMMIT_LOCK.release)
        rel5.start()
        s.PENDING["stop"], wait5 = 1, s.PENDING_WAIT
        s.PENDING_WAIT = 0.2
        t5 = time.time()
        try:
            r_dl = s._if_still(lambda: True, functools.partial(s._HERDR, timeout=30), "agent", "prompt", "w9:p1", "x")
        finally:
            el5 = time.time() - t5
            s.PENDING_WAIT, s.PENDING["stop"] = wait5, 0
            rel5.join()
            s.subprocess.Popen = real_popen5
        check("指示の送りの上限は鍵を取る前から数え、上限で送らずに返る", ((r_dl or {}).get("error", {}).get("code"), el5 < 0.6, dl_started), ("stop_pending", True, []))
        # 受け付けは読んだ順に確定する（別々の画面の受け付けが鍵を待っても、先に読んだ発言に新しい番号を付けない。codex-review 27fc1d4 の P1）

        async def order_case():
            entered, gate = [], asyncio.Event()
            real_acq = s._acquire_commit

            async def fake_acq():
                entered.append(len(entered))
                await gate.wait()
                s.COMMIT_LOCK.acquire()

            s._acquire_commit = fake_acq
            try:
                t1 = asyncio.create_task(s.admit_async(-1))
                await asyncio.sleep(0.05)
                t2 = asyncio.create_task(s.admit_async(-1))
                await asyncio.sleep(0.05)
                before = len(entered)
                gate.set()
                a, b = await t1, await t2
            finally:
                s._acquire_commit = real_acq
            return before, a < b

        check("受け付けは読んだ順に 1 つずつ確定する（先に読んだ発言の番号が小さい）", asyncio.run(order_case()), (1, True))
        # 指示の送りは、確定待ちの停止が無いのを見て送ると決めるまでを、停止を読んだ印と並べる。見たあとに停止を読んだら、送らずに確定を待つ
        # （codex-review 27fc1d4 の P2）
        real_afs, inject = s._acquire_for_send, {"done": False}
        still_calls = []

        def afs_then_stop(end):
            ok = real_afs(end)
            if ok and not inject["done"]:  # 鍵を取った直後に、本体が停止を読んだ
                inject["done"] = True
                s.PENDING["stop"] = 1

                def finish():
                    time.sleep(0.3)
                    s.PENDING["stop"] = 0

                threading.Thread(target=finish).start()
            return ok

        sent6 = []
        real_popen6 = s.subprocess.Popen
        s.subprocess.Popen = lambda *a, **k: (sent6.append(1), None)[1]
        s._acquire_for_send = afs_then_stop
        t6 = time.time()
        try:
            r6 = s._if_still(lambda: (still_calls.append(round(time.time() - t6, 2)), False)[1], functools.partial(s._HERDR, timeout=30), "agent", "prompt", "w9:p1", "x")
        finally:
            s._acquire_for_send, s.subprocess.Popen = real_afs, real_popen6
            s.PENDING["stop"] = 0
        check("送ると決める前に停止を読んだら、送りを確定せずに停止の確定を待ってから確かめる",
              (r6, sent6, len(still_calls), bool(still_calls) and still_calls[0] >= 0.25), (None, [], 1, True))
        # 取り消して「送っていません」と知らせた指示には、「送信できませんでした」を重ねない（codex-review 27fc1d4 の P3）
        check("（前提）前の試験の直接の指示が片づいている（知らせは 1 回）", wait_idle(), True)
        real_ask7 = s.ask

        async def drop_then_fail(agent, text):
            for pend in list(s.DIRECT_PENDING.values()):  # 送る前に停止が確定して取り消した
                s.drop_pending(pend)
            return "error", "送れなかった"

        s.ask = drop_then_fail
        s.list_agents = lambda: [{"pane": "w9:p1", "cwd": "/x/proj", "agent": "claude", "session": "s1"}]
        got7 = []
        try:
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa:
                wa.send_text(json.dumps({"type": "hello", "speaker": 0, "win": "winonce00001"}))
                wa.send_text(json.dumps({"type": "begin", "b": 1, "pane": "w9:p1", "hid": -1}))
                wa.send_text(json.dumps({"type": "text", "text": "知らせは 1 回の指示", "pane": "w9:p1", "speaker": 0, "hid": -1, "begin": 1}))
                time.sleep(0.5)
                wait_idle()
                got7 = read_notice(wa, lambda t, x: "送っていません" in x, 120)
                time.sleep(0.3)
                got7 += read_notice(wa, lambda t, x: False, 130, tries=1)
        finally:
            s.ask, s.list_agents = real_ask7, real_agents
        check("取り消して知らせた指示には、送れなかったを重ねない。取り消しの知らせは notice（停止の結果の表示を消さない。codex-review 5fff07e）",
              (sum("送っていません" in x for t, x in got7), sum("送信できませんでした" in x for t, x in got7), [t for t, x in got7 if "送っていません" in x]),
              (1, 0, ["notice"]))
        # 声の頭を受けたあと、声の本体が届くまでに別の画面で止めたら、聞き取らずに捨てて録音した画面に知らせる（codex-review fdc6b04 の P1）
        check("（前提）前の試験の仕事が片づいている（声の本体の前の停止）", wait_idle(), True)
        heard8 = []
        real_transcribe8 = s.transcribe
        s.transcribe = lambda audio, filename: (heard8.append(1), "はい（本体の前に別の画面で止めた）")[1]
        try:
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa:
                wa.send_text(json.dumps({"type": "turn", "pane": hm.HERMES_PANE, "speaker": 0, "filename": "a.webm"}))
                time.sleep(0.2)
                with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wb:
                    wb.send_text(json.dumps({"type": "stop", "n": 104}))
                    receive_until(wb, "stopped")
                wa.send_bytes(b"0" * 4000)
                drop8 = receive_until(wa, "error")
        finally:
            s.transcribe = real_transcribe8
        check("声の本体の前に別の画面で止めたら、聞き取らずに捨てて知らせる", (heard8, "捨てました" in (drop8 or {}).get("text", "")), ([], True))
        # 次の発言を受け付けたあとに終わった古い往復は、あとから置かれた新しい問い（聞いた文と返事の番号）を消さない（codex-review fdc6b04）
        old_mark = hm.hermes_admit()
        ask0 = hm.ask_hermes

        def newer_then_old(text):  # 古い往復が Hermes を待つ間に、次の発言を受け付け、その返事が画面に届いた
            hm.hermes_admit()
            hm.HERMES_HEARD["text"], hm.HEARD_ID["n"] = Q, 9100
            return "古い往復の返事"

        hm.ask_hermes = newer_then_old
        check("（前提）前の試験の仕事が片づいている（古い往復）", wait_idle(), True)
        s.PANE_LOCKS.pop(hm.HERMES_PANE, None)  # 鍵はイベントループに結び付くので、この実行の分を作り直す
        try:
            asyncio.run(asyncio.wait_for(hm.hermes_exchange("古い往復", admitted=old_mark), 10))
        finally:
            hm.ask_hermes = ask0
            s.PANE_LOCKS.clear()
            s.ACCEPT_LOCKS.clear()
        check("次の発言のあとに終わった古い往復は、新しい問いの聞いた文と返事の番号を消さない", (hm.HERMES_HEARD["text"], hm.HEARD_ID["n"]), (Q, 9100))
        # 録音を始めた（合図の）あと、声の頭が届く前にどこかの画面で止めていた声は、聞き取らずに捨てる（codex-review 52a66a0 の P1）
        check("（前提）前の試験の仕事が片づいている（停止をまたいだ声）", wait_idle(), True)
        heard9 = []
        real_transcribe9 = s.transcribe
        s.transcribe = lambda audio, filename: (heard9.append(1), "はい（停止をまたいだ声）")[1]
        try:
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa:
                wa.send_text(json.dumps({"type": "hello", "speaker": 0, "win": "wincrossv001"}))
                g9 = (recv_type(wa, "stopgen") or {}).get("n")
                with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wb:
                    wb.send_text(json.dumps({"type": "stop", "n": 105}))
                    receive_until(wb, "stopped")
                wa.send_text(json.dumps({"type": "begin", "b": 1, "pane": hm.HERMES_PANE, "hid": -1, "sg": g9}))  # 停止を知る前に録音を始めた
                wa.send_text(json.dumps({"type": "turn", "pane": hm.HERMES_PANE, "speaker": 0, "filename": "a.webm", "hid": -1, "begin": 1}))
                wa.send_bytes(b"0" * 4000)
                drop9 = receive_until(wa, "error") or {}
        finally:
            s.transcribe = real_transcribe9
        check("録音を始めたあと声が届く前に止めていた声は、聞き取らずに捨てる", (heard9, "捨てました" in drop9.get("text", "")), ([], True))
        # 合図で受け付けた発言の、遅れて届いた声の頭で、あとから届いたハブの新しい問いを消さない（codex-review 52a66a0）
        with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa:
            wa.send_text(json.dumps({"type": "hello", "speaker": 0, "win": "winlatehd001"}))
            g10 = (recv_type(wa, "stopgen") or {}).get("n")
            check("（前提）ハブ以外への録音の合図を本体が受けた", begin_and_wait(wa, {"type": "begin", "b": 1, "pane": "w9:p1", "hid": -1, "sg": g10}), True)
            hm.HERMES_HEARD["text"], hm.HEARD_ID["n"] = Q, 9200  # 録音の間に、ほかの画面にハブの新しい問いが届いた
            real_transcribe10 = s.transcribe
            s.transcribe = lambda audio, filename: ""  # 聞き取りは空（この試験では聞き取らない）
            try:
                wa.send_text(json.dumps({"type": "turn", "pane": "w9:p1", "speaker": 0, "filename": "a.webm", "hid": -1, "begin": 1}))
                wa.send_bytes(b"0" * 4000)
                receive_until(wa, "error")
            finally:
                s.transcribe = real_transcribe10
        check("合図で受け付けた発言の遅れて届いた声の頭で、ハブの新しい問いを消さない", (hm.HERMES_HEARD["text"], hm.HEARD_ID["n"]), (Q, 9200))
        # 次の発言を受け付けたあとの古い発言が、断り・その場の答え・失敗・返信先なしで終わっても、あとの新しい問いを消さない（codex-review 83aa65b）
        check("（前提）前の試験の仕事が片づいている（古い発言の終わり方）", wait_idle(), True)
        kept11 = []

        def old_then_newer():
            g = hm.hermes_admit()
            hm.hermes_admit()  # その古い発言のあとに、次の発言を受け付けた
            hm.HERMES_HEARD["text"], hm.HEARD_ID["n"] = Q, 9300  # 次の発言の返事（新しい問い）が画面に届いた
            s.PANE_LOCKS.pop(hm.HERMES_PANE, None)
            return g

        g11 = old_then_newer()
        asyncio.run(hm.TALKER.refused("古い断り", "断りました", admitted=g11))
        kept11.append(hm.HERMES_HEARD["text"])
        g11 = old_then_newer()
        asyncio.run(hm.TALKER.answered_here("古いその場の答え", "答え", admitted=g11))
        kept11.append(hm.HERMES_HEARD["text"])
        g11 = old_then_newer()
        ask11 = hm.ask_hermes
        hm.ask_hermes = lambda text: (_ for _ in ()).throw(RuntimeError("届かなかった"))
        try:
            asyncio.run(hm.hermes_exchange("古い往復の失敗", admitted=g11))
        finally:
            hm.ask_hermes = ask11
        kept11.append(hm.HERMES_HEARD["text"])
        g11 = old_then_newer()
        asyncio.run(hm.hermes_exchange("古い返信", reply_at=1.5, admitted=g11))  # 返信先の報告が見つからずに断る
        kept11.append(hm.HERMES_HEARD["text"])
        s.PANE_LOCKS.clear()
        s.ACCEPT_LOCKS.clear()
        check("古い発言が断り・その場の答え・失敗・返信先なしで終わっても、あとの新しい問いを消さない", kept11, [Q, Q, Q, Q])
        # 止めて抜けた読み上げの終わりの知らせには、止めた印を付ける（画面は停止の結果の表示を上書きしない。codex-review 10a2010）
        class SpeakC:
            voice, speaker, audio = False, None, "wav"

            def __init__(self):
                self.got = []

            async def send(self, **m):
                self.got.append(m)

        sc_stop, sc_go = SpeakC(), SpeakC()
        ev_stop, ev_go = asyncio.Event(), asyncio.Event()
        ev_stop.set()
        asyncio.run(s.speak_to([sc_stop], "止めた報告", ev_stop, plain=True))
        asyncio.run(s.speak_to([sc_go], "ふつうの報告", ev_go, plain=True))
        check("止めて抜けた読み上げの終わりには止めた印を付け、ふつうの終わりには付けない",
              ([m for m in sc_stop.got if m.get("type") == "done"], [m for m in sc_go.got if m.get("type") == "done"]),
              ([{"type": "done", "sentences": 0, "stopped": True}], [{"type": "done", "sentences": 0}]))
        # 合図の無い更新前の画面の添付つきの直接の指示は、届くまでに止めたか分からないので送らず、読み込み直しを頼む
        prompts13 = []
        s.herdr = lambda *a, **k: (prompts13.append(a[3]) if a[:2] == ("agent", "prompt") else None, {"result": {"agent": {"agent_status": "idle"}}})[1]
        s.list_agents = lambda: [{"pane": "w9:p1", "cwd": "/x/proj", "agent": "claude", "session": "s1"}]
        seen13 = []
        try:
            w.send_text(json.dumps({"type": "text", "text": "古い画面の添付つきの指示", "pane": "w9:p1", "speaker": 0, "attachments": ["a1"]}))
            time.sleep(0.3)
            w.send_text(json.dumps({"type": "stop", "n": 2}))  # 前の試験の読み残しも含めて、止めたの返事までを全部見る
            for _ in range(60):
                m = json.loads(w.receive_text())
                seen13.append((m.get("type"), m.get("text", "")))
                if m.get("type") == "stopped":
                    break
        finally:
            s.herdr, s.list_agents = real_herdr, real_agents
        check("合図の無い更新前の画面の添付つきの直接の指示は送らず、読み込み直しを頼む", (prompts13, any("読み込み直して" in x for t, x in seen13)), ([], True))
        # 合図の無い更新前の画面の声の直接の指示も、録音を始めた時点が分からないので送らず、読み込み直しを頼む（a2a 4135f4c2 の 3 回目）
        prompts14 = []
        s.herdr = lambda *a, **k: (prompts14.append(a[3]) if a[:2] == ("agent", "prompt") else None, {"result": {"agent": {"agent_status": "idle"}}})[1]
        s.list_agents = lambda: [{"pane": "w9:p1", "cwd": "/x/proj", "agent": "claude", "session": "s1"}]
        s.transcribe = lambda audio, filename: "古い画面の声の指示"
        seen14 = []
        try:
            w.send_text(json.dumps({"type": "turn", "pane": "w9:p1", "speaker": 0, "filename": "a.webm"}))
            w.send_bytes(b"0" * 4000)
            time.sleep(0.3)
            w.send_text(json.dumps({"type": "stop", "n": 3}))
            for _ in range(60):
                m = json.loads(w.receive_text())
                seen14.append((m.get("type"), m.get("text", "")))
                if m.get("type") == "stopped":
                    break
        finally:
            s.herdr, s.list_agents, s.transcribe = real_herdr, real_agents, real_transcribe
        check("合図の無い更新前の画面の声の直接の指示は送らず、読み込み直しを頼む", (prompts14, any("読み込み直して" in x for t, x in seen14)), ([], True))
        # 次の声の発言の頭を受けたら、この画面の前の、まだ送っていないハブ以外への指示は送らずに知らせる（文字と同じ。codex-review c72fbbe）
        prompts11, gate11, entered11 = [], threading.Event(), threading.Event()

        def waiting_if_still11(still, fn, *a):
            entered11.set()
            gate11.wait(5)
            return real_if(still, fn, *a)

        s._if_still, s.jsonl_path = waiting_if_still11, (lambda a: None)
        s.herdr = lambda *a, **k: (prompts11.append(a[3]) if a[:2] == ("agent", "prompt") else None, {"result": {"agent": {"agent_status": "idle"}}})[1]
        s.list_agents = lambda: [{"pane": "w9:p1", "cwd": "/x/proj", "agent": "claude", "session": "s1"}]
        s.transcribe = lambda audio, filename: "次の声の発言"
        hm.ask_hermes = lambda text: "わかりました"
        seen11 = []
        try:
            w.send_text(json.dumps({"type": "text", "text": "声の頭で取り消す指示", "pane": "w9:p1", "speaker": 0, "hid": -1}))
            check("（前提）前の指示が送るスレッドの手前で待っている", entered11.wait(5), True)
            w.send_text(json.dumps({"type": "turn", "pane": hm.HERMES_PANE, "speaker": 0, "filename": "a.webm", "hid": -1}))
            for _ in range(60):
                m = json.loads(w.receive_text())
                seen11.append((m.get("type"), m.get("text", "")))
                if m.get("type") in ("error", "notice") and any(k in m.get("text", "") for k in WAITED):
                    break
            gate11.set()
            w.send_bytes(b"0" * 4000)
            receive_until(w, "transcript")
            time.sleep(0.3)
        finally:
            gate11.set()
            s._if_still, s.herdr, s.list_agents, s.jsonl_path, s.transcribe = real_if, real_herdr, real_agents, real_jsonl, real_transcribe
        check("次の声の発言の頭を受けたら、前のまだ送っていないハブ以外への指示は送らずに知らせる",
              (prompts11, any(t in ("error", "notice") and "送っていません" in x for t, x in seen11)), ([], True))
        # 話し始めるときに再生だけを止める停止（scope=talk）では、ほかの画面の送る前の指示は取り消さない（codex-review dad00a5）
        prompts9, gate9, entered9 = [], threading.Event(), threading.Event()

        def waiting_if_still9(still, fn, *a):
            entered9.set()
            gate9.wait(5)
            return real_if(still, fn, *a)

        def fake_herdr9(*a, **k):
            if a[:2] == ("agent", "prompt"):
                prompts9.append(a[3])
            return {"result": {"agent": {"agent_status": "idle"}}}

        s._if_still, s.herdr, s.jsonl_path = waiting_if_still9, fake_herdr9, (lambda a: None)
        s.list_agents = lambda: [{"pane": "w9:p1", "cwd": "/x/proj", "agent": "claude", "session": "s1"}]
        try:
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wb:
                w.send_text(json.dumps({"type": "text", "text": "話し始めの停止では消さない指示", "pane": "w9:p1", "speaker": 0}))
                check("（前提）送るスレッドの手前で待っている（話し始めの停止の前）", entered9.wait(5), True)
                wb.send_text(json.dumps({"type": "stop", "scope": "talk"}))
                talk_reply = receive_until(wb, "stopped")
            gate9.set()
            for _ in range(100):
                if prompts9:
                    break
                time.sleep(0.05)
        finally:
            gate9.set()
            s._if_still, s.herdr, s.list_agents, s.jsonl_path = real_if, real_herdr, real_agents, real_jsonl
        check("話し始めるときの停止（scope=talk）では、ほかの画面の送る前の指示を取り消さない（返事にも scope を付ける）",
              (prompts9, (talk_reply or {}).get("scope")), (["話し始めの停止では消さない指示"], "talk"))
        # 別の画面の「停止」が、この画面の声（ハブ以外宛て）の聞き取りの間に来ても、聞き取ったあとの指示は送らずに知らせる（a2a 38ef4720 の 4 回目）
        prompts7, tr_in7, tr_go7 = [], threading.Event(), threading.Event()

        def slow_transcribe7(audio, filename):
            tr_in7.set()
            tr_go7.wait(5)
            return "聞き取りの間に別の画面で止めた指示"

        def fake_herdr7(*a, **k):
            if a[:2] == ("agent", "prompt"):
                prompts7.append(a[3])
            return {"result": {"agent": {"agent_status": "idle"}}}

        s.transcribe, s.herdr, s.jsonl_path = slow_transcribe7, fake_herdr7, (lambda a: None)
        s.list_agents = lambda: [{"pane": "w9:p1", "cwd": "/x/proj", "agent": "claude", "session": "s1"}]
        seen7 = []
        try:
            w.send_text(json.dumps({"type": "begin", "b": 951, "pane": "w9:p1", "hid": -1}))
            w.send_text(json.dumps({"type": "turn", "pane": "w9:p1", "speaker": 0, "filename": "a.webm", "hid": -1, "begin": 951}))
            w.send_bytes(b"0" * 4000)
            check("（前提）この画面の声を聞き取っている", tr_in7.wait(5), True)
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wb:
                wb.send_text(json.dumps({"type": "stop"}))
                receive_until(wb, "stopped")
            tr_go7.set()
            for _ in range(60):  # 知らせが来なければ、送ったあとの「回答を読めませんでした」で終わる
                m = json.loads(w.receive_text())
                seen7.append((m.get("type"), m.get("text", "")))
                if m.get("type") in ("error", "notice") and any(k in m.get("text", "") for k in WAITED):
                    break
            time.sleep(0.3)
        finally:
            tr_go7.set()
            s.transcribe, s.herdr, s.list_agents, s.jsonl_path = real_transcribe, real_herdr, real_agents, real_jsonl
        w.send_text(json.dumps({"type": "stop", "n": 41}))  # 知らせのあとに「送りました」と出ていないかを、止めたの返事まで読んで見る
        for _ in range(30):
            m = json.loads(w.receive_text())
            seen7.append((m.get("type"), m.get("text", "")))
            if m.get("type") == "stopped":
                break
        after = seen7[next(i for i, (t, x) in enumerate(seen7) if t in ("error", "notice") and "送っていません" in x):]
        check("別の画面の停止がこの画面の聞き取りの間に来たら、聞き取ったハブ以外への指示は送らず、送っていないと知らせる（そのあと「送りました」と出さない）",
              (prompts7, any(t in ("error", "notice") and "送っていません" in x for t, x in seen7), any("エージェントに送りました" in x for t, x in after)), ([], True, False))
        check("止めたの返事は、画面の停止の番号を付けて返す", seen7[-1], ("stopped", ""))
        check("止めたの返事の番号", m.get("n"), 41)
        # 指示を出した画面が、送るスレッドの空きを待つ間に切れても、別の画面の停止で取り消す（切れた画面の指示も送らせない）
        prompts8, gate8, entered8 = [], threading.Event(), threading.Event()

        def waiting_if_still8(still, fn, *a):
            entered8.set()
            gate8.wait(5)
            return real_if(still, fn, *a)

        def fake_herdr8(*a, **k):
            if a[:2] == ("agent", "prompt"):
                prompts8.append(a[3])
            return {"result": {"agent": {"agent_status": "idle"}}}

        s._if_still, s.herdr, s.jsonl_path = waiting_if_still8, fake_herdr8, (lambda a: None)
        s.list_agents = lambda: [{"pane": "w9:p1", "cwd": "/x/proj", "agent": "claude", "session": "s1"}]
        try:
            with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as wa:
                wa.send_text(json.dumps({"type": "text", "text": "切れる画面の指示", "pane": "w9:p1", "speaker": 0, "hid": -1}))
                check("（前提）切れる画面の指示が、送るスレッドの手前で待っている", entered8.wait(5), True)
            time.sleep(0.3)  # 画面 A が切れた
            w.send_text(json.dumps({"type": "stop"}))
            receive_until(w, "stopped")
            gate8.set()
            time.sleep(0.5)
        finally:
            gate8.set()
            s._if_still, s.herdr, s.list_agents, s.jsonl_path = real_if, real_herdr, real_agents, real_jsonl
        check("指示を出した画面が切れたあとでも、別の画面の停止で送る前の指示を取り消す", prompts8, [])
        # 送り始めたあとに次の発言でターンを取り消し、そのあとで相手が断ったら、送れなかったことを画面に知らせる（a2a 38ef4720）
        gate4, entered4 = threading.Event(), threading.Event()

        def refusing_herdr(*a, **k):
            if a[:2] == ("agent", "prompt"):
                entered4.set()
                gate4.wait(5)  # 相手が受け付けるのを待っている間
                return {"error": {"code": "agent_busy", "message": "受け付けません"}}
            return {"result": {}}

        s.herdr, s.jsonl_path = refusing_herdr, (lambda a: None)
        s.list_agents = lambda: [{"pane": "w9:p1", "cwd": "/x/proj", "agent": "claude", "session": "s1"}]
        seen4 = []
        try:
            w.send_text(json.dumps({"type": "text", "text": "断られる指示", "pane": "w9:p1", "speaker": 0}))
            check("（前提）相手に送り始めている", entered4.wait(5), True)
            hm.ask_hermes = lambda text: "わかりました"
            w.send_text(json.dumps({"type": "text", "text": "次の発言", "pane": hm.HERMES_PANE, "speaker": 0}))
            time.sleep(0.3)
            gate4.set()
            time.sleep(0.5)
            w.send_text(json.dumps({"type": "stop"}))
            for _ in range(60):
                m = json.loads(w.receive_text())
                seen4.append((m.get("type"), m.get("text", "")))
                if m.get("type") == "stopped":
                    break
        finally:
            gate4.set()
            s.herdr, s.list_agents, s.jsonl_path = real_herdr, real_agents, real_jsonl
        check("ハブ以外への指示を送り始めたあとに次の発言をし、相手が断ったら、送れなかったことを画面に知らせる",
              sum(t == "error" and "送信できませんでした" in x for t, x in seen4), 1)
        # 取り消さずに相手が断ったときも、知らせは 1 回（送る仕事の終わりの 1 か所で出す。ターンを取り消したのが断られたあとでも同じ所で出る）
        s.herdr = lambda *a, **k: {"error": {"code": "agent_busy", "message": "受け付けません"}} if a[:2] == ("agent", "prompt") else {"result": {}}
        s.list_agents = lambda: [{"pane": "w9:p1", "cwd": "/x/proj", "agent": "claude", "session": "s1"}]
        s.jsonl_path = lambda a: None
        seen5 = []
        try:
            w.send_text(json.dumps({"type": "text", "text": "すぐ断られる指示", "pane": "w9:p1", "speaker": 0}))
            time.sleep(0.5)
            w.send_text(json.dumps({"type": "stop"}))
            for _ in range(60):
                m = json.loads(w.receive_text())
                seen5.append((m.get("type"), m.get("text", "")))
                if m.get("type") == "stopped":
                    break
        finally:
            s.herdr, s.list_agents, s.jsonl_path = real_herdr, real_agents, real_jsonl
        check("ハブ以外への指示を相手が断ったら、取り消していなくても、送れなかった知らせは 1 回",
              sum(t == "error" and "送信できませんでした" in x for t, x in seen5), 1)
        check("声の頭と本体の間に止めたら、届いた声は聞き取らず、ハブにも台帳にも送らない",
              (first is not None, second and second.get("type"), heard_calls, any(r[1] == "止めたあとに届いた声" for r in list(hm.HERMES_TURNS))), (True, "stopped", [], False))
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
