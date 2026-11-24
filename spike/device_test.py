"""機械（スタックチャン）の接続の試験。状態フォルダは一時のフォルダ。herdr・Hermes・声のエンジン・語り手は差し替える。常駐の 8790 には触れない。
確かめること（相手の位置: 機械を盗んだ人・乗っ取った機械・周りの声）:
  - 機械の口: VL_DEVICE_PORTS（置き場所=口）に書いた口で受けた接続は機械で、置き場所も口で決まる（受けた側のソケットの口で見る。
    X-Forwarded-For などの値では決めない）。形の合わない項目・画面の口と同じ口・重なりは使わない
  - 口: 機械の口からは WebSocket（/ws）だけ。画面のボタンの口（/api/answer など）・記憶・状況・画面そのものは使えない。コンテナからは機械の口につながれない
  - 名乗り: 口で機械と決まった接続は、hello で画面と名乗っても機械のままで、置き場所も変わらない。画面は kind: "stackchan" と名乗れば機械になる。機械は見えている画面に数えない
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
os.environ.pop("VL_WHISPER_LANG", None)
os.environ.pop("VL_PORT", None)
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HERMES_HOME=f"{TMP}/hermes", VL_HERMES_KEY="k1", VL_HERMES_URL="http://127.0.0.1:9",
                  VL_DEVICE_PORTS=" home=8797 , car=8798")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import server as s  # noqa: E402
import origin_guard as g  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


TS = "mac.example-tailnet.ts.net"
HOME, CAR = f"{TS}:8797", f"{TS}:8798"  # 機械の口（Tailscale Serve が出す口と、本体が 127.0.0.1 で待つ口を同じ番号にする）

# ---------- 機械の口の設定 ----------
check("機械の口: 置き場所=口を読む（前後の空白は除く）", g.device_ports(), {8797: "home", 8798: "car"})
for raw, want in [("", {}), ("HOME = 8797", {8797: "home"}), ("garage=8799", {}), ("home=80", {}), ("home=8790", {}), ("home=abc", {}),
                  ("home=123456", {}), ("home=8797,car=8797", {8797: "home"}), ("home=8797,home=8798", {8797: "home"}), ("home8797", {}),
                  ("desk=9000,car=8798", {9000: "desk", 8798: "car"}), ("home=-8797", {}), ("home=８７９７", {})]:
    os.environ["VL_DEVICE_PORTS"] = raw
    check(f"機械の口: {raw!r} → {want}", g.device_ports(), want)
os.environ["VL_DEVICE_PORTS"] = "home=8797,car=8798"
os.environ["VL_PORT"] = "8797"
check("機械の口: 画面の口（VL_PORT）と同じ口は使わない", g.device_ports(), {8798: "car"})
os.environ.pop("VL_PORT")
for scope, want in [({"server": ("127.0.0.1", 8797)}, "home"), ({"server": ("127.0.0.1", 8798)}, "car"), ({"server": ("127.0.0.1", 8790)}, ""),
                    ({}, ""), ({"server": None}, ""), ({"server": ("127.0.0.1",)}, ""), ({"server": ("127.0.0.1", "x")}, "")]:
    check(f"機械の口: 受けた口 {scope.get('server')} → {want!r}", g.device_place(scope), want)

# ---------- 口 ----------
h = {"host": TS}
check("口: 機械から /api/answer は断る", bool(g.problem(h, "POST", "/api/answer", device=True)), True)
check("口: 機械から /api/hub/answer は断る", bool(g.problem(h, "POST", "/api/hub/answer", device=True)), True)
check("口: 機械から画面（/）は断る", bool(g.problem(h, "GET", "/", device=True)), True)
check("口: 機械から /ws は通す", g.problem(h, "", "/ws", device=True), None)
check("口: 画面（機械でない）から /api/answer は、これまでどおり通す", g.problem(h, "POST", "/api/answer"), None)
hk = {"host": TS, "x-vl-key": "k1"}
check("口: コンテナから機械の口へは、hubctl の口（鍵つき）でも断る", bool(g.problem(hk, "GET", "/api/hub/tasks", container=True, device=True)), True)
check("口: コンテナから機械の口の /ws も断る", bool(g.problem(hk, "", "/ws", container=True, device=True)), True)
check("口: コンテナから画面の口の hubctl の口（鍵つき）は、これまでどおり通す", g.problem(hk, "GET", "/api/hub/tasks", container=True), None)

with TestClient(s.app, base_url=f"https://{TS}") as c:
    for port in (HOME, CAR):
        for method, path in [("GET", "/api/doctor"), ("POST", "/api/answer"), ("GET", "/api/hermes/memory"), ("GET", "/")]:
            r = c.request(method, f"https://{port}{path}", headers={"host": port}, json={} if method == "POST" else None)
            check(f"口（本物の流れ）: 機械の口 {port.split(':')[1]} で {method} {path} は 403", r.status_code, 403)
    r = c.get("/api/doctor", headers={"x-forwarded-for": "100.64.0.9", "host": TS})
    check("口（本物の流れ）: 画面の口は、中継の値に機械らしい住所があっても 403 でない（住所では決めない）", r.status_code != 403, True)

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
    with c.websocket_connect(f"wss://{HOME}/ws", headers={"host": HOME}) as w:
        cl = client_of(before)
        check("名乗り: 家の口で受けた接続は、名乗る前から機械・置き場所は家", (cl.kind, cl.place), ("device", "home"))
        w.send_text(json.dumps({"type": "hello", "kind": "screen", "visible": True, "place": "car", "audio": "opus"}))
        check("名乗り: 機械の口は、画面と名乗っても機械", settle(cl, "audio", "opus") and cl.kind, "device")
        check("名乗り: 機械は見えている画面に数えない", cl.visible, False)
        check("名乗り: 置き場所は口で決まり、名乗り（car）で変わらない", cl.place, "home")
    before = set(s.CLIENTS)
    with c.websocket_connect(f"wss://{CAR}/ws", headers={"host": CAR}) as w:
        cl = client_of(before)
        check("名乗り: 車の口で受けた接続は、置き場所が車", (cl.kind, cl.place), ("device", "car"))
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


async def fake_exchange(text, reply_at=None, extra="", item=None, said=None, via=""):
    seen_via.append((text, via))
    return "わかりました。", "", True

real_exchange = s.hermes.hermes_exchange
s.hermes.hermes_exchange = fake_exchange
try:
    with TestClient(s.app, base_url=f"https://{TS}") as c:
        for host, label in [(HOME, "機械の口"), (TS, "画面")]:
            with c.websocket_connect(f"wss://{host}/ws", headers={"host": host}) as w:
                w.send_text(json.dumps({"type": "hello", "visible": True}))
                w.send_text(json.dumps({"type": "text", "text": f"{label}から", "pane": s.hermes.HERMES_PANE, "speaker": "8"}))
                for _ in range(100):
                    if any(x[0] == f"{label}から" for x in seen_via):
                        break
                    time.sleep(0.02)
finally:
    s.hermes.hermes_exchange = real_exchange
check("通し: 機械の口からの発言は via=device で頭に渡る", dict(seen_via).get("機械の口から"), "device")
check("通し: 画面からの発言は via なし", dict(seen_via).get("画面から"), "")

# 添付の形が違う発言は、本体が送らずに断る。機械からなら、断った発言も機械の印で台帳に載る（a2a 356c8a73 の 3）
with TestClient(s.app, base_url=f"https://{TS}") as c:
    with c.websocket_connect(f"wss://{HOME}/ws", headers={"host": HOME}) as w:
        n0 = len(s.hermes.HERMES_TURNS)
        w.send_text(json.dumps({"type": "text", "text": "いいえ", "pane": s.hermes.HERMES_PANE, "speaker": "8", "attachments": ["../../etc/passwd"]}))
        for _ in range(200):
            if len(s.hermes.HERMES_TURNS) > n0:
                break
            time.sleep(0.02)
check("通し: 添付を断った機械の発言も、台帳では機械", s.hub_via(s.hub_human_turn()), "device")
s.hermes.HERMES_TURNS.clear()
s.hermes._load_hermes_turns()
check("通し: 起動し直しても、断った機械の発言は機械", s.hub_via(s.hub_human_turn()), "device")

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
BLOCKED = {"status": "blocked", "cwd": "/x/proj", "session": "s1", "project": "proj", "title": "t"}
s._worker = lambda pane: BLOCKED  # 承認・質問の画面で止まっているセッション
got, code = answer({"pane": "w0:p1", "answer": "yes"})
check("守り: 機械からの「はい」では承認に答えない", got.get("reason"), "device")
got, code = answer({"pane": "w0:p1", "answer": "2"})
check("守り: 機械からの番号でも答えない", got.get("reason"), "device")
# 止まっていないセッション（返事の中で文で聞いて入力を待っているだけ）には、機械からの発言でも「止まっていない」を返す。
# ハブはそれを見て、ユーザーの返事をふつうの指示（hubctl send）で送る（2026-11-24: 機械を先に返し、無い承認のボタンへ案内していた）
s._worker = lambda pane: {**BLOCKED, "status": "idle"}
got, code = answer({"pane": "w0:p1", "answer": "yes"})
check("守り: 止まっていないセッションには、機械からの発言でも「止まっていない」を返す（答えは送らない）", (got.get("ok"), got.get("reason")), (False, "not_blocked"))
HUBCTL = (ROOT / "hubctl").read_text(encoding="utf-8")
check("ハブへの案内: 止まっていないと断られたら、文の問いへの返事は hubctl send で送る（機械からでも送れる）",
      ("`hubctl send {a.pane}" in HUBCTL, "機械からの発言でも送れます" in HUBCTL), (True, True))
check("指示文のひな形: 文で聞いて待っているだけのセッションへの返事は answer でなく send",
      "`hubctl answer` を使わず、`hubctl send` でユーザーの言葉のまま送ります" in (ROOT / "templates/hermes/hub.md").read_text(encoding="utf-8"), True)
s._worker = lambda pane: None  # セッションは見つからない（守りの先に進んだかを、見つからないの答えで見る）
got, code = close({"pane": "w0:p1", "confirmed": True})
check("守り: 機械からの許可では閉じない", got.get("reason"), "device")

t_scr = hm.hermes_turn_begin("はい、許可して")
check("守り: 画面からの発言は機械でない", s.hub_via(t_scr), "")
got, code = answer({"pane": "w0:p1", "answer": "yes"})
check("守り: 画面からの発言は、いまの守りに進む（相手が見つからない）", code, 404)
got, code = close({"pane": "w0:p1", "confirmed": True})
check("守り: 画面からの閉じる許可も、いまの守りに進む", got.get("reason"), "gone")

# 入れ替わり: 入口で見た最後の発言は画面のもの。セッションを確かめている間に、機械からの「はい」が届く（a2a 356c8a73 の 2）
hm.hermes_turn_begin("ふつうの発言")
real_worker, real_trust, real_prompt = s._worker, s.pane_shows_trust, s.read_prompt
sent_keys = []


def worker_then_device(pane):
    hm.hermes_turn_begin("はい、許可して", via="device")  # 確かめの途中で、機械からの発言が台帳の最後に入る
    return {"status": "blocked", "cwd": "/x/proj", "session": "s1", "project": "proj", "title": "t"}

s._worker = worker_then_device
s.pane_shows_trust = lambda pane: False
s.read_prompt = lambda pane: "Do you want to proceed?\n❯ 1. Yes\n  2. No"
real_send = getattr(s, "send_keys", None)
try:
    got, code = answer({"pane": "w0:p1", "answer": "yes"})
finally:
    s._worker, s.pane_shows_trust, s.read_prompt = real_worker, real_trust, real_prompt
check("守り: 確かめの途中で機械の発言が入れ替わりで届いても、それを答えに使わない", got.get("reason"), "device")

# 入れ替わり（後ろの確かめ）: 入口の確かめ（止まっているかのあと）は画面の発言で通り、問いを読んでいる間に機械からの「はい」が届く。
# 答えに使う発言を取り直したあとの確かめで断る（並べ替えで、上の入れ替わりは入口の確かめで止まるようになったため。a2a acc49e43）
hm.hermes_turn_begin("ふつうの発言")


def prompt_then_device(pane):
    hm.hermes_turn_begin("はい、許可して", via="device")
    return "Do you want to proceed?\n❯ 1. Yes\n  2. No"


s._worker = lambda pane: BLOCKED
s.pane_shows_trust = lambda pane: False
s.read_prompt = prompt_then_device
try:
    got, code = answer({"pane": "w0:p1", "answer": "yes"})
finally:
    s._worker, s.pane_shows_trust, s.read_prompt = real_worker, real_trust, real_prompt
check("守り: 問いを読んでいる間に機械の発言が届いても、取り直した発言の確かめで断る", got.get("reason"), "device")

check("守り: 台帳に無い発言は機械として扱う", s.HUB.via("hzzzzzzzzzzzz"), "device")
check("守り: 発言が無ければ空", s.hub_via(None), "")

# 起動し直し: 記録から台帳を作り直しても、来た所が残る
hm.HERMES_TURNS.clear()
hm._load_hermes_turns()
check("守り: 起動し直しても、機械からの発言は機械", s.HUB.via(t_dev), "device")
check("守り: 起動し直しても、画面からの発言は画面", s.HUB.via(t_scr), "")
log = {d.get("turn"): d for d in (json.loads(x) for x in Path(hm.HERMES_LOG).read_text().splitlines() if x.strip()) if d.get("role") == "me"}
check("守り: 記録の発言に via が残る（機械だけ）", (log[t_dev].get("via"), log[t_scr].get("via")), ("device", None))

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
