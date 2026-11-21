"""顔の表示（/?face。スマホを仮のスタックチャンにする）の通し。本体の写しを 127.0.0.1:8791 に立て、本物のブラウザ（Chromium）で開く
（常駐の 8790・herdr・docker には触れない。声の一覧は、この試験の中の小さな偽物 127.0.0.1:8796 から取る）。
見ること:
  - ふだんの画面（/）には顔が出ない。名乗りに kind は付かない
  - /?face では顔が画面いっぱいに出て、つながると「ふつう」の表情。名乗りに kind: "stackchan"・place: "home" が付く
  - 顔をタップすると「タップして話す」が押される。「画面に戻る」を押したときは押されず、ふだんの画面に戻る
  - 承認・入力待ちがあると縁が光り、無くなると消える
  - 状態の文が顔の下に出る
使い方: uv run spike/face_ui_test.py"""
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

# 画面が送る hello を拾う
CATCH = """(() => { window.__hello = []; const o = WebSocket.prototype.send;
  WebSocket.prototype.send = function (m) {
    let d = null; try { d = typeof m === 'string' ? JSON.parse(m) : null; } catch {}
    if (d && d.type === 'hello') window.__hello.push(d);
    return o.call(this, m);
  }; })()"""

with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 390, "height": 844})
    pg.add_init_script(CATCH)
    asks = {"list": []}
    pg.route("**/api/prompts", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(asks["list"])))

    pg.goto(BASE + "/")
    pg.wait_for_function("window.__hello.length > 0", timeout=10000)
    check("ふだん: 顔は出ない", pg.locator("#face").is_visible(), False)
    check("ふだん: 名乗りに kind は付かない", pg.evaluate("window.__hello[0].kind ?? null"), None)

    pg.goto(BASE + "/?face")
    pg.wait_for_function("window.__hello.length > 0", timeout=10000)
    check("顔: 画面いっぱいに出る", pg.evaluate("(() => { const r = document.getElementById('face').getBoundingClientRect(); return [r.width >= 380, r.height >= 800]; })()"), [True, True])
    pg.wait_for_function("document.getElementById('face').dataset.s === 'idle'", timeout=10000)
    check("顔: つながると「ふつう」の表情", pg.evaluate("document.getElementById('face').dataset.s"), "idle")
    h = pg.evaluate("window.__hello[0]")
    check("顔: 名乗りに stackchan と home が付く", (h.get("kind"), h.get("place")), ("stackchan", "home"))
    check("顔: 状態の文が顔の下に出る", pg.evaluate("document.getElementById('faceCap').textContent"), pg.evaluate("document.getElementById('statusText').textContent"))

    pg.evaluate("window.__talk = 0; document.getElementById('talk').addEventListener('click', () => { window.__talk++; }, true)")
    if pg.evaluate("document.getElementById('talk').disabled"):  # 話し相手が決まる前（この試験にはハブがいない）
        pg.locator("#face svg").click()
        check("顔: 話せないときにタップすると、押さずに理由を出す", (pg.evaluate("window.__talk"), pg.evaluate("document.getElementById('faceCap').textContent").startswith("いまは話せません")), (0, True))
    pg.wait_for_function("!!document.querySelector('#agent option[value=\"w1:p1\"]')", timeout=10000)
    pg.select_option("#agent", "w1:p1")
    pg.wait_for_function("!document.getElementById('talk').disabled", timeout=10000)
    pg.locator("#face svg").click()
    check("顔: タップすると「タップして話す」が押される", pg.evaluate("window.__talk"), 1)

    asks["list"] = [{"pane": "w1:p1", "fp": "f1", "project": "blog", "title": "記事", "kind": "permission", "text": "Do you want to proceed?\n❯ 1. Yes\n  2. No",
                     "options": ["Yes", "No"], "session": "s1"}]
    pg.evaluate("loadAsks()")
    pg.wait_for_function("document.getElementById('face').classList.contains('attn')", timeout=5000)
    check("顔: 承認・入力待ちがあると縁が光る", pg.evaluate("document.getElementById('face').classList.contains('attn')"), True)
    asks["list"] = []
    pg.evaluate("loadAsks()")
    pg.wait_for_function("!document.getElementById('face').classList.contains('attn')", timeout=5000)
    check("顔: 無くなると縁の光は消える", pg.evaluate("document.getElementById('face').classList.contains('attn')"), False)

    shots = os.environ.get("FACE_SHOTS")  # 表情ごとの画面写し（見た目を確かめるときだけ）
    if shots:
        for st in ("idle", "listen", "think", "speak", "offline"):
            pg.evaluate(f"face('{st}')")
            pg.wait_for_timeout(350)
            pg.screenshot(path=f"{shots}/face-{st}.png")
        pg.evaluate("face('idle'); document.getElementById('face').classList.add('attn')")
        pg.wait_for_timeout(400)
        pg.screenshot(path=f"{shots}/face-attn.png")
        pg.evaluate("document.getElementById('face').classList.remove('attn')")
    pg.evaluate("window.__talk = 0")
    pg.locator("#faceBack").click()
    pg.wait_for_url(BASE + "/", timeout=5000)
    check("顔: 「画面に戻る」はふだんの画面に戻る", pg.locator("#face").is_visible(), False)
    b.close()

srv.should_exit = True
th.join(5)  # 本体を止め終えてから終わる（止める途中で終わると、まれに異常終了する）
bad = [r for r in results if not r[1]]
for name, ok, got, want in results:
    print(("ok  " if ok else "NG  ") + name + ("" if ok else f"  got={got!r} want={want!r}"))
print(f"\n{len(results) - len(bad)}/{len(results)} 通過")
sys.exit(1 if bad else 0)
