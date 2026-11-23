"""ハブの返事を待つ間の本体の一言（「確認しています」。config.env の VL_PROGRESS_AFTER）の試験。Hermes との往復・声の合成・語り手は差し替える。
常駐の 8790・本物の Hermes・声のエンジンには触れない。
確かめること:
  - 返事が PROGRESS_AFTER 秒より遅いと、状態の文「確認しています…」と、一言の声を 1 つ送る。そのあとに返事の文と声が続く（一言のあとに終わりの合図は送らない）
  - 返事が早ければ、一言は出さない
  - 読み上げを切った画面（🔇）には、状態の文だけ（声は送らない）
  - 0 にすると使わない
  - 一言は記録にも、守りの台帳（利用者が聞いた返事）にも載せない。Hermes への頼みの中身も変えない
  - 声が作れなくても、返事は届く
  - 一言の声の合成が遅くても、返事を遅らせない。返事が先に来たら、まだ送っていない一言は捨てる（返事のあとに一言が届かない）
  - 一言の声を送ったあと往復が失敗・例外で終わったら、終わりの合図（done）を送って画面を話し終わりに戻す
  - 往復そのものが期限より前に TimeoutError で終わっても、一言は出さない（待つ期限と取り違えない）
  - 合成の間に読み上げを切った画面には、一言の声を送らない
  - 一言を送っている途中でターンが取り消された（止めて次の発言）ら、一言は取り消す（次のターンの返事のあとに届かない）
使い方: uv run spike/progress_test.py"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HERMES_KEY="k1", VL_HERMES_URL="http://127.0.0.1:9",
                  VL_HERMES_HOME=f"{TMP}/no-hermes")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import server as s  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


HUB = s.hermes.HERMES_PANE
mode = {"delay": 0.0, "synth_fail": False, "synth_delay": 0.0, "fail": None}
asked = []


async def fake_accepted(talker, text, pane, *a, **k):  # Hermes との往復の代わり。返事までの時間だけを決める
    asked.append(text)
    await asyncio.sleep(mode["delay"])
    if mode["fail"] == "why":
        return "", "Hermes に届きませんでした", True
    if mode["fail"] == "raise":
        raise RuntimeError("往復の途中で落ちました")
    if mode["fail"] == "timeout":
        raise asyncio.TimeoutError()
    return "わかりました。", "", True


def fake_synth(text, speaker):
    if mode["synth_fail"] and text == s.PROGRESS_TEXT:
        raise OSError("声のエンジンが止まっています")
    if text == s.PROGRESS_TEXT and mode["synth_delay"]:
        time.sleep(mode["synth_delay"])
    return f"WAV:{text}".encode()


async def fake_narrate(text, cancel):
    yield text


s.hub_accepted = fake_accepted
s.synth = fake_synth
s.narrate = fake_narrate


def turn(text="ねえ、いまどうなってる？", mute=False, mute_after=None, linger=0.0):
    """ハブに打った文を送り、終わり（done か error）までに届いたものを順に返す。文の通知は (種類, 文)、声は ("wav", 中身)。"""
    got = []
    with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
        with c.websocket_connect("ws://127.0.0.1:8790/ws") as ws:
            if mute:
                ws.send_text(json.dumps({"type": "hello", "voice": False}))
            ws.send_text(json.dumps({"type": "text", "text": text, "pane": HUB, "speaker": "8"}))
            t0 = time.monotonic()
            if mute_after is not None:  # 合成の間に、読み上げを切る
                time.sleep(mute_after)
                ws.send_text(json.dumps({"type": "hello", "voice": False}))
            for _ in range(40):
                m = ws.receive()
                if m.get("bytes") is not None:
                    got.append(("wav", m["bytes"].decode()))
                    continue
                d = json.loads(m["text"])
                got.append((d["type"], d.get("text", "")))
                if d["type"] == "reply":
                    got.append(("at", round(time.monotonic() - t0, 2)))
                if d["type"] == "error" or (d["type"] == "done" and mode["fail"] not in ("why", "raise")):  # 失敗の試しでは、done のあとのエラーまで読む
                    break
            time.sleep(linger)  # 遅れて送られるものを待つ（つないだまま。試験の本体を閉じると、本体の仕事も止まる）
    return got


try:
    s.PROGRESS_AFTER = 0.2
    mode["delay"] = 0.6
    got = turn()
    kinds = [k for k, _ in got]
    cue = ("status", "確認しています…")
    check("遅い返事: 状態の文「確認しています…」を出す", cue in got, True)
    check("遅い返事: 一言の声を 1 つ送る", got.count(("wav", "WAV:確認しています")), 1)
    check("遅い返事: 一言のあとに返事の文と声が続く（一言の声が先）",
          (got.index(("wav", "WAV:確認しています")) < got.index(("reply", "わかりました。")) < got.index(("wav", "WAV:わかりました。"))), True)
    check("遅い返事: 終わりの合図は 1 回だけ（返事の声のあと）", (kinds.count("done"), kinds[-1]), (1, "done"))
    check("遅い返事: Hermes への頼みの中身は変えない", asked[-1], "ねえ、いまどうなってる？")
    log = s.hermes.HERMES_LOG.read_text() if s.hermes.HERMES_LOG.exists() else ""
    check("一言は記録にも、守りの台帳の「聞いた返事」にも載せない", (s.PROGRESS_TEXT in log, s.hermes.HERMES_HEARD["text"] == s.PROGRESS_TEXT,
                                                         any(s.PROGRESS_TEXT in str(r) for r in s.hermes.HERMES_TURNS)), (False, False, False))

    mode["delay"] = 0.0
    got = turn()
    check("早い返事: 一言は出さない", (("status", "確認しています…") in got, any(w == "WAV:確認しています" for k, w in got if k == "wav")), (False, False))
    check("早い返事: 返事は届く", ("reply", "わかりました。") in got, True)

    mode["delay"] = 0.6
    got = turn(mute=True)
    check("読み上げを切った画面: 状態の文だけ（声は送らない）", (("status", "確認しています…") in got, [w for k, w in got if k == "wav"]), (True, []))

    s.PROGRESS_AFTER = 0
    got = turn()
    check("0 にすると使わない", ("status", "確認しています…") in got, False)

    s.PROGRESS_AFTER = 0.2
    s._progress_wav.clear()
    mode["synth_fail"] = True
    got = turn()
    check("声が作れなくても、返事は届く", (("reply", "わかりました。") in got, [k for k, _ in got][-1]), (True, "done"))
    mode["synth_fail"] = False

    # 合成が遅い: 返事を遅らせず、返事のあとに一言を送らない
    s._progress_wav.clear()
    s.PROGRESS_AFTER, mode["delay"], mode["synth_delay"] = 0.05, 0.15, 0.8
    got = turn()
    at = next(v for k, v in got if k == "at")
    check("合成が遅い: 返事は合成を待たずに届く（1 秒より前）", at < 0.6, True)
    check("合成が遅い: 返事のあとに一言の声は届かない", any(k == "wav" and w == "WAV:確認しています" for k, w in got), False)
    check("合成が遅い: 終わりの合図は返事の声のあとの 1 回", ([k for k, _ in got].count("done"), got[-1][0]), (1, "done"))
    mode["synth_delay"] = 0.0

    # 一言の声のあと、往復が失敗（理由つき）・例外で終わる
    s._progress_wav.clear()
    s.PROGRESS_AFTER, mode["delay"] = 0.05, 0.3
    for fail in ("why", "raise"):
        mode["fail"] = fail
        got = turn()
        kinds = [k for k, _ in got]
        check(f"一言のあと失敗（{fail}）: 一言の声を送り、終わりの合図のあとにエラー", (("wav", "WAV:確認しています") in got, kinds[-2:]), (True, ["done", "error"]))
    mode["fail"] = None

    # 往復が期限より前に TimeoutError で終わる
    s.PROGRESS_AFTER, mode["delay"], mode["fail"] = 0.3, 0.01, "timeout"
    got = turn()
    check("往復が期限前に TimeoutError: 一言は出さずにエラー", (("status", "確認しています…") in got, [k for k, _ in got][-1]), (False, "error"))
    mode["fail"] = None

    # 合成の間に読み上げを切る
    s._progress_wav.clear()
    s.PROGRESS_AFTER, mode["delay"], mode["synth_delay"] = 0.05, 0.8, 0.5
    got = turn(mute_after=0.2)
    check("合成の間に読み上げを切った: 一言の声は送らない", any(k == "wav" and w == "WAV:確認しています" for k, w in got), False)
    mode["synth_delay"] = 0.0

    # 一言を送っている途中（合図の送り）で、止めて次の発言をする
    s._progress_wav.clear()
    real_send = s.Client.send
    hold = {"n": 0}
    sent_log = []  # 本体が画面に送った文の通知の種類（送った順）

    async def slow_send(self, **msg):  # 一言の合図（speaking）の最初の 1 回だけ、送りを 2.5 秒止める（次のターンが終わるより長く）
        if msg.get("type") == "speaking" and hold["n"] == 0:
            hold["n"] += 1
            await asyncio.sleep(2.5)
        sent_log.append(msg.get("type"))
        return await real_send(self, **msg)

    s.Client.send = slow_send
    s.PROGRESS_AFTER, mode["delay"] = 0.05, 1.0
    got = []
    try:
        with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
            with c.websocket_connect("ws://127.0.0.1:8790/ws") as ws:
                ws.send_text(json.dumps({"type": "text", "text": "一つ目", "pane": HUB, "speaker": "8"}))
                time.sleep(0.25)  # 一言の合図を送っている途中
                ws.send_text(json.dumps({"type": "stop"}))
                time.sleep(2.6)  # 止めただけ（次の発言はまだ）。止める前の一言の合図が、止めたあとに送られないか待つ
                mode["delay"] = 0.05
                ws.send_text(json.dumps({"type": "text", "text": "二つ目", "pane": HUB, "speaker": "8"}))
                for _ in range(40):
                    m = ws.receive()
                    if m.get("bytes") is not None:
                        got.append(("wav", m["bytes"].decode()))
                        continue
                    d = json.loads(m["text"])
                    got.append((d["type"], d.get("text", "")))
                    if d["type"] == "done":
                        break
                ws.send_text(json.dumps({"type": "clientlog", "msg": "end"}))
    finally:
        s.Client.send = real_send
    check("取り消したターンの一言の声は届かない（次のターンの返事の前にも後にも）", [w for k, w in got if k == "wav"], ["WAV:わかりました。"])
    stopped = sent_log.index("stopped")
    nxt = sent_log.index("status", stopped)  # 次のターンの「送りました」
    check("止めたターンの一言の合図（speaking）は、止めたあとに送られない", sent_log[stopped + 1:nxt].count("speaking"), 0)
    check("次のターンの返事は届く", ("reply", "わかりました。") in got, True)

    # 一言の状態の文の送りが遅れても、返事・失敗のあとに届かない（a2a f2bb8b69 の 3 回目）
    for fail in (None, "why"):
        real_send = s.Client.send
        log2 = []

        async def slow_status(self, **msg):
            if msg.get("type") == "status" and msg.get("text") == "確認しています…":
                await asyncio.sleep(0.4)
            log2.append((msg.get("type"), msg.get("text", "")))
            return await real_send(self, **msg)

        s.Client.send = slow_status
        s.PROGRESS_AFTER, mode["delay"], mode["fail"] = 0.05, 0.15, fail
        try:
            turn(linger=0.6)
        finally:
            s.Client.send = real_send
            mode["fail"] = None
        end = max(i for i, (k, _) in enumerate(log2) if k in ("done", "error"))
        check(f"状態の文の送りが遅れる（{fail or '返事'}）: 終わりのあとに「確認しています…」が届かない", ("status", "確認しています…") in log2[end + 1:], False)

    # 設定の読み方
    for raw, want in [("3", 3.0), ("1.5", 1.5), ("0", 0.0), ("-1", 0.0), ("abc", 3.0), ("", 3.0)]:
        os.environ["VL_PROGRESS_AFTER"] = raw
        src = (ROOT / "server.py").read_text()
        ns = {"os": os}
        start = src.index('_progress = os.environ.get("VL_PROGRESS_AFTER"')
        end = src.index("_progress_wav: dict")
        exec(src[start:end], ns)
        check(f"VL_PROGRESS_AFTER={raw!r} → {want}", ns["PROGRESS_AFTER"], want)
finally:
    os.environ.pop("VL_PROGRESS_AFTER", None)

bad = [r for r in results if not r[1]]
for name, ok, got, want in results:
    print(("ok  " if ok else "NG  ") + name + ("" if ok else f"  got={got!r} want={want!r}"))
print(f"\n{len(results) - len(bad)}/{len(results)} 通過")
sys.exit(1 if bad else 0)
