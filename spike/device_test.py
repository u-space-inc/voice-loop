"""機械（スタックチャン）の接続の試験。状態フォルダは一時のフォルダ。herdr・Hermes・声のエンジン・語り手は差し替える。常駐の 8790 には触れない。
確かめること（相手の位置: 機械を盗んだ人・乗っ取った機械・周りの声）:
  - 住所: VL_DEVICE_ADDRS に書いた tailnet の住所からの接続（中継が X-Forwarded-For の最後に付けた値）は機械。前に値を足しても、最後の値で決まる
  - 口: 機械の住所からは WebSocket（/ws）だけ。画面のボタンの口（/api/answer など）・記憶・状況・画面そのものは使えない
  - 名乗り: 住所で機械と決まった接続は、hello で画面と名乗っても機械のまま。画面は kind: "stackchan" と名乗れば機械になる。機械は見えている画面に数えない
  - 守り: 最後の発言が機械から届いたときは、ハブは承認・選択肢に答えられず、セッションを閉じられない（画面からの発言なら、いまの守りに進む）。
    台帳に無い発言は機械として扱う（断る側）。来た所は記録に残り、本体を起動し直しても台帳に戻る
  - 声: audio: "opus" を名乗った接続には Ogg の Opus、ほかには WAV。縮められないときは WAV のまま
使い方: uv run spike/device_test.py"""
import asyncio
import io
import json
import math
import os
import struct
import sys
import tempfile
import time
import wave
from pathlib import Path

TMP = tempfile.mkdtemp()
DEV = "100.64.0.9"
os.environ.pop("VL_WHISPER_LANG", None)
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HERMES_KEY="k1", VL_HERMES_URL="http://127.0.0.1:9",
                  VL_DEVICE_ADDRS=f" {DEV} , 100.64.0.10")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import server as s  # noqa: E402
import origin_guard as g  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


TS = "mac.example-tailnet.ts.net"

# ---------- 住所 ----------
check("住所: 最後が機械の住所", g.from_device({"x-forwarded-for": DEV}), True)
check("住所: 前に値を足しても最後で決まる", g.from_device({"x-forwarded-for": f"1.2.3.4, {DEV}"}), True)
check("住所: 機械の住所を前に足しただけでは機械にならない", g.from_device({"x-forwarded-for": f"{DEV}, 100.70.0.1"}), False)
check("住所: 中継の値が無ければ機械でない", g.from_device({}), False)
check("住所: 2 つ目の住所も機械", g.from_device({"x-forwarded-for": "100.64.0.10"}), True)
os.environ["VL_DEVICE_ADDRS"] = ""
check("住所: 設定が空なら誰も機械でない", g.from_device({"x-forwarded-for": DEV}), False)
os.environ["VL_DEVICE_ADDRS"] = f" {DEV} , 100.64.0.10"

# ---------- 口 ----------
h = {"host": TS}
check("口: 機械から /api/answer は断る", bool(g.problem(h, "POST", "/api/answer", device=True)), True)
check("口: 機械から /api/hub/answer は断る", bool(g.problem(h, "POST", "/api/hub/answer", device=True)), True)
check("口: 機械から画面（/）は断る", bool(g.problem(h, "GET", "/", device=True)), True)
check("口: 機械から /ws は通す", g.problem(h, "", "/ws", device=True), None)
check("口: 画面（機械でない）から /api/answer は、これまでどおり通す", g.problem(h, "POST", "/api/answer"), None)

with TestClient(s.app, base_url=f"https://{TS}") as c:
    for method, path in [("GET", "/api/doctor"), ("POST", "/api/answer"), ("GET", "/api/hermes/memory"), ("GET", "/")]:
        r = c.request(method, path, headers={"x-forwarded-for": DEV, "host": TS}, json={} if method == "POST" else None)
        check(f"口（本物の流れ）: 機械の住所から {method} {path} は 403", r.status_code, 403)
    r = c.get("/api/doctor", headers={"x-forwarded-for": "100.70.0.1", "host": TS})
    check("口（本物の流れ）: ほかの tailnet の端末からは 403 でない", r.status_code != 403, True)

    # ---------- 名乗り ----------
    def client_of(before):
        for _ in range(100):
            new = [x for x in s.CLIENTS if x not in before]
            if new:
                return new[0]
            time.sleep(0.02)

    def settle(cl, attr, want):
        for _ in range(100):
            if getattr(cl, attr) == want:
                return want
            time.sleep(0.02)
        return getattr(cl, attr)

    before = set(s.CLIENTS)
    with c.websocket_connect(f"wss://{TS}/ws", headers={"x-forwarded-for": DEV, "host": TS}) as w:
        cl = client_of(before)
        w.send_text(json.dumps({"type": "hello", "kind": "screen", "visible": True, "place": "car", "audio": "opus"}))
        check("名乗り: 機械の住所は、画面と名乗っても機械", settle(cl, "audio", "opus") and cl.kind, "device")
        check("名乗り: 機械は見えている画面に数えない", cl.visible, False)
        check("名乗り: 置き場所を受け取る", cl.place, "car")
    before = set(s.CLIENTS)
    with c.websocket_connect(f"wss://{TS}/ws", headers={"host": TS}) as w:
        cl = client_of(before)
        w.send_text(json.dumps({"type": "hello", "visible": True}))
        check("名乗り: 画面はこれまでどおり（見えている）", settle(cl, "visible", True) and cl.kind, "screen")
        w.send_text(json.dumps({"type": "hello", "kind": "stackchan", "visible": True, "place": "garage"}))
        check("名乗り: 画面も stackchan と名乗れば機械", settle(cl, "kind", "device"), "device")
        check("名乗り: 機械になったら見えている画面に数えない", settle(cl, "visible", False), False)
        check("名乗り: 知らない置き場所は空", cl.place, "")

# ---------- 通し: 機械の WebSocket の発言は、機械の印つきで頭に渡る ----------
seen_via = []


async def fake_exchange(text, reply_at=None, extra="", item=None, via=""):
    seen_via.append((text, via))
    return "わかりました。", "", True

real_exchange = s.hermes.hermes_exchange
s.hermes.hermes_exchange = fake_exchange
try:
    with TestClient(s.app, base_url=f"https://{TS}") as c:
        for hdr, label in [({"x-forwarded-for": DEV, "host": TS}, "機械の住所"), ({"host": TS}, "画面")]:
            with c.websocket_connect(f"wss://{TS}/ws", headers=hdr) as w:
                w.send_text(json.dumps({"type": "hello", "visible": True}))
                w.send_text(json.dumps({"type": "text", "text": f"{label}から", "pane": s.hermes.HERMES_PANE, "speaker": "8"}))
                for _ in range(100):
                    if any(x[0] == f"{label}から" for x in seen_via):
                        break
                    time.sleep(0.02)
finally:
    s.hermes.hermes_exchange = real_exchange
check("通し: 機械の住所からの発言は via=device で頭に渡る", dict(seen_via).get("機械の住所から"), "device")
check("通し: 画面からの発言は via なし", dict(seen_via).get("画面から"), "")

# ---------- 守り ----------
s._worker = lambda pane: None  # セッションは見つからない（守りの先に進んだかを、見つからないの答えで見る）
s.REPORTING = False
hm = s.hermes


def answer(body):
    r = asyncio.run(s._hub_answer(body))
    return json.loads(r.body), r.status_code


def close(body):
    r = asyncio.run(s.hub_close(body))
    return json.loads(r.body), r.status_code


t_dev = hm.hermes_turn_begin("はい、許可して", via="device")
check("守り: 台帳は機械からの発言を覚える", s.hub_via(t_dev), "device")
got, code = answer({"pane": "w0:p1", "answer": "yes"})
check("守り: 機械からの「はい」では承認に答えない", got.get("reason"), "device")
got, code = answer({"pane": "w0:p1", "answer": "2"})
check("守り: 機械からの番号でも答えない", got.get("reason"), "device")
got, code = close({"pane": "w0:p1", "confirmed": True})
check("守り: 機械からの許可では閉じない", got.get("reason"), "device")

t_scr = hm.hermes_turn_begin("はい、許可して")
check("守り: 画面からの発言は機械でない", s.hub_via(t_scr), "")
got, code = answer({"pane": "w0:p1", "answer": "yes"})
check("守り: 画面からの発言は、いまの守りに進む（相手が見つからない）", code, 404)
got, code = close({"pane": "w0:p1", "confirmed": True})
check("守り: 画面からの閉じる許可も、いまの守りに進む", got.get("reason"), "gone")

check("守り: 台帳に無い発言は機械として扱う", s.HUB.via("hzzzzzzzzzzzz"), "device")
check("守り: 発言が無ければ空", s.hub_via(None), "")

# 起動し直し: 記録から台帳を作り直しても、来た所が残る
hm.HERMES_TURNS.clear()
hm._load_hermes_turns()
check("守り: 起動し直しても、機械からの発言は機械", s.HUB.via(t_dev), "device")
check("守り: 起動し直しても、画面からの発言は画面", s.HUB.via(t_scr), "")
check("守り: 起動し直したあとの最後の発言は画面のもの", s.hub_human_turn(), t_scr)
log = [json.loads(x) for x in Path(hm.HERMES_LOG).read_text().splitlines() if x.strip()]
check("守り: 記録の発言に via が残る（機械だけ）", [d.get("via") for d in log if d.get("role") == "me"][-2:], ["device", None])

# ---------- 声 ----------
def tone_wav(sec=0.4, rate=24000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(int(sec * rate))))
    return buf.getvalue()


WAV = tone_wav()
op = s.to_opus(WAV)
check("声: WAV を Ogg の Opus にできる", bool(op) and op[:4], b"OggS")
check("声: Opus は WAV より小さい", bool(op) and len(op) < len(WAV) // 4, True)


class FakeSock:
    def __init__(self):
        self.headers = {}
        self.got = []
        self.texts = []

    async def send_bytes(self, b):
        self.got.append(b)

    async def send_text(self, t):
        self.texts.append(json.loads(t))


async def one_sentence(text, cancel):
    yield "テストの文です。"

s.narrate = one_sentence
s.synth = lambda text, sp: WAV


def speak(ffmpeg_ok=True):
    real = s.ffmpeg_path
    if not ffmpeg_ok:
        s.ffmpeg_path = lambda: None
    try:
        a, b = s.Client(FakeSock()), s.Client(FakeSock())
        b.audio = "opus"
        asyncio.run(s.speak_to([a, b], "テスト", asyncio.Event()))
        return a.sock.got, b.sock.got
    finally:
        s.ffmpeg_path = real


ga, gb = speak()
check("声: 画面には WAV", [x[:4] for x in ga], [b"RIFF"])
check("声: Opus を望んだ接続には Opus", [x[:4] for x in gb], [b"OggS"])
ga, gb = speak(ffmpeg_ok=False)
check("声: 縮められないときは WAV のまま", [x[:4] for x in gb], [b"RIFF"])

bad = [r for r in results if not r[1]]
for name, ok, got, want in results:
    print(("ok  " if ok else "NG  ") + name + ("" if ok else f"  got={got!r} want={want!r}"))
print(f"\n{len(results) - len(bad)}/{len(results)} 通過")
sys.exit(1 if bad else 0)
