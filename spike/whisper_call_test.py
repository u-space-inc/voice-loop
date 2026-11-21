"""聞き取り（whisper）への呼び出しの試験。whisper は差し替え、要求の中身だけを見る。常駐の 8790・本物の whisper には触れない。
確かめること:
  - ファイル名は本体が付ける。送られてきた名前は拡張子だけを見る（webm・mp4・m4a・ogg・opus・wav。ほかと拡張子の無いものは webm）
  - 画面や機械が、名前に " や改行を入れて送っても、whisper への要求の見出しは崩れない（相手の位置: tailnet の中の WebSocket の turn）
  - 言語は毎回渡す（既定は ja）。空にすると渡さない
使い方: uv run spike/whisper_call_test.py"""
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HERMES_KEY="k1", VL_HERMES_URL="http://127.0.0.1:9")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import server as s  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


# ---------- 名前 ----------
for given, want in [("rec.wav", "audio.wav"), ("audio.webm", "audio.webm"), ("x.MP4", "audio.mp4"), ("a.m4a", "audio.m4a"),
                    ("rec.ogg", "audio.ogg"), ("rec.opus", "audio.opus"), ("../../etc/passwd.wav", "audio.wav"),
                    ("noext", "audio.webm"), ("run.exe", "audio.webm"), ("", "audio.webm"), (None, "audio.webm"), (123, "audio.webm"),
                    ('x"; name="model\r\n\r\nevil\r\n--b.webm', "audio.webm"), ("a.webm\r\nX-Evil: 1", "audio.webm")]:
    check(f"名前 {given!r}", s.audio_name(given), want)

# ---------- 要求の中身 ----------
seen = []


class Resp:
    def __init__(self, body): self.body = body
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self.body


def fake_urlopen(req, timeout=None):
    seen.append(req.data)
    return Resp(json.dumps({"text": ""}).encode())


real = s.urllib.request.urlopen
s.urllib.request.urlopen = fake_urlopen
try:
    s._whisper(b"AUDIO", 'evil"\r\nContent-Type: text/plain\r\n\r\nX.webm')
    body = seen[-1]
    check("言語を渡す（既定 ja）", b'name="language"\r\n\r\nja\r\n' in body, True)
    check("名前は本体が付けた audio.webm", b'filename="audio.webm"' in body, True)
    check("送られた名前の断片は要求に入らない", b"evil" in body or b"text/plain" in body, False)
    check("multipart の区切りは 3 つ（model・language・file）と終わり", body.count(b"\r\n--" + body.split(b"\r\n", 1)[0][2:]) + 1, 4)

    s.WHISPER_LANG = ""
    s._whisper(b"AUDIO", "rec.ogg")
    body = seen[-1]
    check("空にすると言語を渡さない", b'name="language"' in body, False)
    check("ogg はそのまま", b'filename="audio.ogg"' in body, True)
    s.WHISPER_LANG = "ja"

    # 相手の位置: tailnet の中の画面・機械が、WebSocket の turn に細工した名前を付けて送る
    seen.clear()
    with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
        with c.websocket_connect("ws://127.0.0.1:8790/ws") as ws:  # Origin なし（機械や hubctl と同じ）
            ws.send_text(json.dumps({"type": "turn", "pane": "w0:p1", "speaker": "8", "filename": 'a"\r\nX-Evil: 1\r\n.wav'}))
            ws.send_bytes(b"RIFFAUDIO")
            got = []
            for _ in range(20):
                m = ws.receive_json()
                got.append(m.get("type"))
                if m.get("type") == "error":
                    break
    check("WebSocket の turn は whisper に届いた", len(seen), 1)
    if seen:
        check("WebSocket の turn: 名前は audio.wav", b'filename="audio.wav"' in seen[0], True)
        check("WebSocket の turn: 細工した見出しは入らない", b"X-Evil" in seen[0], False)
        check("WebSocket の turn: 言語を渡す", b'name="language"\r\n\r\nja\r\n' in seen[0], True)
    check("空の聞き取りは「聞き取れませんでした」", "error" in got, True)
finally:
    s.urllib.request.urlopen = real

bad = [r for r in results if not r[1]]
for name, ok, got, want in results:
    print(("ok  " if ok else "NG  ") + name + ("" if ok else f"  got={got!r} want={want!r}"))
print(f"\n{len(results) - len(bad)}/{len(results)} 通過")
sys.exit(1 if bad else 0)
