"""顔の表示（/?face。スマホを仮のスタックチャンにする）の通し。本体の写しを 127.0.0.1:8791 に立て、本物のブラウザ（Chromium）で開く
（常駐の 8790・herdr・docker には触れない。声の一覧は、この試験の中の小さな偽物 127.0.0.1:8796 から取る）。
見ること:
  - ふだんの画面（/）には顔が出ない。名乗りに kind は付かない
  - PC の幅では、ふだんの画面の上の帯に顔の表示へのリンク（🙂）があり、押すとそのタブが顔の表示になる。スマホの幅では出さない
  - /?face では顔が画面いっぱいに出て、つながると「ふつう」の表情。名乗りに kind: "stackchan"・place: "home" が付く
  - 顔をタップすると「タップして話す」が押される。「画面に戻る」を押したときは押されず、ふだんの画面に戻る
  - 承認・入力待ちがあると縁が光り、無くなると消える
  - 状態の文が顔の下に出る。失敗の理由（聞き取れなかったなど）も顔の下に出る
  - 再生にタップが要るとき（自動の再生を断られた）に顔を押すと、録音ではなく再生を始める
  - 「画面に戻る」（押す・Enter）は、読み込み直さずに顔を閉じ、名乗りを画面に戻す。顔のキーの長押しの繰り返しでは押さない
  - 通知・リンクで件・受け箱を開いたら、顔を閉じて中身を見せる
  - 録音中は、再生のタップ待ちがあっても、顔のタップで録音を止めて送る
  - 「停止」で再生を止めたら、口を閉じる。録音中は、再生の始まり・終わり・停止でも「聞いている」のまま
  - タップのときに画面を消さない許可を頼み、外されたら画面に戻ったときに取り直す。顔を閉じたら手放し（頼んでいる途中でも）、取り直さない
  - 件・受け箱の場所つき（/?face#inbox など）で開いたときも、顔を閉じて中身を見せる
  - 顔の表示中は、裏の画面をフォーカス・操作できない（閉じたら戻る）
  - 代理（Hermes）からの知らせは、縁と顔の下の一言で示し、顔をタップしたら聞いたことにする
  - たまっている判断待ちの数は、左上の「💡 数」で示す（開いたときから。0 件なら出さない）
  - 録音中・再生中に接続が切れたら、録音や再生の終わりで「ふつう」に戻さず「つながっていない」のまま
  - 顔の表示は、保存された話し相手（前に別の画面で選んだセッション）を見ずにハブと話し、保存は書き換えない（ふだんの画面は保存どおり）
  - ハブが居なければ、ほかのセッションに替えず、顔の下にそう出す。顔を閉じても黙って替えず、選び直したらその相手を覚える
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
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_VOICE_URL="http://127.0.0.1:8796", VL_HERMES_HOME=f"{TMP}/hermes",
                  # 本物の聞き取り・Hermes に届かないよう、何も待ち受けていないポートを指す（a2a 35d3b740 の指摘）
                  VL_WHISPER="http://127.0.0.1:9/v1/audio/transcriptions", VL_HERMES_URL="http://127.0.0.1:9")
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
s.transcribe = lambda audio, filename: ""  # 作り物の録音は聞き取りに出さない（聞き取れなかった扱い）

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
    b = pw.chromium.launch(args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])  # 作り物のマイク
    pg = b.new_page(viewport={"width": 390, "height": 844})
    pg.add_init_script(CATCH)
    asks = {"list": []}
    pg.route("**/api/prompts", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(asks["list"])))
    ITEM = lambda n: {"n": n, "title": f"件 {n}", "project": "blog", "kind": "judge", "source": "mail", "when": "2026-11-22T03:00:00+09:00", "old": False}
    inbox = {"items": [ITEM(1), ITEM(2)]}
    pg.route("**/api/inbox", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(inbox)))

    pg.goto(BASE + "/")
    pg.wait_for_function("window.__hello.length > 0", timeout=10000)
    check("ふだん: 顔は出ない", pg.locator("#face").is_visible(), False)
    check("ふだん: 名乗りに kind は付かない", pg.evaluate("window.__hello[0].kind ?? null"), None)
    check("ふだん: スマホの幅では、顔の表示へのリンク（🙂）を出さない（帯が狭い）", pg.locator("#faceGo").is_visible(), False)
    pg.set_viewport_size({"width": 1400, "height": 900})
    pg.wait_for_function("document.body.classList.contains('wide')", timeout=5000)
    check("ふだん: PC の幅では、上の帯に顔の表示へのリンク（🙂）がある", (pg.locator("#faceGo").is_visible(), pg.evaluate("document.getElementById('faceGo').getAttribute('href')")), (True, "/?face"))
    with pg.expect_navigation():
        pg.locator("#faceGo").click()  # 🙂 を押すと、このタブが顔の表示になる
    pg.set_viewport_size({"width": 390, "height": 844})  # ここから先の顔の確かめは、スマホの幅で
    check("ふだん: 🙂 を押すと、顔の表示（/?face）を開く", pg.evaluate("location.search"), "?face")
    pg.wait_for_function("window.__hello.length > 0", timeout=10000)
    check("顔: 画面いっぱいに出る", pg.evaluate("(() => { const r = document.getElementById('face').getBoundingClientRect(); return [r.width >= 380, r.height >= 800]; })()"), [True, True])
    pg.wait_for_function("document.getElementById('face').dataset.s === 'idle'", timeout=10000)
    pg.wait_for_function("!document.getElementById('faceSit').hidden", timeout=10000)
    check("顔: 開いたときから、たまっている判断待ちの数を左上に出す", pg.evaluate("document.getElementById('faceSit').textContent"), "💡 2")
    check("顔: 数は縁を光らせない（急ぎだけが縁）", pg.evaluate("document.getElementById('face').classList.contains('attn')"), False)
    inbox["items"] = []
    pg.evaluate("refreshSitCount()")
    pg.wait_for_function("document.getElementById('faceSit').hidden", timeout=5000)
    check("顔: 0 件なら数を出さない", pg.evaluate("document.getElementById('faceSit').hidden"), True)
    check("顔: つながると「ふつう」の表情", pg.evaluate("document.getElementById('face').dataset.s"), "idle")
    h = pg.evaluate("window.__hello[0]")
    check("顔: 名乗りに stackchan と home が付く", (h.get("kind"), h.get("place")), ("stackchan", "home"))
    INERT = "['side','rec','asks','tasks','situation','log','bar'].map(id => document.getElementById(id).inert)"
    check("顔: 表示中は、裏の画面をフォーカス・操作できない", pg.evaluate(INERT), [True] * 7)
    pg.keyboard.press("Tab"); pg.keyboard.press("Tab"); pg.keyboard.press("Tab")
    check("顔: Tab で裏のボタンに移らない", pg.evaluate("!!document.activeElement.closest('#bar, #side, #asks, #log, #rec, #tasks, #situation')"), False)
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
    pg.wait_for_function("document.getElementById('face').dataset.s === 'listen'", timeout=10000)
    check("顔: 録音中は「聞いている」", pg.evaluate("document.getElementById('face').dataset.s"), "listen")
    pg.evaluate("face('speak'); finishSpeaking()")  # 録音中に、前の返答・ハブの報告の再生が始まり、終わる
    check("顔: 録音中は、再生の始まり・終わりでも「聞いている」のまま", pg.evaluate("document.getElementById('face').dataset.s"), "listen")
    pg.evaluate("ws.close()")  # 録音中に接続が切れる（画面は録音を止め、2 秒後につなぎ直す）
    pg.wait_for_timeout(600)
    check("顔: 録音中に切れたら、録音の終わりでも「つながっていない」のまま", pg.evaluate("document.getElementById('face').dataset.s"), "offline")
    pg.wait_for_function("document.getElementById('face').dataset.s === 'idle'", timeout=10000)
    # 再生の途中で切れたら、再生の始まり・終わりでも「つながっていない」のまま（codex-review c62083a）
    pg.evaluate("ws.close()")
    pg.wait_for_timeout(200)
    pg.evaluate("face('speak'); finishSpeaking()")
    check("顔: 再生の途中で切れたら、話し終わっても「つながっていない」のまま", pg.evaluate("document.getElementById('face').dataset.s"), "offline")
    pg.wait_for_function("document.getElementById('face').dataset.s === 'idle' || linked()", timeout=10000)
    pg.wait_for_function("linked()", timeout=10000)
    pg.evaluate("face('idle')")

    # 再生にタップが要るとき（iPhone で自動の再生を断られたとき）は、顔を押すと再生を始める
    # 画面を消さない許可: タップで頼み、外されたら、画面に戻ったときに取り直す
    WAKE = ("window.__wake = 0; window.__released = 0; window.__sentinel = null; window.__wakeDelay = 0;"
            "Object.defineProperty(navigator, 'wakeLock', { configurable: true, value: { request: async () => {"
            " window.__wake++; if (window.__wakeDelay) await new Promise(r => setTimeout(r, window.__wakeDelay));"
            " const w = new EventTarget(); w.release = async () => { window.__released++; w.dispatchEvent(new Event('release')); }; window.__sentinel = w; return w; } } })")
    pg.evaluate(WAKE)
    pg.evaluate("window.__talk = 0")
    pg.locator("#face svg").click()  # 録音を始める（作り物のマイク）
    pg.wait_for_function("window.__wake === 1 && rec && rec.state === 'recording'", timeout=10000)
    pg.evaluate("window.__tap = 0; document.getElementById('tap').addEventListener('click', () => { window.__tap++; }, true); queue.push(new ArrayBuffer(8)); showTap(true)")
    pg.locator("#face svg").click()  # 録音中は、再生のタップ待ちがあっても、まず録音を止めて送る（作り物の音なので短すぎるか、聞き取れない）
    pg.wait_for_function("rec.state !== 'recording'", timeout=10000)
    check("顔: 録音中は、再生のタップ待ちより録音を止めるのが先", pg.evaluate("window.__tap"), 0)
    pg.evaluate("stopPlayback()")
    pg.evaluate("window.__sentinel.dispatchEvent(new Event('release')); document.dispatchEvent(new Event('visibilitychange'))")
    pg.wait_for_function("window.__wake === 2", timeout=5000)
    check("顔: 外された画面を消さない許可を、画面に戻ったときに取り直す", pg.evaluate("window.__wake"), 2)

    pg.evaluate("window.__talk = 0; window.__tap = 0; queue.push(new ArrayBuffer(8)); showTap(true)")
    pg.locator("#face svg").click()
    check("顔: 再生にタップが要るときは、録音でなく再生を押す", (pg.evaluate("window.__tap"), pg.evaluate("window.__talk")), (1, 0))
    pg.evaluate("stopPlayback()")

    # 「停止」（PC では Esc）で再生を止めたら、口を閉じる（codex-review b5a95ff）
    pg.evaluate("face('speak'); document.getElementById('stop').click()")
    check("顔: 停止で再生を止めたら「ふつう」に戻る", pg.evaluate("document.getElementById('face').dataset.s"), "idle")
    # 顔にフォーカスしてキーを長押ししても、繰り返しでは押さない
    pg.evaluate("window.__talk = 0; const f = document.getElementById('face');"
                "f.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', repeat: true, bubbles: true }));"
                "f.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', repeat: true, bubbles: true }))")
    check("顔: キーの長押しの繰り返しでは「話す」を押さない", pg.evaluate("window.__talk"), 0)

    # 代理からの知らせは、縁と一言で示し、タップしたら聞いたことにする（前の「停止」への本体の返事が届き終わってから）
    pg.wait_for_timeout(500)
    pg.evaluate("ws.onmessage({ data: JSON.stringify({ type: 'agent_notice', n: 7, title: '見積もりの返事の催促', sender: 'ハブ' }) })")
    pg.wait_for_function("document.getElementById('face').classList.contains('attn')", timeout=5000)
    check("顔: 代理からの知らせで縁が光り、題が顔の下に出る",
          (pg.evaluate("document.getElementById('face').classList.contains('attn')"), "見積もりの返事の催促" in pg.evaluate("document.getElementById('faceCap').textContent")), (True, True))
    pg.evaluate("queue.push(new ArrayBuffer(8)); showTap(true)")  # タップは再生に回す（録音を始めない）
    pg.locator("#face svg").click()
    check("顔: タップしたら知らせは聞いたことにして、縁を消す", pg.evaluate("document.getElementById('face').classList.contains('attn')"), False)
    pg.evaluate("stopPlayback()")

    # 失敗の理由を顔の下に出す
    pg.evaluate("ws.onmessage({ data: JSON.stringify({ type: 'error', text: '聞き取れませんでした' }) })")
    pg.wait_for_function("document.getElementById('faceCap').textContent.includes('聞き取れませんでした')", timeout=5000)
    check("顔: 失敗の理由が顔の下に出る", pg.evaluate("document.getElementById('faceCap').textContent"), "⚠ 聞き取れませんでした")

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
    pg.evaluate("window.__talk = 0; window.__keep = 1; window.__hello.length = 0")
    pg.locator("#faceBack").focus()
    pg.keyboard.press("Enter")
    pg.wait_for_function("document.getElementById('face').hidden", timeout=5000)
    check("顔: 「画面に戻る」に Enter で、読み込み直さずに顔を閉じる（?face も外れる）",
          (pg.locator("#face").is_visible(), pg.evaluate("window.__talk ?? 0"), pg.evaluate("window.__keep"), pg.evaluate("location.search")), (False, 0, 1, ""))
    w0 = pg.evaluate("window.__wake")
    pg.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    pg.wait_for_timeout(200)
    check("顔: 閉じたら、画面を消さない許可を手放し、画面に戻っても取り直さない", (pg.evaluate("window.__released") >= 1, pg.evaluate("window.__wake") == w0), (True, True))
    pg.wait_for_function("window.__hello.length > 0", timeout=5000)
    check("顔: 閉じたら、裏の画面を操作できるように戻す", pg.evaluate(INERT), [False] * 7)
    check("顔: 閉じたら、名乗りを画面に戻す（kind なし）", pg.evaluate("window.__hello[window.__hello.length - 1].kind ?? null"), None)
    pg.goto(BASE + "/?face")
    pg.wait_for_function("document.getElementById('face').dataset.s === 'idle'", timeout=10000)
    pg.evaluate(WAKE + "; window.__wakeDelay = 400; window.__keep = 2")
    pg.locator("#face svg").click()  # 許可を頼む（届くのは 0.4 秒後）。録音も始まる
    pg.evaluate("queue.push(new ArrayBuffer(8)); showTap(true)")
    pg.locator("#face svg").click()  # 届く前にもう一度タップ（頼みは重ねない）
    check("顔: 許可が届く前に続けてタップしても、頼みは 1 つだけ", pg.evaluate("window.__wake"), 1)
    pg.locator("#faceBack").click()  # 頼んでいる途中で閉じる
    pg.wait_for_function("document.getElementById('face').hidden", timeout=5000)
    check("顔: 「画面に戻る」を押すと、読み込み直さずに顔を閉じる", (pg.locator("#face").is_visible(), pg.evaluate("window.__keep")), (False, 2))
    pg.wait_for_function("window.__released >= 1", timeout=5000)
    check("顔: 許可を頼んでいる途中で閉じても、届いたらすぐ手放す", pg.evaluate("window.__released"), 1)
    pg.evaluate("if (rec && rec.state === 'recording') stopRec()")
    # 再生にタップが要るとき（報告を聞くだけの使い方）のタップでも、画面を消さない許可を頼む
    pg.goto(BASE + "/?face")
    pg.wait_for_function("document.getElementById('face').dataset.s === 'idle'", timeout=10000)
    pg.evaluate(WAKE + "; window.__tap = 0; document.getElementById('tap').addEventListener('click', () => { window.__tap++; }, true); queue.push(new ArrayBuffer(8)); showTap(true)")
    pg.locator("#face svg").click()
    pg.wait_for_function("window.__wake === 1", timeout=5000)
    check("顔: 再生を始めるタップでも、画面を消さない許可を頼む", (pg.evaluate("window.__tap"), pg.evaluate("window.__wake")), (1, 1))
    pg.evaluate("stopPlayback()")
    # 件・受け箱の場所つきで開いたとき（最初の表示では場所の変化が起きない）
    pg.goto(BASE + "/?face#inbox")
    pg.wait_for_function("document.getElementById('face').hidden", timeout=10000)
    check("顔: 受け箱の場所つきで開いたら、顔を閉じる（?face も外れる）", (pg.locator("#face").is_visible(), pg.evaluate("location.search")), (False, ""))
    # 通知で受け箱・件を開いたら、顔を閉じて中身を見せる（通知は開いている画面の場所（#…）を変える）
    pg.goto(BASE + "/?face")
    pg.wait_for_function("document.getElementById('face').dataset.s === 'idle'", timeout=10000)
    pg.evaluate("location.hash = '#inbox'")
    pg.wait_for_function("document.getElementById('face').hidden", timeout=5000)
    check("顔: 通知で受け箱を開いたら、顔を閉じる（?face も外れる）", (pg.locator("#face").is_visible(), pg.evaluate("location.search")), (False, ""))

    # 顔の表示は、いつもハブと話す（2026-11-24: 前に PC の画面で選んだセッションを顔が引き継ぎ、声の頼みがそのセッションにそのまま届いた）
    HUB_PANE = s.hermes.HERMES_PANE
    s.HUB.on = lambda: True  # この試験の中だけ、ハブ（Hermes）を一覧に載せる（本物の Hermes には届かない。VL_HERMES_URL は何も待ち受けていないポート）
    pg.evaluate("localStorage.setItem('vl.agentKey3', 's1')")  # 前に別の画面で、ハブでないセッションを選んでいた
    pg.goto(BASE + "/")
    pg.wait_for_function("document.getElementById('agent').value !== ''", timeout=10000)
    check("ふだん: 保存された話し相手（前に選んだセッション）を開く", pg.evaluate("document.getElementById('agent').value"), "w1:p1")
    pg.goto(BASE + "/?face")
    pg.wait_for_function("document.getElementById('agent').value !== '' && linked()", timeout=10000)
    check("顔: 保存された話し相手がほかのセッションでも、ハブと話す", (pg.evaluate("document.getElementById('agent').value"), pg.evaluate("curKey()")), (HUB_PANE, "hub"))
    check("顔: 保存された話し相手は書き換えない（ほかの画面の選択はそのまま）", pg.evaluate("localStorage.getItem('vl.agentKey3')"), "s1")
    pg.locator("#faceBack").click()
    pg.wait_for_function("document.getElementById('face').hidden", timeout=5000)
    pg.evaluate("agentsSig = ''; loadAgents()")  # 一覧が変わって作り直しても
    pg.wait_for_timeout(500)
    check("顔を閉じても、黙ってほかの相手に替えない", pg.evaluate("document.getElementById('agent').value"), HUB_PANE)
    pg.evaluate("localStorage.setItem('vl.agentKey3', 'x9')")  # 選び直したら書くことを見分けるため
    pg.select_option("#agent", "w1:p1")
    pg.evaluate("agentsSig = ''; loadAgents()")
    pg.wait_for_timeout(500)
    check("顔を閉じたあとで選び直したら、その相手を覚える", (pg.evaluate("document.getElementById('agent').value"), pg.evaluate("localStorage.getItem('vl.agentKey3')")), ("w1:p1", "s1"))
    s.HUB.on = lambda: False  # ハブが居ない
    pg.goto(BASE + "/?face")
    pg.wait_for_function("linked() && agents.length > 0", timeout=10000)
    pg.wait_for_timeout(300)  # 一覧を受けて、状態の文を出し終えるまで
    check("顔: ハブが居なければ、ほかのセッションに替えず、顔の下にそう出す",
          (pg.evaluate("document.getElementById('agent').value"), pg.evaluate("document.getElementById('talk').disabled"), pg.evaluate("document.getElementById('faceCap').textContent")),
          ("", True, "ハブが起動していません（「画面に戻る」から起動できます）"))
    check("顔: ハブが居なくても、保存された話し相手は書き換えない", pg.evaluate("localStorage.getItem('vl.agentKey3')"), "s1")
    pg.locator("#faceBack").click()
    pg.wait_for_function("document.getElementById('face').hidden", timeout=5000)
    check("顔を閉じたら、案内から「画面に戻る」を外す", pg.evaluate("document.getElementById('statusText').textContent"), "ハブが起動していません")
    del s.HUB.on
    b.close()

srv.should_exit = True
th.join(5)  # 本体を止め終えてから終わる（止める途中で終わると、まれに異常終了する）
bad = [r for r in results if not r[1]]
for name, ok, got, want in results:
    print(("ok  " if ok else "NG  ") + name + ("" if ok else f"  got={got!r} want={want!r}"))
print(f"\n{len(results) - len(bad)}/{len(results)} 通過")
sys.exit(1 if bad else 0)
