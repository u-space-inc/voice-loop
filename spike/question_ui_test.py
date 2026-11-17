"""選択式の質問で止まっているセッションの欄（画面の上に並べるもの）の通し。本体の写しを 127.0.0.1:8792 に立て、本物のブラウザ（Chromium）で開く。
herdr には触れない（一覧・画面の読み取り・キーの送信を差し替える）。声の一覧は、この試験の中の小さな偽物（127.0.0.1:8796）から取る。
見ること: いまの 1 問を読めない（この試験では読み取りを止めている）選択式の質問には、答えるボタンを 1 つも出さず、中身を見出しの印から
文字として見せる（前の「選択肢の番号だけを送る」ボタンは 0.26.1 で外した。1 問ずつ答える欄は form_ui_test が確かめる）・画面から読んだ
文字はタグにしない・何も送らない・スマホの幅（390px）と PC の幅（1400px）。
使い方: uv run spike/question_ui_test.py"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

TMP = tempfile.mkdtemp()
TTS_PORT = 8796
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_VOICE_URL=f"http://127.0.0.1:{TTS_PORT}")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "spike"))
import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

import server as s  # noqa: E402
from question_screens import REAL_CHOICE, REAL_MULTI  # noqa: E402

PORT = 8792
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


threading.Thread(target=HTTPServer(("127.0.0.1", TTS_PORT), Speakers).serve_forever, daemon=True).start()


async def nothing(*a, **k):
    return None


s.boot_ensure_hub = s.keep_gateway_token = s.watch_blocked = s.watch_delegation = s.intake_loop = s.hermes.watch_hermes_skills = nothing
XSS = '<img src=x onerror="window.__xss=1">'
SCREENS = {"w2:p1": REAL_CHOICE.replace("ぶどう", XSS),
           "w3:p1": REAL_CHOICE.replace("試験の質問です。どれにしますか？", "本番に反映しますか？"),
           "w4:p1": REAL_MULTI}
AGENTS = [{"pane": "w1:p1", "title": "記事", "cwd": "/x/blog", "kind": "claude", "session": "s1", "status": "idle", "name": "", "hub": False}]
AGENTS += [{"pane": p, "title": "受注", "cwd": f"/x/{name}", "kind": "claude", "session": f"s{i}", "status": "blocked", "name": "", "hub": False}
           for i, (p, name) in enumerate((("w2:p1", "shop"), ("w3:p1", "deploy"), ("w4:p1", "multi")), start=2)]
SENT = []
s.list_agents = lambda: [dict(a) for a in AGENTS]
s.jsonl_path = lambda a: None
s.pane_shows_trust = lambda pane: False
s.read_prompt = lambda pane: SCREENS.get(pane, "")
s.read_dialog = lambda pane, *_: (_ for _ in ()).throw(RuntimeError("この試験では読まない"))  # 1 問ずつの読み取りは answer_form_test・form_ui_test で確かめる（本物の herdr を呼ばない）
s.herdr = lambda *a, **k: (SENT.append((a[2], a[3])), {"result": {}})[1] if a[:2] == ("agent", "send-keys") else {"result": {}}

srv = uvicorn.Server(uvicorn.Config(s.app, host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=srv.run, daemon=True).start()
for _ in range(200):
    if srv.started:
        break
    time.sleep(0.05)


def wait(fn, limit=8.0):
    end = time.time() + limit
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


def card(page, pane):
    return page.locator(f'#asks .ask[data-pane="{pane}"]')


def run(page, width):
    who = f"{width}px:"
    SENT.clear()
    s.ANSWERED.clear()
    for a in AGENTS[1:]:
        a["status"] = "blocked"
    page.set_viewport_size({"width": width, "height": 900 if width >= 1100 else 844})
    page.goto(BASE + "/")
    page.wait_for_function("document.getElementById('conn').classList.contains('on')")
    page.wait_for_selector('#asks .ask[data-pane="w4:p1"]')
    shop, risky, multi = card(page, "w2:p1"), card(page, "w3:p1"), card(page, "w4:p1")
    check(f"{who} 1 つを選ぶ質問でも、いまの 1 問を読めなければ答えるボタンを出さず、中身を見出しの印から文字で見せる",
          (shop.locator(".head").text_content(), shop.locator("button").count(), shop.locator("pre").text_content().splitlines()[0],
           XSS in shop.locator("pre").text_content()),
          ("shop｜受注｜質問で止まっています。端末かハブで答えてください", 0, " ☐ 試験", True))
    check(f"{who} 画面から読んだ文字はタグにしない", (page.evaluate("window.__xss === undefined"), shop.locator("img").count()), (True, 0))
    check(f"{who} 取り消しにくく見える問いにも、答えるボタンを出さない", risky.locator("button").count(), 0)
    check(f"{who} 横にはみ出さない", page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), True)
    check(f"{who} 複数選択の質問もボタンを出さず、中身を見出しの印から見せる",
          (multi.locator("button").count(), multi.locator("pre").text_content().splitlines()[0], multi.locator("pre").evaluate("e => e.scrollTop")),
          (0, "←  ☐ 複数  ✔ Submit  →", 0))
    r = page.evaluate("""async () => (await (await fetch('/api/answer', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({pane: 'w2:p1', key: 's2', fp: 'x', choice: 'choose', n: 2})})).json()).reason""")
    check(f"{who} 前の答え方（choose）で頼まれても送らない", r, "bad_choice")
    check(f"{who} 何も送らない", SENT, [])


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    for width in (390, 1400):
        run(page, width)
    browser.close()

srv.should_exit = True
bad = [f"{n}: {g!r}（期待 {w!r}）" for n, ok, g, w in results if not ok]
print("\n".join(bad) or f"{len(results)} 例、すべて期待どおり")
sys.exit(1 if bad else 0)
