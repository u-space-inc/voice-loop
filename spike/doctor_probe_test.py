"""点検（doctor）の「語り手」と、点検の結果をログに残すことの試験。本物の Anthropic・常駐の 8790 には触れない。
語り手の点検は、127.0.0.1:8791 に立てた偽の Anthropic に問い合わせる（決めた状態コードを返す）。
確かめること: Anthropic 側の障害（5xx）を、ログインの問題と取り違えずに伝えること。認証・モデル名・それ以外の失敗の文面は前のまま。
点検の結果は、使えなくなった・理由が変わった・使えるように戻ったときだけ、日付と時刻つきでログに 1 行残すこと（初めての「使える」・同じ結果の繰り返し・覚えている間は書かない）。
使い方: uv run spike/doctor_probe_test.py"""
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import anthropic  # noqa: E402
import server as s  # noqa: E402

PORT = 8791
results = []


def check(name, ok, got=None):
    results.append(ok)
    print(("✓ " if ok else "✗ ") + name + ("" if ok else f"（得た値: {got}）"))


# ---------- 偽の Anthropic（モデルの情報の取得に、決めた状態コードで答える） ----------
answer = {"status": 200}


class FakeAnthropic(BaseHTTPRequestHandler):
    def do_GET(self):
        code = answer["status"]
        body = ({"type": "model", "id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5", "created_at": "2025-10-01T00:00:00Z"} if code == 200
                else {"type": "error", "error": {"type": "api_error", "message": "test"}})
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("x-should-retry", "false")  # 5xx でも SDK に繰り返させない（試験を速くするため。本番の繰り返しは変えない）
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", PORT), FakeAnthropic)
threading.Thread(target=srv.serve_forever, daemon=True).start()
Real = anthropic.Anthropic
anthropic.Anthropic = lambda **kw: Real(api_key="test-key", base_url=f"http://127.0.0.1:{PORT}", **kw)  # 鍵とログインの情報は読まない


def narrator(code):
    answer["status"] = code
    return s._narrator_problem("claude-haiku-4-5")


# ---------- 語り手の点検の文面 ----------
for code in (500, 503, 529):
    got = narrator(code)
    check(f"{code} は Anthropic 側の一時的な障害と伝え、ログインし直しを勧めない",
          f"一時的な障害が起きています（{code}）" in got and "ログインし直す必要はありません" in got and "ant auth login" not in got, got)
got = narrator(200)
check("届けば、問題なし（空）", got == "", got)
got = narrator(401)
check("401 は前のまま「認証が通りません」", got.startswith("Anthropic の認証が通りません"), got)
got = narrator(404)
check("404 は前のまま「モデル名が見つかりません」", "が見つかりません" in got and "narrator_model" in got, got)
got = narrator(429)
check("429（5xx でない）は前のまま「確かめられませんでした（RateLimitError）」", got.startswith("Anthropic に確かめられませんでした（RateLimitError）"), got)
srv.shutdown()
srv.server_close()
got = narrator(200)
check("偽の Anthropic を止めたら「届きません」（前のまま）", got.startswith("Anthropic に届きません"), got)

# ---------- 点検の結果をログに残す ----------
state = {"problem": ""}
calls = []


def fake_check():
    calls.append(1)
    return state["problem"]


def probe(problem, keep=0.0):
    state["problem"] = problem
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        got = s._probe("fake", fake_check, keep_ok=keep, keep_ng=keep)
    return got, [l for l in out.getvalue().splitlines() if l.startswith("[doctor]")]


STAMP = r"  @\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"
got, lines = probe("")
check("初めての「使える」は書かない", got == "" and lines == [], lines)
got, lines = probe("Anthropic 側で一時的な障害が起きています（500）。\n次の行")
check("使えなくなったら、日付と時刻つきで 1 行書く（改行は空白にする）",
      len(lines) == 1 and re.fullmatch(r"\[doctor\] fake: 使えません — Anthropic 側で一時的な障害が起きています（500）。 次の行" + STAMP, lines[0]) is not None, lines)
got, lines = probe("Anthropic 側で一時的な障害が起きています（500）。\n次の行")
check("同じ結果の繰り返しは書かない", lines == [], lines)
got, lines = probe("Anthropic の認証が通りません。")
check("理由が変わったら書く", len(lines) == 1 and "fake: 使えません — Anthropic の認証が通りません。" in lines[0], lines)
got, lines = probe("")
check("使えるように戻ったら書く", got == "" and len(lines) == 1 and re.fullmatch(r"\[doctor\] fake: 使えるように戻りました" + STAMP, lines[0]) is not None, lines)
n = len(calls)
got, lines = probe("また壊れた", keep=60)
check("覚えている間は試さず、書かない", len(calls) == n and got == "" and lines == [], (len(calls) - n, got, lines))
s._probes.pop("fake")
state["problem"] = ""
got, lines = probe("ValueError: x")
check("覚えが無いところからの「使えません」も書く", len(lines) == 1 and "fake: 使えません — ValueError: x" in lines[0], lines)

print(f"\n{sum(results)}/{len(results)} 件が期待どおり")
sys.exit(0 if all(results) else 1)
