"""S1（報告の声を鳴らすのは 1 か所）と S2（決まった問いに本体がその場で答える）の画面の通し。本体の写しを 127.0.0.1:8791 に立て、
本物のブラウザ（Chromium）で開く（常駐の 8790・herdr・docker・Hermes・声のエンジンには触れない。声の一覧は、この試験の中の小さな偽物 127.0.0.1:8796 から取る）。
見ること:
  - 画面は hello で窓の印（タブごと）を名乗り、読み込み直しても同じ印のまま（本体は最後に話しかけた窓でだけ報告の声を鳴らす）
  - 決まった問い（「次は？」）には本体がその場で答え、その吹き出しに「本体がすぐ答えました」と出る
  - 声を鳴らさない窓に届いた報告（quiet）では、読み上げ中の印を付けない（ふつうの報告では付ける）
使い方: uv run spike/ring_ui_test.py"""
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
                  VL_HERMES_URL="http://127.0.0.1:9", VL_HERMES_HOME=f"{TMP}/hermes")
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


async def no_voice(clients, text, cancel):  # 声は合成しない。合図は本物と同じく、話し始め（speaking）と終わり（done）を送る
    for c in clients:
        await c.send(type="speaking")
    await asyncio.sleep(0.6)  # 本物は声の合成に時間がかかる（画面が会話を読み直して、読み上げの印を付けてから終わる）
    for c in clients:
        await c.send(type="done", sentences=0)


s.boot_ensure_hub = s.keep_gateway_token = s.watch_blocked = s.watch_delegation = s.hermes.watch_hermes_skills = nothing
s.list_agents = lambda: [{"pane": "w1:p1", "cwd": "/x/blog", "title": "記事", "status": "working", "kind": "claude", "session": "s1", "name": "", "hub": False}]
s.jsonl_path = lambda a: None
s.speak_to = no_voice
s.hermes.ask_hermes = lambda text: (_ for _ in ()).throw(AssertionError("決まった問いを Hermes に送った"))
srv = uvicorn.Server(uvicorn.Config(s.app, host="127.0.0.1", port=PORT, log_level="warning"))
th = threading.Thread(target=srv.run, daemon=True)
th.start()
for _ in range(200):
    if srv.started:
        break
    time.sleep(0.05)


def wins():
    return sorted(c.win for c in list(s.CLIENTS))


with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 1280, "height": 800})
    pg.route("**/api/prompts", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
    pg.goto(BASE + "/")
    pg.wait_for_function("[...document.getElementById('agent').options].some(o => o.value === 'hermes')", timeout=10000)
    pg.select_option("#agent", "hermes")
    end = time.time() + 10
    while not any(len(w) == 24 for w in wins()) and time.time() < end:
        time.sleep(0.05)
    first = [w for w in wins() if len(w) == 24]
    check("窓: 画面は hello で窓の印（16 進 24 字）を名乗る", (len(first), all(set(w) <= set("0123456789abcdef") for w in first)), (1, True))
    pg.reload()
    pg.wait_for_function("[...document.getElementById('agent').options].some(o => o.value === 'hermes')", timeout=10000)
    pg.select_option("#agent", "hermes")
    end = time.time() + 10
    while first and first[0] not in wins() and time.time() < end:
        time.sleep(0.05)
    check("窓: 読み込み直しても、同じタブは同じ印", first[0] in wins() if first else False, True)

    # S2: 決まった問いに本体が答え、吹き出しに印
    pg.wait_for_function("!document.getElementById('send').disabled", timeout=10000)
    pg.fill("#typed", "次は？")
    pg.click("#send")
    pg.wait_for_function("[...document.querySelectorAll('#log .msg.ai')].some(e => e.textContent.includes('いま、あなたを待っているものはありません'))", timeout=10000)
    last = pg.evaluate("(() => { const ai = document.querySelectorAll('#log .msg.ai'); return ai[ai.length - 1].textContent; })()")
    check("S2: 本体が答えた吹き出しに「本体がすぐ答えました」", ("本体がすぐ答えました" in last, "いま、あなたを待っているものはありません" in last), (True, True))
    pg.wait_for_function("!document.querySelector('#log .msg.speaking')", timeout=10000)

    # S1: 声を鳴らさない窓に届いた報告（quiet）には、読み上げ中の印を付けない。ふつうの報告では付ける
    def deliver(quiet):
        msg = {"type": "reply", "pane": "hermes", "text": "blog の記事が書けました", "report": True, "about": "blog｜記事", **({"quiet": True} if quiet else {})}
        pg.evaluate("m => ws.onmessage({ data: JSON.stringify(m) })", msg)
        time.sleep(0.8)
        return pg.evaluate("!!document.querySelector('#log .msg.ai.speaking')")
    check("S1: quiet の報告では、読み上げ中の印を付けない", deliver(True), False)
    check("S1: ふつうの報告では、読み上げ中の印を付ける（比べるため）", deliver(False), True)
    b.close()

srv.should_exit = True
th.join(timeout=10)
bad = [r for r in results if not r[1]]
for name, ok, got, want in results:
    print(("✓ " if ok else "✗ ") + name + ("" if ok else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{len(results) - len(bad)}/{len(results)} 件が期待どおり")
sys.exit(1 if bad else 0)
