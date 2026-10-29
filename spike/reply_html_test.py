# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "anthropic>=1.0", "openai>=1.40", "pywebpush>=2.0", "playwright==1.63.0"]  # 手元に入っているブラウザ（chromium 1243）に合わせる
# ///
"""返事の HTML の守りの通し。本体の写しを 127.0.0.1:8791 に立て、本物のブラウザ（Chromium）で、PC の幅とスマホの幅で開く
（常駐の 8790・herdr・docker には触れない。Hermes には送らない。声の一覧は、この試験の中の小さな偽物 127.0.0.1:8795 から取る）。
相手は、乗せられたハブやセッション（返事に好きな Markdown と HTML を書ける）。画面の部品は id で探し（$('agent') など）、会話（#log）より後ろに
入力の欄（#bar）があるので、返事に同じ id を書くと本物より先に見つかる。
見ること: 返事に送る宛先と文の欄と同じ id（agent・typed）を書かれても、あなたが打った文を、選んだ相手へ送る。返事の label（for）・button
（form・popovertarget・commandfor）を押させても、本物の送る・録音は動かず、打ちかけの文は残る。返事の HTML に id・name・class・style・for・
form などが残らず、style・form・SVG の要素も落ちる。Markdown の表示（表の寄せ・番号の始まり・チェックボックス・外へのリンク）は残る。返事の画像
（<img>・<input type="image">・<svg><image>）は読み込まず説明の文字にし、表示しただけで本体の口（/api/hub/screen）や外のサイトへ要求を出さない。
本体を指すリンクはリンクにしない（押しても本体の口を呼ばない）。本体の別の名前を指すリンクは、押しても本体が断る（Sec-Fetch-Site）。
見出し・引用・コードの表示も残る。
使い方: uv run spike/reply_html_test.py"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

TMP = Path(tempfile.mkdtemp()).resolve()
TTS_PORT = 8795
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HUB="hermes", VL_HERMES_KEY="k1", VL_HERMES_URL="http://127.0.0.1:9",
                  VL_HERMES_HOME=f"{TMP}/hermes", VL_VOICE_URL=f"http://127.0.0.1:{TTS_PORT}")
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


threading.Thread(target=HTTPServer(("127.0.0.1", TTS_PORT), Speakers).serve_forever, daemon=True).start()


async def nothing(*a, **k):
    return None


s.boot_ensure_hub = s.keep_gateway_token = s.watch_blocked = s.watch_delegation = s.intake_loop = s.watch_hermes_skills = nothing
s.list_agents = lambda: [{"pane": "w1:p1", "title": "記事", "cwd": "/x/blog", "kind": "claude", "session": "s1", "status": "idle", "name": "", "hub": False}]
REPLY = ("# 見出し\n\n> 引用の文\n\n`手元のコード` と\n\n```\nブロックのコード\n```\n\n確認してください。\n\n| 項目 | 数 |\n|:--|--:|\n| 傷 | 3 |\n\n3. 三つ目から\n4. 四つ目\n\n- [x] 済んだこと\n- [ ] まだのこと\n\n"
         "[説明](https://example.invalid/doc \"題\") ![図の説明](out/a.png)\n\n"
         # 画面の部品と同じ id を書いて、本物を取り違えさせる（送る宛先と文）
         "<input id=\"agent\" value=\"w9:p9\"><input id=\"typed\" value=\"攻め手の文\"><span id=\"send\" class=\"msg\" style=\"color:red\" name=\"x\">赤</span>"
         # 本物の部品を押させる（label の for・button の form・popovertarget・commandfor）、外へ送らせる form、外の CSS
         "<label for=\"send\">画像を大きく見る</label> <label for=\"talk\">録音を見る</label> <button form=\"typedForm\">図を開く</button>"
         "<button popovertarget=\"status\" commandfor=\"status\" command=\"--x\">開く</button>"
         "<form action=\"https://example.invalid/f\"><button>外へ送る</button></form><style>.md { color: red }</style>"
         # 表示しただけで本体の口（承認の守りに使う記録を書き換える /api/hub/screen）や外へ要求を出させる、本体を指すリンクを押させる
         "<input type=\"image\" src=\"/api/hub/screen?pane=w1:p1\"><svg><image href=\"/api/hub/screen?pane=w1:p2\"></image></svg>"
         "<img src=\"/api/hub/screen?pane=w1:p4\" alt=\"本体の画像\"><img src=\"https://example.invalid/x.png\" alt=\"外の画像\">"
         "<a href=\"/api/hub/screen?pane=w1:p3\">画面を見る</a>\n\n"
         # 本体の別の名前を指すリンク（画面は 127.0.0.1 で開くので、localhost は別の場所としてリンクのまま残る）
         "[別名で画面](http://localhost:8791/api/hub/screen?pane=w1:p5)")
s.hermes_log("ai", REPLY)

srv = uvicorn.Server(uvicorn.Config(s.app, host="127.0.0.1", port=PORT, log_level="warning"))
th = threading.Thread(target=srv.run, daemon=True)
th.start()
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


def run(page, who, width):
    asked = []
    page.on("request", lambda r: asked.append(r.url))
    page.set_viewport_size({"width": width, "height": 900 if width >= 1100 else 844})
    page.goto(BASE + "/")
    page.wait_for_function("document.getElementById('conn').classList.contains('on')")
    page.wait_for_selector("#log .msg.ai table")
    reply = "#log .msg.ai"
    check(f"{who} 返事の HTML に id・name・class・style・for・form などが残らず、style と form の要素も落ちる",
          page.locator(f"{reply} :is([id], [name], [class]:not(.tw, .lead, .mref, .media, .media *, .mfig, .mfig *), [style], [for], [form], [popovertarget], [commandfor], [command], style, form)").count(), 0)
    check(f"{who} 見出し・引用・コードの表示は残る", page.evaluate(f"""(() => {{ const r = document.querySelector('{reply}');
            return [r.querySelector('h1').textContent, r.querySelector('blockquote').textContent.trim(), r.querySelector('p code').textContent,
                    r.querySelector('pre code').textContent.trim()]; }})()"""), ["見出し", "引用の文", "手元のコード", "ブロックのコード"])
    check(f"{who} Markdown の表示は残る（表の寄せ・番号の始まり・チェックボックス・外へのリンクと題）",
          page.evaluate(f"""(() => {{ const r = document.querySelector('{reply}');
            return [[...r.querySelectorAll('th')].map(e => e.getAttribute('align')), r.querySelector('ol').getAttribute('start'),
                    [...r.querySelectorAll('input')].slice(0, 2).map(e => [e.type, e.checked, e.disabled]),
                    [r.querySelector('a').getAttribute('href'), r.querySelector('a').getAttribute('title'), r.querySelector('a').getAttribute('target')]]; }})()"""),
          [["left", "right"], "3", [["checkbox", True, True], ["checkbox", False, True]], ["https://example.invalid/doc", "題", "_blank"]])
    text = page.text_content(reply)
    check(f"{who} 返事の画像は読み込まず、説明の文字にする（本体の口や外のサイトへ、表示しただけで要求を出さない。SVG も落ちる）",
          (page.locator(f"{reply} :is(img:not(.mfig img), svg, image)").count(), "🖼 図の説明" in text, "🖼 本体の画像" in text, "🖼 外の画像" in text,
           [x for x in asked if "/api/hub/screen" in x or "example.invalid" in x]), (0, True, True, True, []))
    link = page.locator(reply).get_by_text("画面を見る", exact=True)
    link.click()
    time.sleep(0.4)
    check(f"{who} 本体を指すリンクはリンクにしない（押しても本体の口を呼ばない）", (link.get_attribute("href"), [x for x in asked if "/api/hub/screen" in x]), (None, []))
    got = []
    page.context.on("response", lambda r: got.append((r.url.split("?")[0], r.status)) if "/api/hub/screen" in r.url else None)
    with page.context.expect_page() as tab:
        page.locator(reply).get_by_text("別名で画面", exact=True).click()
    tab.value.wait_for_load_state()
    check(f"{who} 本体の別の名前を指すリンクを押しても、本体が断る（ブラウザが別のサイトからと知らせる）",
          wait(lambda: page.evaluate("0") == 0 and got != []) and got, [(f"http://localhost:{PORT}/api/hub/screen", 403)])
    tab.value.close()
    check(f"{who} 画面の部品は本物のまま（id で探して、会話の中の要素を取らない）",
          page.evaluate("['agent', 'typed', 'send', 'talk', 'status'].map(id => document.getElementById(id).closest('#log') === null)"), [True] * 5)
    page.evaluate("(() => { window.__sent = []; const o = ws.send.bind(ws); ws.send = m => { window.__sent.push(m); if (typeof m !== 'string' || JSON.parse(m).type !== 'text') o(m); }; })()")
    sent = lambda: page.evaluate("window.__sent.map(m => JSON.parse(m)).filter(m => m.type === 'text').map(m => [m.pane, m.text])")
    page.fill("#bar #typed", "まだ送らない文")
    for text in ("画像を大きく見る", "録音を見る", "図を開く", "開く"):  # label・button は落ちて文字だけが残る。その文字を押す
        loc = page.locator(reply).get_by_text(text)
        if loc.count():
            loc.first.click()
    time.sleep(0.4)
    check(f"{who} 返事の label・button は落ち、残った文字を押しても、本物の送る・録音は動かず、打ちかけの文は残る",
          (page.locator(f"{reply} :is(label, button)").count(), sent(), page.input_value("#bar #typed"), page.locator("#talk.rec").count()), (0, [], "まだ送らない文", 0))
    wait(lambda: not page.locator("#bar #send").is_disabled())
    page.fill("#bar #typed", "本物の文")
    page.press("#bar #typed", "Enter")
    check(f"{who} 返事に送る宛先と文の欄と同じ id を書かれても、あなたが打った文を、選んだ相手へ送る",
          wait(lambda: sent() != []) and sent(), [[s.HERMES_PANE, "本物の文"]])


with sync_playwright() as pw:
    b = pw.chromium.launch()
    run(b.new_page(), "Chromium（PC）:", 1400)
    run(b.new_page(), "Chromium（スマホ）:", 390)
    b.close()

srv.should_exit = True
th.join(5)
ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
