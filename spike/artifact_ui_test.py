# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "anthropic>=1.0", "openai>=1.40", "pywebpush>=2.0", "pillow>=10", "playwright==1.63.0"]  # 手元に入っているブラウザ（chromium 1243）に合わせる
# ///
"""成果物（返事に出てきた画像・動画）の画面の通し。本体の写しを 127.0.0.1:8791 に立て、本物のブラウザ（Chromium）で開く
（常駐の 8790・herdr・docker には触れない。声の一覧は、この試験の中の小さな偽物 127.0.0.1:8795 から取る）。
見ること: PC の幅で、返事の中の場所（` ` で囲んだもの・文の中・Markdown のリンクと画像）を拾ってサムネイルを並べる。本体が返さないもの
（無いファイル・隠しフォルダ）は並べたあとで消える。Markdown の画像は、その場では読み込まずに文字にする（外のサイトの画像は出さない）。
style の url() に書いた外のサイトの画像は CSP で止まり、外のサイトへは何も取りに行かない。サムネイルを押すと大きく見る画面（画像は原寸の
大きさで読める・動画は再生の操作つき）、← → で送る、Esc で閉じて押したサムネイルに戻る、開いている間はほかの列（右の列も）が inert。
ハブ（Hermes）の会話では絶対パスだけ。スマホの幅ではサムネイルを出さず、PC の幅に戻すと付ける（スマホの幅で描いた返事にも）。
使い方: uv run spike/artifact_ui_test.py"""
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
from PIL import Image  # noqa: E402
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
PROJ = TMP / "proj"
(PROJ / "out").mkdir(parents=True)
(PROJ / ".git").mkdir()
Image.new("RGB", (64, 48), (40, 120, 200)).save(PROJ / "out" / "a.png")
Image.new("RGB", (80, 40), (200, 120, 40)).save(PROJ / "out" / "b.webp")
Image.new("RGB", (30, 30), (40, 200, 120)).save(PROJ / "out" / "c.png")
Image.new("RGB", (30, 30), (0, 0, 0)).save(PROJ / ".git" / "x.png")
(PROJ / "out" / "clip.webm").write_bytes(b"\x1a\x45\xdf\xa3" + b"0" * 2000)  # 再生はできない中身（並べるのは HEAD で見て、▶ だけ出す）
A = lambda pane: {"pane": pane, "title": "検証", "cwd": str(PROJ), "kind": "claude", "session": pane, "status": "idle", "name": "", "hub": False}
s.list_agents = lambda: [A("w1:p1"), A("w2:p1")]
# 乗せられたセッションが書きうる、外へ通信する HTML（外のサイトの画像・CSS の @import・@font-face・style の url()）も混ぜる
TEXT = ("検証の結果です。\n\n- 差分の画像は `out/a.png` です\n- 二つ目は out/b.webp を見てください\n- 動画は [ここ](out/clip.webm) にあります\n"
        "- 無い out/none.png と、隠しの .git/x.png\n\n![図の説明](out/c.png)\n\n![外](https://example.invalid/x.png)\n\n"
        "<div><style>@import url(\"https://example.invalid/import.css\"); @font-face { font-family: x; src: url(https://example.invalid/f.woff); } "
        ".md { font-family: x; }</style><span style=\"background:url(https://example.invalid/y.png)\">背景</span></div>")
TEXT2 = "動画は out/missing.mp4、画像は out/a.png と out/b.webp です。"  # 無い動画は、有無の確かめ（HEAD）が遅れて返る
JSONL = {"w1:p1": TMP / "s1.jsonl", "w2:p1": TMP / "s2.jsonl"}
for pane, text in (("w1:p1", TEXT), ("w2:p1", TEXT2)):
    JSONL[pane].write_text(json.dumps({"type": "user", "message": {"content": "検証の画像を見せて"}}, ensure_ascii=False) + "\n"
                           + json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}, ensure_ascii=False) + "\n")
s.jsonl_path = lambda a: JSONL.get(a["pane"]) if a else None
s.hermes_log("ai", f"api の画像は {PROJ}/out/a.png です。相対の out/b.webp は見せません。")

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


def open_page(page, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(BASE + "/")
    page.wait_for_function("document.getElementById('conn').classList.contains('on')")


def names(page):
    return page.eval_on_selector_all("#log .msg.ai .media .thumb .nm", "els => els.map(e => e.textContent)")


EXPECT = ["a.png", "b.webp", "clip.webm", "c.png"]  # 文に出てくる順。無いもの・隠しフォルダは消える


def run(page, who):
    done, failed = [], []  # 外のサイトへの要求の、終わり方（CSP で止まった要求も request には出るので、失敗の理由で見る）
    page.on("requestfinished", lambda r: done.append(r.url))
    page.on("requestfailed", lambda r: failed.append((r.url, r.failure)))
    page.add_init_script("window.__csp = []; document.addEventListener('securitypolicyviolation', e => window.__csp.push(e.blockedURI));")
    open_page(page, 1400)
    page.select_option("#agent", "w1:p1")
    check(f"{who} 返事の中の場所を拾い、本体が返すものだけをサムネイルに並べる", wait(lambda: names(page) == EXPECT), True)
    check(f"{who} Markdown の画像は、その場では読み込まず文字にする（外のサイトの画像は出さない）",
          (page.eval_on_selector_all("#log .msg.ai .mref", "els => els.map(e => e.textContent)"), page.locator("#log .msg.ai img:not(.media img)").count()),
          (["🖼 図の説明", "🖼 外（外の画像は出しません）"], 0))
    time.sleep(0.5)
    ext = lambda: [x for x in done if "example.invalid" in x] + [x for x, _ in failed if "example.invalid" in x]
    check(f"{who} 返事の style の要素と属性は落とし、外のサイトへは何も要求しない（@import・@font-face・url()）。ふだんの画面に CSP の違反は出ない",
          (page.locator("#log .msg.ai style, #log .msg.ai [style]").count(), ext(), page.evaluate("window.__csp")), (0, [], []))
    # 画面の CSP そのもの（返事の HTML を落とす働きを抜けた場合の守り）: 外のサイトの画像・CSS・フォントは、ブラウザが止める
    page.evaluate("""(() => { const d = document.createElement('div');
      d.innerHTML = '<img src="https://example.invalid/z.png"><link rel="stylesheet" href="https://example.invalid/s.css">'
        + '<style>@font-face { font-family: y; src: url(https://example.invalid/g.woff); } .probe { font-family: y; }</style><span class="probe">あ</span>';
      document.body.appendChild(d); })()""")
    blocked = lambda: sorted(x.split("/")[-1] for x in page.evaluate("window.__csp") if "example.invalid" in x)
    check(f"{who} 画面の CSP が、外のサイトの画像・CSS・フォントを止める（届いた要求は無い）",
          (wait(lambda: blocked() == ["g.woff", "s.css", "z.png"]), [x for x in done if "example.invalid" in x]), (True, []))
    page.locator("#log .media .thumb", has_text="a.png").click()
    inert = lambda: page.evaluate("['side', 'asks', 'log', 'bar', 'rec'].map(id => document.getElementById(id).inert)")
    check(f"{who} 押すと大きく見る画面が開き、画像を原寸の大きさで読める。ほかの列（右の列も）は inert",
          (page.locator("#viewer").is_visible(), page.text_content("#vName"), page.text_content("#vPos"),
           wait(lambda: page.evaluate("(document.querySelector('#vStage img') || {}).naturalWidth") == 64), inert(), page.evaluate("document.activeElement.id")),
          (True, "a.png", "1 / 4", True, [True] * 5, "vClose"))
    check(f"{who} 最初では ← を押せない", page.locator("#vPrev").is_disabled(), True)
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    check(f"{who} → で次へ。動画は再生の操作つきで出す", (page.text_content("#vName"), page.text_content("#vPos"), page.evaluate("(document.querySelector('#vStage video') || {}).controls")),
          ("clip.webm", "3 / 4", True))
    page.keyboard.press("ArrowRight")
    check(f"{who} 最後では → を押せない", (page.text_content("#vName"), page.text_content("#vPos"), page.locator("#vNext").is_disabled()), ("c.png", "4 / 4", True))
    page.keyboard.press("ArrowLeft")
    check(f"{who} ← で前へ", (page.text_content("#vName"), page.text_content("#vPos")), ("clip.webm", "3 / 4"))
    page.keyboard.press("Escape")
    check(f"{who} Esc で閉じ、ほかの列も押せるようになり、押したサムネイルに戻る",
          (page.locator("#viewer").is_visible(), inert(), page.evaluate("document.activeElement.title")), (False, [False] * 5, "out/a.png"))
    page.locator("#log .media .thumb", has_text="c.png").click()
    page.click("#vClose")
    check(f"{who} 「✕ 閉じる」でも閉じる", (page.locator("#viewer").is_visible(), inert()), (False, [False] * 5))
    page.locator("#log .media .thumb", has_text="clip.webm").click()
    page.focus("#vStage video")
    page.keyboard.press("ArrowRight")
    check(f"{who} 動画の上の ← → は動画のもの（成果物を送らない）", page.text_content("#vName"), "clip.webm")
    page.click("#vClose")

    held = []  # 無い動画の要求（HEAD と、先頭のコマの読み込み）を、大きく見る画面を開くまで止めておく
    page.route("**/*missing.mp4*", lambda route: held.append(route))
    page.select_option("#agent", "w2:p1")
    wait(lambda: names(page) == ["missing.mp4", "a.png", "b.webp"] and len(held) >= 1)
    page.locator("#log .media .thumb", has_text="a.png").click()
    before = page.text_content("#vPos")
    for r in held:
        r.fulfill(status=404, body="")
    check(f"{who} 開いている間に並びから消えたものがあれば、位置を直し、→ で次へ進める",
          (before, wait(lambda: page.text_content("#vPos") == "1 / 2"), page.text_content("#vName")), ("2 / 3", True, "a.png"))
    page.keyboard.press("ArrowRight")
    check(f"{who} …→ で b.webp（2 / 2）", (page.text_content("#vName"), page.text_content("#vPos")), ("b.webp", "2 / 2"))
    page.set_viewport_size({"width": 390, "height": 844})
    check(f"{who} 開いたままスマホの幅に狭めると閉じ、inert も外す", (wait(lambda: not page.locator("#viewer").is_visible()), inert()), (True, [False] * 5))
    page.set_viewport_size({"width": 1400, "height": 900})
    page.unroute("**/*missing.mp4*")

    page.select_option("#agent", s.HERMES_PANE)
    check(f"{who} ハブ（Hermes）の会話では、絶対パスのものだけを並べる（相対の場所は本体が返さない）", wait(lambda: names(page) == ["a.png"]), True)

    page.set_viewport_size({"width": 390, "height": 844})
    check(f"{who} スマホの幅に狭めると、サムネイルを外す（画像・動画の要素も残さない）",
          wait(lambda: page.locator("#log .media").count() == 0 and page.locator("#log video, #log .media img").count() == 0), True)
    page.select_option("#agent", "w1:p1")
    page.wait_for_selector("#log .msg.ai .mref")
    check(f"{who} スマホの幅で描いた返事は、サムネイルをまだ作らない（読み込まない）", page.locator("#log .media").count(), 0)
    page.set_viewport_size({"width": 1400, "height": 900})
    check(f"{who} PC の幅に戻すと、スマホの幅で描いた返事にもサムネイルを付ける", wait(lambda: names(page) == EXPECT), True)


with sync_playwright() as pw:
    b = pw.chromium.launch()
    run(b.new_page(), "Chromium:")
    b.close()

srv.should_exit = True
th.join(5)
ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
