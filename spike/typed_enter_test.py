"""声の一覧が空の間は、文字の欄の Enter でも送らないことの通し。本体の写しを 127.0.0.1:8791 に立て、本物のブラウザ（Chromium）で開く
（常駐の 8790・herdr・docker には触れない。声の一覧は、この試験の中の小さな偽物 127.0.0.1:8796 から取り、止まっている間は 500 を返す）。
見ること: 音声エンジンが止まっていて声の一覧が空の間は「送る」を押せず、Enter を押しても送らず、打った文が欄に残り、本体との接続も切れない
（これまでは、Enter が「送る」の無効を確かめずに送り、欄を空にしたうえ、空の声を受けた本体が接続ごと落ちていた）。
エンジンが戻って一覧が届くと「送る」を押せるようになり、残っていた文を Enter で、選んだ相手と声で送り、欄は空になる。
文字の送信は画面の中で受け止めて数える。声が決まっている文は本体へ渡さない（本物の herdr に送らないため）。空の声の文は、本体がどう扱うかを見るため渡す
（本体は声の番号を読むところで止まり、セッションへは送らない）。
使い方: uv run spike/typed_enter_test.py"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

TMP = Path(tempfile.mkdtemp()).resolve()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_VOICE_URL="http://127.0.0.1:8796")
os.environ.pop("VL_HUB", None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

import server as s  # noqa: E402

PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
TEXT = "声の一覧が届く前に打った文"
results = []
voice = {"up": False}  # False の間、声の一覧は 500（音声エンジンが止まっている）


def check(name, got, want):
    results.append((name, got == want, got, want))


class Speakers(BaseHTTPRequestHandler):
    def do_GET(self):
        if not voice["up"]:
            self.send_response(500)
            self.end_headers()
            return
        body = json.dumps([{"name": "試しの声", "styles": [{"id": 1, "name": "ふつう"}]}]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


threading.Thread(target=HTTPServer(("127.0.0.1", 8796), Speakers).serve_forever, daemon=True).start()


async def nothing(*a, **k):
    return None


s.boot_ensure_hub = s.keep_gateway_token = s.watch_blocked = s.watch_delegation = s.hermes.watch_hermes_skills = nothing
s.list_agents = lambda: [{"pane": "w1:p1", "title": "記事", "cwd": "/x/blog", "kind": "claude", "session": "s1", "status": "idle", "name": "", "hub": False}]
s.jsonl_path = lambda a: None

srv = uvicorn.Server(uvicorn.Config(s.app, host="127.0.0.1", port=PORT, log_level="warning"))
th = threading.Thread(target=srv.run, daemon=True)
th.start()
for _ in range(200):
    if srv.started:
        break
    time.sleep(0.05)

# 画面が本体へ送る文字を数える（つなぎ直したあとの接続でも数えるよう、WebSocket の送り口ごと差し替える）
CATCH = """(() => { window.__sent = []; const o = WebSocket.prototype.send;
  WebSocket.prototype.send = function (m) {
    let d = null; try { d = typeof m === 'string' ? JSON.parse(m) : null; } catch {}
    if (d && d.type === 'text') { window.__sent.push([d.pane, d.text, d.speaker]); if (d.speaker !== '') return; }
    return o.call(this, m);
  }; })()"""


def wait(fn, limit=8.0):
    end = time.time() + limit
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


with sync_playwright() as pw:
    b = pw.chromium.launch()
    page = b.new_context(viewport={"width": 1400, "height": 900}).new_page()
    page.add_init_script(CATCH)
    page.goto(BASE + "/")
    page.wait_for_function("document.getElementById('conn').classList.contains('on')")
    page.select_option("#agent", "w1:p1")
    sent = lambda: page.evaluate("window.__sent")
    linked = lambda: page.evaluate("document.getElementById('conn').classList.contains('on')")
    check("試験の前提: マウスで使う端末として開いている（Enter で送る形）", page.evaluate("matchMedia('(pointer: coarse)').matches"), False)

    check("声の一覧が空の間は「送る」を押せない（文字の欄には打てる）",
          (page.evaluate("document.getElementById('speaker').options.length"), page.locator("#send").is_disabled(), page.locator("#typed").is_disabled()), (0, True, False))
    page.fill("#typed", TEXT)
    page.press("#typed", "Enter")
    wait(lambda: sent() or not linked(), 1.5)
    check("声の一覧が空の間は、Enter を押しても送らず、打った文は欄に残る", (sent(), page.input_value("#typed")), ([], TEXT))
    check("そのあいだ、本体との接続は切れない", wait(lambda: not linked(), 1.5), False)

    voice["up"] = True  # エンジンが戻る（画面は 3 秒ごとに一覧を読み直している）
    check("一覧が届くと「送る」を押せるようになり、打った文は残っている",
          (wait(lambda: not page.locator("#send").is_disabled(), 10), page.input_value("#typed")), (True, TEXT))
    page.evaluate("window.__sent = []")
    page.press("#typed", "Enter")
    wait(lambda: sent(), 3)
    check("一覧が届いたあとは、今までどおり Enter で、選んだ相手と声で送り、欄は空になる", (sent(), page.input_value("#typed")), ([["w1:p1", TEXT, "1"]], ""))
    b.close()

srv.should_exit = True
th.join(5)
ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
