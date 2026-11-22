"""聞き取り（whisper）への呼び出しの試験。whisper は差し替え、要求の中身だけを見る。常駐の 8790・本物の whisper には触れない。
確かめること:
  - ファイル名は本体が付ける。送られてきた名前は拡張子だけを見る（webm・mp4・m4a・ogg・opus・wav。ほかと拡張子の無いものは webm）
  - 画面や機械が、名前に " や改行を入れて送っても、whisper への要求の見出しは崩れない（相手の位置: tailnet の中の WebSocket の turn）
  - 言語は毎回渡す（既定は ja）。空にすると渡さない
  - 声の区間だけを聞き取らせる（vad=true。既定は on）。off にすると渡さない
  - 診断: whisper が vad=true にだけ答えない（VAD のモデルを読み込んでいないことが多い）ときは、そう知らせる。vad を黙って無視する whisper
    （声の入っていない雑音を文字にする）にも気づく。いつも 500・つながらないときは VAD のせいにしない
  - 聞き取りに失敗しても（whisper が止まっている・500・返事の形が違う）、WebSocket は切らずに「聞き取りに失敗しました」と返し、次の録音を受ける
使い方: uv run spike/whisper_call_test.py"""
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.pop("VL_WHISPER_LANG", None)  # 外の設定を引き継がない（既定の ja を確かめる）
os.environ.pop("VL_WHISPER_VAD", None)  # 同じく既定の on を確かめる
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
                    ('x"; name="model\r\n\r\nevil\r\n--b.webm', "audio.webm"), ("a.webm\r\nX-Evil: 1", "audio.webm"),
                    ("rec.ｗａｖ", "audio.webm"), ("録音.ＷＥＢＭ", "audio.webm"), ("a." + "w" * 5000, "audio.webm"), ("x" * 5000 + ".wav", "audio.wav"),
                    (["a.wav"], "audio.webm"), ({"name": "a.wav"}, "audio.webm"), (".wav", "audio.wav"), ("a.", "audio.webm")]:
    check(f"名前 {given!r}", s.audio_name(given), want)

# ---------- 要求の中身 ----------
seen = []


class Resp:
    def __init__(self, body): self.body = body
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self.body


mode = {"fail": None}  # None: 空の聞き取りを返す / "vad": vad=true のときだけ 500 / "all": いつも 500 / "down": つながらない
# "ignore": vad を読まない版（雑音を文字にする）/ "list": 返事が [] / "null": 文が null / "noise_down": 雑音の確かめだけつながらない
# "missing": 返事に text が無い（{"error": …}）/ "noise_missing": 雑音の確かめだけ text が無い


def fake_urlopen(req, timeout=None):
    seen.append(req.data)
    fail = mode["fail"]
    if fail == "down":
        raise s.urllib.error.URLError("connection refused")
    if fail == "all" or (fail == "vad" and b'name="vad"' in req.data):
        raise s.urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", {}, None)
    noise = len(req.data) > 20000  # 点検の雑音（2 秒）。無音の点検（0.3 秒）と録音の作り物は小さい
    if fail == "noise_down" and noise:
        raise s.urllib.error.URLError("connection reset")
    if fail == "ignore" and noise:
        return Resp(json.dumps({"text": "コミット\n"}).encode())
    if fail == "list":
        return Resp(b"[]")
    if fail == "missing" or (fail == "noise_missing" and noise):
        return Resp(json.dumps({"error": "failed to process audio"}).encode())
    if fail == "null":
        return Resp(json.dumps({"text": None}).encode())
    return Resp(json.dumps({"text": ""}).encode())


VAD = b'name="vad"\r\n\r\ntrue\r\n'


real = s.urllib.request.urlopen
s.urllib.request.urlopen = fake_urlopen
try:
    s._whisper(b"AUDIO", 'evil"\r\nContent-Type: text/plain\r\n\r\nX.webm')
    body = seen[-1]
    check("言語を渡す（既定 ja）", b'name="language"\r\n\r\nja\r\n' in body, True)
    check("名前は本体が付けた audio.webm", b'filename="audio.webm"' in body, True)
    check("送られた名前の断片は要求に入らない", b"evil" in body or b"text/plain" in body, False)
    check("声の区間だけを聞き取らせる（既定 vad=true）", VAD in body, True)
    check("既定の WHISPER_VAD は on", s.WHISPER_VAD, True)
    check("multipart の区切りは 4 つ（model・language・vad・file）と終わり", body.count(b"\r\n--" + body.split(b"\r\n", 1)[0][2:]) + 1, 5)
    s._whisper(b"AUDIO", "rec.wav", vad=False)
    check("vad=False を指定すると渡さない", b'name="vad"' in seen[-1], False)
    s.WHISPER_VAD = False
    s._whisper(b"AUDIO", "rec.wav")
    check("off にすると vad を渡さない", b'name="vad"' in seen[-1], False)
    s.WHISPER_VAD = True

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
    if seen:
        check("WebSocket の turn: 声の区間だけを聞き取らせる", VAD in seen[0], True)

    # 聞き取りに失敗しても接続は切らない。続けて送った録音も聞き取りに回る
    for fail in ("all", "down", "list", "null", "missing"):
        seen.clear()
        errors = []
        try:
            with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
                with c.websocket_connect("ws://127.0.0.1:8790/ws") as ws:
                    for round_fail in (fail, None):
                        mode["fail"] = round_fail
                        ws.send_text(json.dumps({"type": "turn", "pane": "w0:p1", "speaker": "8", "filename": "a.webm"}))
                        ws.send_bytes(b"AUDIO")
                        for _ in range(20):
                            m = ws.receive_json()
                            if m.get("type") == "error":
                                errors.append(m.get("text", ""))
                                break
        except Exception as e:  # 本体の受け口が例外で落ちた（＝接続が切れた）
            errors.append(f"例外 {type(e).__name__}")
        mode["fail"] = None
        check(f"失敗（{fail}）: 「聞き取りに失敗しました」と返す", bool(errors) and errors[0].startswith("聞き取りに失敗しました"), True)
        check(f"失敗（{fail}）: 接続は切れず、次の録音も聞き取りに回る", (len(seen), errors[1:]), (2, ["聞き取れませんでした"]))

    # ---------- 診断 ----------
    mode["fail"] = None
    check("診断: 答えれば問題なし", s._whisper_problem(), "")
    check("診断: 雑音の確かめも送る（vad=true つき）", any(len(b) > 20000 and VAD in b for b in seen), True)
    mode["fail"] = "vad"
    msg = s._whisper_problem()
    check("診断: vad=true だけ答えないなら、VAD のモデルを確かめるよう知らせる", "VAD" in msg and "-vm" in msg and "VL_WHISPER_VAD=off" in msg, True)
    mode["fail"] = "ignore"
    msg = s._whisper_problem()
    check("診断: vad を黙って無視する whisper（雑音を文字にする）に気づく", "効いていません" in msg and "コミット" in msg, True)
    mode["fail"] = "noise_down"
    check("診断: 雑音の確かめが失敗したら、問題なしとは言わない", "雑音を渡す確かめに失敗" in s._whisper_problem(), True)
    mode["fail"] = "noise_missing"
    check("診断: 雑音の確かめの返事に text が無ければ、問題なしとは言わない", "雑音を渡す確かめに失敗" in s._whisper_problem(), True)
    for fail in ("list", "null", "missing"):
        mode["fail"] = fail
        check(f"診断: 返事の形が違う（{fail}）なら、whisper の返事ではないと言う", "whisper の返事ではありません" in s._whisper_problem(), True)
    s.WHISPER_VAD = False
    mode["fail"] = "vad"
    check("診断: off なら vad を付けずに確かめる（問題なし）", s._whisper_problem(), "")
    mode["fail"] = "ignore"
    check("診断: off なら雑音の確かめはしない", s._whisper_problem(), "")
    s.WHISPER_VAD = True
    mode["fail"] = "all"
    msg = s._whisper_problem()
    check("診断: いつも 500 なら、whisper の返事ではないと言う（VAD のせいにしない）", "whisper の返事ではありません" in msg and "VAD" not in msg, True)
    mode["fail"] = "down"
    check("診断: つながらなければ、動いていないと言う", "動いていません" in s._whisper_problem(), True)
    mode["fail"] = None
finally:
    s.urllib.request.urlopen = real

bad = [r for r in results if not r[1]]
for name, ok, got, want in results:
    print(("ok  " if ok else "NG  ") + name + ("" if ok else f"  got={got!r} want={want!r}"))
print(f"\n{len(results) - len(bad)}/{len(results)} 通過")
sys.exit(1 if bad else 0)
