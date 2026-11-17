"""選択肢の問い（AskUserQuestion）を問いの形で答える欄の通し。本体の写しを 127.0.0.1:8791 に立て、本物のブラウザ（Chromium）で開く。
herdr には触れない（一覧・画面の読み取り・キーと文字の送信を、偽物の画面 FakeTUI に差し替える）。声の一覧は、この試験の中の小さな偽物（127.0.0.1:8795）から取る。
見ること: 問いごとに、1 つ選ぶ（丸）・複数選ぶ（四角）・自由記入の欄・見本を出す・記録から読んだ文字はタグにしない・選んでいない問いがあれば送らない・
送ると 1 手ずつ偽物の画面に届き、送ったことを出す・自由記入の欄に書くとその丸が選ばれる・取り消しにくく見える問いはもう一度押すまで送らない・
スマホの幅（390px）と PC の幅（1400px）で横にはみ出さない。
使い方: uv run spike/form_ui_test.py"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

TMP = tempfile.mkdtemp()
TTS_PORT = 8795
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_VOICE_URL=f"http://127.0.0.1:{TTS_PORT}")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "spike"))
import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

import server as s  # noqa: E402
from ask_tui import REAL_SHOP_INPUT, REAL_TWO_INPUT, FakeTUI  # noqa: E402

PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
s.ask_form.WAIT, s.ask_form.STEP = 1.0, 0.02
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
TWO_X = json.loads(json.dumps(REAL_TWO_INPUT))
TWO_X["questions"][1]["options"][2]["description"] = XSS
RISKY = {"questions": [{"question": "本番に反映しますか？", "header": "反映", "options": [{"label": "反映する", "description": "いま反映"}, {"label": "やめる"}]}]}
INPUTS = {"w2:p1": TWO_X, "w3:p1": REAL_SHOP_INPUT, "w4:p1": RISKY}
AGENTS = [{"pane": "w1:p1", "title": "記事", "cwd": "/x/blog", "kind": "claude", "session": "s1", "status": "idle", "name": "", "hub": False}]
AGENTS += [{"pane": p, "title": "受注", "cwd": f"/x/{name}", "kind": "claude", "session": f"s{i}", "status": "blocked", "name": "", "hub": False}
           for i, (p, name) in enumerate((("w2:p1", "shop"), ("w3:p1", "store"), ("w4:p1", "deploy")), start=2)]
TUIS: dict = {}
PATHS: dict = {}


def reset():
    for pane, inp in INPUTS.items():
        TUIS[pane] = FakeTUI(inp)
        p = Path(tempfile.mkdtemp()) / "s.jsonl"
        p.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"t-{pane}", "name": "AskUserQuestion", "input": inp}]}},
                                ensure_ascii=False) + "\n")
        PATHS[pane] = p
    for a in AGENTS[1:]:
        a["status"] = "blocked"
    s.ANSWERED.clear()
    s._PENDING.clear()


def fake_herdr(*a, **k):
    if a[:2] == ("agent", "send-keys"):
        TUIS[a[2]].keys(a[3])
    elif a[:2] == ("pane", "send-text"):
        TUIS[a[2]].text(a[3])
    return {"result": {}}


def shown(pane):
    t = TUIS[pane].screen() if pane in TUIS else ""
    return "\n".join(l for l in t.split("\n") if l.strip() and not set(l.strip()) <= set("─"))


s.list_agents = lambda: [dict(a) for a in AGENTS]
s.jsonl_path = lambda a: PATHS.get(a["pane"])
s.pane_shows_trust = lambda pane: False
s.read_prompt = shown
s.read_dialog = lambda pane, *_: TUIS[pane].screen()
s.herdr = fake_herdr

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
    reset()
    page.set_viewport_size({"width": width, "height": 900 if width >= 1100 else 844})
    page.goto(BASE + "/")
    page.wait_for_function("document.getElementById('conn').classList.contains('on')")
    page.wait_for_selector('#asks .ask[data-pane="w4:p1"] .send')
    shop, store, risky = card(page, "w2:p1"), card(page, "w3:p1"), card(page, "w4:p1")
    check(f"{who} 見出しと、問いごとの欄（1 つ選ぶは丸・複数選ぶは四角・自由記入の欄）",
          (shop.locator(".head").text_content(), shop.locator("legend").all_text_contents(),
           shop.locator('fieldset[data-i="0"] input[type=radio]').count(), shop.locator('fieldset[data-i="1"] input[type=checkbox]').count(),
           shop.locator('fieldset[data-i="0"] input.other').count(), shop.locator('fieldset[data-i="1"] input.other').count()),
          ("shop｜受注｜質問で止まっています。選んで「送る」を押してください", ["［朝］ 朝はどれ？", "［色］ 好きな色は？（複数可）（いくつでも選べます）"], 3, 3, 1, 0))
    check(f"{who} 見本つきの問いは見本を出し、自由記入の欄は出さない",
          (store.locator('fieldset[data-i="1"] pre.pv').count(), store.locator('fieldset[data-i="1"] input.other').count(),
           store.locator('fieldset[data-i="1"] pre.pv').nth(1).text_content()), (2, 0, "+----+\n|    |\n|    |\n+----+"))
    check(f"{who} 記録から読んだ文字はタグにしない", (page.evaluate("window.__xss === undefined"), shop.locator("img").count(), XSS in shop.text_content()), (True, 0, True))
    check(f"{who} 横にはみ出さない", page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), True)

    shop.locator('fieldset[data-i="0"] label', has_text="ごはん").click()
    shop.locator(".send").click()
    check(f"{who} 選んでいない問いがあれば送らない", (wait(lambda: "まだ選んでいない問いがあります" in (page.text_content("#log") or "")), TUIS["w2:p1"].log), (True, []))
    shop.locator('fieldset[data-i="1"] label', has_text="赤").click()
    shop.locator('fieldset[data-i="1"] label', has_text="緑").click()
    shop.locator(".send").click()
    done = wait(lambda: TUIS["w2:p1"].result is not None)
    AGENTS[1]["status"] = "idle"
    check(f"{who} 送ると 1 手ずつ届き、送ったことを出す",
          (done, TUIS["w2:p1"].result, TUIS["w2:p1"].log, wait(lambda: "shop｜受注 に「［朝］ごはん／［色］赤, 緑」を送りました" in (page.text_content("#log") or ""))),
          (True, ["ごはん", "赤, 緑"], ["2", "1", "3", "tab", "1"], True))

    check(f"{who} 自由記入の欄は、文字が見える幅がある", store.locator('fieldset[data-i="0"] input.other').evaluate("e => e.getBoundingClientRect().width > 150"), True)
    store.locator('fieldset[data-i="0"] input.other').fill("駅前の店")
    check(f"{who} 自由記入の欄に書くと、その丸が選ばれる", store.locator('fieldset[data-i="0"] input[value=other]').is_checked(), True)
    store.locator('fieldset[data-i="1"] label', has_text="縦長").click()
    store.locator(".send").click()
    done = wait(lambda: TUIS["w3:p1"].result is not None)
    AGENTS[2]["status"] = "idle"
    check(f"{who} 自由記入と見本つきの問いに答える", (done, TUIS["w3:p1"].result, TUIS["w3:p1"].log), (True, ["駅前の店", "縦長"], ["3", "text:駅前の店", "enter", "2", "enter", "1"]))

    check(f"{who} 取り消しにくく見える問いには印を出す", risky.locator(".head").text_content(), "deploy｜受注｜⚠ 取り消しにくい操作かもしれません。質問で止まっています。選んで「送る」を押してください")
    risky.locator("label", has_text="反映する").click()
    risky.locator(".send").click()
    check(f"{who} 取り消しにくく見える問いは、1 回目では送らず「もう一度」を出す", (TUIS["w4:p1"].log, risky.locator(".send").text_content()), ([], "もう一度押すと送る"))
    risky.locator(".send").click()
    check(f"{who} もう一度押すと送る", (wait(lambda: TUIS["w4:p1"].result is not None), TUIS["w4:p1"].result), (True, ["反映する"]))
    AGENTS[3]["status"] = "idle"


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
