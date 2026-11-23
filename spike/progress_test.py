"""ハブの返事を待つ間の本体の一言（「確認しています」。config.env の VL_PROGRESS_AFTER）の試験。Hermes との往復・声の合成・語り手は差し替える。
常駐の 8790・本物の Hermes・声のエンジンには触れない。
確かめること:
  - 返事が PROGRESS_AFTER 秒より遅いと、状態の文「確認しています…」と、一言の声を 1 つ送る。そのあとに返事の文と声が続く（一言のあとに終わりの合図は送らない）
  - 返事が早ければ、一言は出さない
  - 読み上げを切った画面（🔇）には、状態の文だけ（声は送らない）
  - 0 にすると使わない
  - 一言は記録にも、守りの台帳（利用者が聞いた返事）にも載せない。Hermes への頼みの中身も変えない
  - 声が作れなくても、返事は届く
使い方: uv run spike/progress_test.py"""
import asyncio
import json
import os
import sys
import tempfile
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
mode = {"delay": 0.0, "synth_fail": False}
asked = []


async def fake_accepted(talker, text, pane, *a, **k):  # Hermes との往復の代わり。返事までの時間だけを決める
    asked.append(text)
    await asyncio.sleep(mode["delay"])
    return "わかりました。", "", True


def fake_synth(text, speaker):
    if mode["synth_fail"] and text == s.PROGRESS_TEXT:
        raise OSError("声のエンジンが止まっています")
    return f"WAV:{text}".encode()


async def fake_narrate(text, cancel):
    yield text


s.hub_accepted = fake_accepted
s.synth = fake_synth
s.narrate = fake_narrate


def turn(text="ねえ、いまどうなってる？", mute=False):
    """ハブに打った文を送り、終わり（done か error）までに届いたものを順に返す。文の通知は (種類, 文)、声は ("wav", 中身)。"""
    got = []
    with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
        with c.websocket_connect("ws://127.0.0.1:8790/ws") as ws:
            if mute:
                ws.send_text(json.dumps({"type": "hello", "voice": False}))
            ws.send_text(json.dumps({"type": "text", "text": text, "pane": HUB, "speaker": "8"}))
            for _ in range(40):
                m = ws.receive()
                if m.get("bytes") is not None:
                    got.append(("wav", m["bytes"].decode()))
                    continue
                d = json.loads(m["text"])
                got.append((d["type"], d.get("text", "")))
                if d["type"] in ("done", "error"):
                    break
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
