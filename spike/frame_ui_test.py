"""画面をほかのページの枠に入れさせない守りの通し。本体の写しを 127.0.0.1:8791 に立て、本物のブラウザ（Chromium）で開く
（常駐の 8790・herdr・docker には触れない。声の一覧は、この試験の中の小さな偽物 127.0.0.1:8796 から取る）。
相手は、ブラウザで開いた別のサイトのページ（本体の画面を、自分のページの枠に入れられる）。
見ること: 別のサイトの枠に入れた画面（/ と /static/index.html）は、本体が 200 を返してもブラウザが表示しない。手元の写し（sw.js）に、
枠を断るヘッダーの無い前の版の画面が残っていて、本体の / が 503 を返す間も、枠の中では写しを出さない。いちばん外の画面として開けば
写しを出す（Mac に届かないときに画面を開ける働きは残す）。
使い方: uv run spike/frame_ui_test.py"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

TMP = Path(tempfile.mkdtemp()).resolve()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HUB="hermes", VL_HERMES_KEY="k1", VL_HERMES_URL="http://127.0.0.1:9",
                  VL_HERMES_HOME=f"{TMP}/hermes", VL_VOICE_URL="http://127.0.0.1:8796")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

import server as s  # noqa: E402

PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
OTHER = "http://localhost:8792"  # 別のサイトのページ（この試験の中の小さなサーバー。127.0.0.1 とは別のサイト。どちらも手元の住所なので、ブラウザは枠の要求を本体へ送る）
OLD = "前の版の画面の写し"
results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


class Speakers(BaseHTTPRequestHandler):
    def do_GET(self):
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


s.boot_ensure_hub = s.keep_gateway_token = s.watch_blocked = s.watch_delegation = s.intake_loop = s.hermes.watch_hermes_skills = nothing
s.list_agents = lambda: []
down = {"on": False}  # True の間、本体の / は 503（Mac の本体が止まっていて、中継だけが答える形）
framed_status = []  # 枠からの要求（Sec-Fetch-Dest: iframe）に、本体が返した状態


async def app(scope, receive, send):
    if down["on"] and scope["type"] == "http" and scope["path"] == "/":
        await send({"type": "http.response.start", "status": 503, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"down"})
        return
    in_frame = scope["type"] == "http" and (b"sec-fetch-dest", b"iframe") in scope["headers"]

    async def note(m):
        if in_frame and m["type"] == "http.response.start":
            framed_status.append(m["status"])
        await send(m)

    await s.app(scope, receive, note)


srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
th = threading.Thread(target=srv.run, daemon=True)
th.start()
for _ in range(200):
    if srv.started:
        break
    time.sleep(0.05)


class OtherSite(BaseHTTPRequestHandler):  # 別のサイトのページ: ?p= の場所を、本体から枠に入れる
    def do_GET(self):
        p = parse_qs(urlparse(self.path).query).get("p", ["/"])[0]
        body = f'<!doctype html><title>other</title><iframe src="{BASE}{p}"></iframe>'.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


threading.Thread(target=HTTPServer(("127.0.0.1", 8792), OtherSite).serve_forever, daemon=True).start()


def framed(page, path):
    """別のサイトのページに本体の path を枠で入れ、(本体の応答の状態, 枠に本物の画面が出たか, 枠に前の版の写しが出たか) を返す。"""
    framed_status.clear()
    page.goto(f"{OTHER}/?p={path}")  # 枠の読み込みが終わるまで待つ
    fr = next(f for f in page.frames if f != page.main_frame)
    try:
        real, old = fr.evaluate("[document.getElementById('conn') !== null, (document.body ? document.body.innerText : '').includes(%r)]" % OLD)
    except Exception:  # 断られた枠（ブラウザのエラーの画面）は読めないことがある
        real, old = False, False
    return framed_status[:], real, old


with sync_playwright() as pw:
    b = pw.chromium.launch()
    ctx = b.new_context()
    for path in ("/", "/static/index.html"):
        check(f"別のサイトの枠に入れた画面（{path}）は、本体が 200 を返しても、ブラウザが表示しない", framed(ctx.new_page(), path), ([200], False, False))

    top = ctx.new_page()
    top.goto(BASE + "/")
    top.wait_for_function("navigator.serviceWorker && navigator.serviceWorker.controller !== null", timeout=15000)  # sw.js が画面を受け持つまで
    top.evaluate("""async old => { const c = await caches.open('voice-loop-shell-v1');
      await c.put('/', new Response('<!doctype html><title>old</title><body>' + old + '</body>', { headers: { 'content-type': 'text/html; charset=utf-8' } })); }""", OLD)
    down["on"] = True
    try:
        check("手元の写しが枠を断るヘッダーの無い前の版で、本体の / が 503 の間も、別のサイトの枠の中では写しを出さない",
              framed(ctx.new_page(), "/")[1:], (False, False))
        again = ctx.new_page()
        again.goto(BASE + "/")
        check("同じとき、いちばん外の画面として開けば写しを出す（Mac に届かないときに画面を開ける働きは残す）",
              again.evaluate("document.body.innerText").strip(), OLD)
    finally:
        down["on"] = False
    b.close()

srv.should_exit = True
th.join(5)
ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
