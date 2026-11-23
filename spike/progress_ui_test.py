"""ハブの返事を待つ間の一言（「確認しています」）を、本物のブラウザ（Chromium）で受ける通し。本体の写しを 127.0.0.1:8791 に立てる
（常駐の 8790・herdr・docker・Hermes・声のエンジンには触れない。ハブとの往復と声の合成は、この試験の中で差し替える）。
見ること:
  - 一言の声の再生をブラウザに止められ（タップ待ち）、そのあと往復が失敗したら、再生待ちの一言を捨てる。タップのボタンは消え、
    「話し終わりました」に上書きされず、エラーの文が残る（a2a f2bb8b69 の指摘）
  - 一言のあとに返事が来たら、一言・返事の順に鳴り、終わったら「話し終わりました」
使い方: uv run spike/progress_ui_test.py"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

TMP = Path(tempfile.mkdtemp()).resolve()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_VOICE_URL="http://127.0.0.1:8796", VL_HERMES_KEY="k1",
                  VL_HERMES_URL="http://127.0.0.1:9", VL_HERMES_HOME=f"{TMP}/no-hermes")
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


class Speakers(BaseHTTPRequestHandler):  # 声の一覧の小さな偽物（画面が読み込みのときに取る）
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


mode = {"fail": True}


async def fake_accepted(talker, text, pane, *a, **k):
    await asyncio.sleep(0.8)
    return ("", "Hermes に届きませんでした", True) if mode["fail"] else ("わかりました。", "", True)


async def fake_narrate(text, cancel):
    yield text


s.boot_ensure_hub = s.keep_gateway_token = s.watch_blocked = s.watch_delegation = s.hermes.watch_hermes_skills = nothing
s.list_agents = lambda: []
s.jsonl_path = lambda a: None
s.hub_accepted = fake_accepted
s.narrate = fake_narrate
s.synth = lambda text, sp: b"RIFF" + text.encode()  # 鳴らない中身（再生は試験の中で差し替える）
s.PROGRESS_AFTER = 0.1
srv = uvicorn.Server(uvicorn.Config(s.app, host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=srv.run, daemon=True).start()
for _ in range(200):
    if srv.started:
        break
    time.sleep(0.05)

# 再生を差し替える: block なら NotAllowedError（タップ待ち）、そうでなければ鳴ったことにして、すぐ終わる
PLAY = """(() => { window.__played = []; window.__block = false;
  HTMLMediaElement.prototype.play = function () {
    if (window.__block) { const e = new Error('blocked'); e.name = 'NotAllowedError'; return Promise.reject(e); }
    const src = this.src; window.__played.push(src === SILENT_URL ? 'silent' : 'blob');
    setTimeout(() => this.dispatchEvent(new Event('ended')), 30); return Promise.resolve();
  }; })()"""


def send_text(pg, text):
    pg.wait_for_function("!document.getElementById('send').disabled", timeout=10000)
    pg.fill("#typed", text)
    pg.click("#send")


with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 1280, "height": 800})
    pg.add_init_script(PLAY)
    pg.route("**/api/prompts", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
    pg.goto(BASE + "/")
    pg.wait_for_function("[...document.getElementById('agent').options].some(o => o.value === 'hermes')", timeout=10000)
    pg.select_option("#agent", "hermes")

    # 1) 一言の再生を止められたあと、往復が失敗する
    pg.evaluate("window.__block = true")
    mode["fail"] = True
    send_text(pg, "いまどうなってる？")
    pg.wait_for_function("document.getElementById('tap').style.display === 'inline-block'", timeout=10000)
    check("止められた一言はタップ待ち", pg.evaluate("queue.length"), 1)
    pg.wait_for_function("[...document.querySelectorAll('#log .sys')].some(e => e.textContent.includes('Hermes に届きませんでした'))", timeout=10000)
    time.sleep(0.3)
    check("失敗のあと: 再生待ちの一言を捨てる", pg.evaluate("queue.length"), 0)
    check("失敗のあと: タップのボタンは消える", pg.evaluate("document.getElementById('tap').style.display"), "none")
    check("失敗のあと: 「話し終わりました」で上書きしない", "話し終わりました" in pg.text_content("#statusText"), False)

    # 2) 一言のあとに返事が来る
    pg.evaluate("window.__block = false; window.__played = []")
    mode["fail"] = False
    send_text(pg, "次は？")
    pg.wait_for_function("document.getElementById('statusText').textContent.includes('話し終わりました')", timeout=10000)
    check("一言のあとに返事: 一言と返事の 2 つを鳴らす", pg.evaluate("window.__played.filter(x => x === 'blob').length"), 2)
    check("一言のあとに返事: 最後は「話し終わりました」", "話し終わりました" in pg.text_content("#statusText"), True)
    b.close()

srv.should_exit = True
bad = [r for r in results if not r[1]]
for name, ok, got, want in results:
    print(("ok  " if ok else "NG  ") + name + ("" if ok else f"  got={got!r} want={want!r}"))
print(f"\n{len(results) - len(bad)}/{len(results)} 通過")
sys.exit(1 if bad else 0)
