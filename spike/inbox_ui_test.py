"""判断待ちの受け箱（画面の 💡）の通し。本体の写しを 127.0.0.1:8791 に立て、本物のブラウザ（Chromium と、iPhone を名乗る WebKit）で開く。
Hermes の置き場は一時のフォルダ、Hermes の API とコンテナの中の記録の道具は差し替える（docker・常駐の 8790・本物の Hermes には触れない）。
声の一覧は、この試験の中の小さな偽物（127.0.0.1:8795）から取る。部品と口の確かめは spike/inbox_test.py。
見ること:
- 💡 に判断待ちの数が出る。開くと、見張りが止まった帯・判断待ちのカード（新しい順）・3 日より前の畳み・今日の宛ての畳み・「その他」が出る
- 文は文字として出す（タグを解釈しない）。元を開くリンクは本体が作ったものだけで、Hermes の文の中の URL はリンクにしない
- カードを押すと中身が開き、ハブの会話に替わって、その件への「↩ 返信」の状態になる。開いただけでは何も送らない
- 空のまま「このまま送る」で、件の前置きつきで送る。ことばを添えれば「送る」。送ったら返信先は外れ、「↩ この件に返信」で付け直せる
- 済んだ: すぐ消えて「戻す」が出る。戻すと何もしない。時間がたつとコンテナの中の道具に付けさせる
- あとで: 選んだ時刻まで隠す
- 通知・リンクの /#i=<番号> でその件、/#inbox で受け箱が開く（無い番号は案内だけ）
- 知らせが届いても、会話の欄に「📨」の行を出さない（💡 の数だけ変わる）
- スマホの幅で横にはみ出さない
使い方: uv run spike/inbox_ui_test.py"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

TMP = tempfile.mkdtemp()
TTS_PORT, PORT = 8795, 8791
HOME = Path(TMP) / "hermes"
(HOME / "voice-loop").mkdir(parents=True)
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HERMES_KEY="k1", VL_HERMES_URL="http://127.0.0.1:9",
                  VL_HERMES_HOME=str(HOME), VL_VOICE_URL=f"http://127.0.0.1:{TTS_PORT}")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

import server as s  # noqa: E402

BASE = f"http://127.0.0.1:{PORT}"
ib = s.hermes.hermes_inbox
ITEMS = HOME / "voice-loop" / "items.jsonl"
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


s.boot_ensure_hub = s.keep_gateway_token = s.watch_blocked = s.watch_delegation = s.hermes.watch_hermes_skills = nothing
s.HUB.watchers = lambda: []
s.list_agents = lambda: [{"pane": "w1:p1", "title": "見積", "cwd": "/x/lp", "kind": "claude", "session": "s1", "status": "idle", "name": "", "hub": False}]
ASKED, CMDS = [], []
s.hermes.ask_hermes = lambda text: ASKED.append(text) or "答えです"


def fake_run(cmd, capture_output=True, timeout=None, **k):
    CMDS.append(cmd)
    n = int(cmd[cmd.index("done") + 1])
    with ITEMS.open("a") as f:
        f.write(json.dumps({"op": "done", "at": time.time(), "n": n, "note": ib.DONE_NOTE, "by": "user"}, ensure_ascii=False) + "\n")
    return subprocess.CompletedProcess(cmd, 0, f"片づけた印を付けました [{n}]\n".encode(), b"")


ib.subprocess = types.SimpleNamespace(run=fake_run, TimeoutExpired=subprocess.TimeoutExpired)
ib.hermes_skills.docker_bin = lambda: "/usr/local/bin/docker"
ib.UNDO = 2.0
NOW = time.time()
TITLE1 = "<img src=x onerror=\"window.__xss=1\">本番環境へのメンバー追加"


def seed():
    rows = [
        {"op": "add", "at": NOW - 3 * 3600, "source": "teams", "id": "0123456789abcdef", "project": "shop", "kind": "judge", "title": "処理時間のアラーム", "note": "回復の知らせなし", "prepared": "", "n": 2},
        {"op": "add", "at": NOW - 600, "source": "mail", "id": "19a2b3c4d5e6f708", "project": "survey", "kind": "judge", "title": TITLE1,
         "note": "田中さんから、開発メンバー 2 名を本番環境に追加したいという申請", "prepared": "返信の案: 申請ありがとうございます。https://evil.example/ も見てください", "n": 1},
        {"op": "add", "at": NOW - 5 * 86400, "source": "slack", "id": "C1/1.2", "project": "lp", "kind": "judge", "title": "古い依頼", "note": "", "prepared": "", "n": 3},
        {"op": "add", "at": NOW - 60, "source": "mail", "id": "aaaaaaaaaaaaaaaa", "project": "shop", "kind": "fyi", "title": "先方の了承の返事", "note": "", "prepared": "対応不要", "n": 4},
    ]
    ITEMS.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    (HOME / "voice-loop" / "teams-status.json").write_text(json.dumps({"orgs": {"o": {"ok": False, "reason": "一覧の形が読めない"}}, "at": NOW}))
    ib.STATE_FILE.unlink(missing_ok=True)
    ib.PENDING.clear()
    ib.FAIL.clear()
    ASKED.clear()
    CMDS.clear()


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


def cards(page):
    return page.eval_on_selector_all("#sitBody > .ibCard .title", "els => els.map(e => e.textContent)")


def notice(title, items=None):
    req = urllib.request.Request(BASE + "/api/hub/notice", data=json.dumps({"title": title, "text": "", **({"items": items} if items else {})}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def run(page, who):
    seed()
    page.goto(BASE + "/")
    page.wait_for_function("document.getElementById('conn').classList.contains('on')")
    check(f"{who} 💡 に判断待ちの数（3 日以内）が出る", wait(lambda: page.text_content("#sitBtn") == "💡 2"), True)
    page.click("#sitBtn")
    page.wait_for_selector("#sitBody .ibCard")
    text = page.text_content("#sitBody")
    check(f"{who} 見張りが止まった帯と直し方を出す", ("Teams の見張りが止まっています" in text, "左の「メンション」を選んだまま" in text), (True, True))
    check(f"{who} 判断待ちのカードを新しい順に出し、題は文字として出す（タグを解釈しない）",
          (cards(page), page.evaluate("window.__xss === undefined"), page.locator("#sitBody img").count()), ([TITLE1, "処理時間のアラーム"], True, 0))
    folds = page.eval_on_selector_all("#sitBody > details > summary", "els => els.map(e => e.textContent)")
    check(f"{who} 3 日より前・今日の宛て・その他は畳んで出す",
          [f.split("（")[0].split(" ")[0] for f in folds], ["3", "今日届いたもの", "その他"])
    check(f"{who} 開いただけでは何も送らない", ASKED, [])
    # 件を開く
    page.locator("#sitBody > .ibCard").first.click()
    page.wait_for_selector(".ibItem h3")
    links = page.eval_on_selector_all(".ibItem a", "els => els.map(e => [e.textContent, e.href, e.target, e.rel])")
    check(f"{who} カードを押すと中身（要ること・準備したこと）が開き、元を開くリンクは本体が作ったものだけ",
          (page.text_content(".ibItem h3"), "田中さんから" in page.text_content(".ibItem"), links),
          (TITLE1, True, [["Gmail で開く ↗", "https://mail.google.com/mail/u/0/#all/19a2b3c4d5e6f708", "_blank", "noopener noreferrer"]]))
    time.sleep(1.5)  # 開いてしばらく待っても、何も送らない
    check(f"{who} …ハブの会話に替わり、その件への「↩ 返信」の状態になる。送るボタンは「このまま送る」。まだ何も送らない",
          (page.input_value("#agent"), page.is_visible("#replyBar"), page.text_content("#replyTo"), page.text_content("#send"), ASKED),
          ("hermes", True, f"↩ 判断待ち [1]「{TITLE1}」に返信", "このまま送る", []))
    page.fill("#typed", "期間は")
    check(f"{who} 文を打つと「送る」に替わる", page.text_content("#send"), "送る")
    page.fill("#typed", "")
    page.dispatch_event("#typed", "input")
    page.click("#send")
    check(f"{who} 空のまま「このまま送る」: 件の前置きつきで、中身と案を聞く文を送る", wait(lambda: len(ASKED) == 1), True)
    check(f"{who} …送った文", ASKED[:1], [f"〔返信: 判断待ち [1]「{TITLE1}」（survey）〕\n{s.hermes.ITEM_EMPTY}"])
    page.wait_for_function("[...document.querySelectorAll('#log .msg')].some(e => e.textContent.includes('答えです'))")
    mine = page.eval_on_selector_all("#log .msg.me", "els => els.map(e => e.textContent)")
    check(f"{who} …会話に「↩ 判断待ち [1] へ」「（ことばなし）」で出し、返信先は外れる",
          (any("↩ 判断待ち [1] へ" in m and "（ことばなし）" in m for m in mine), page.is_visible("#replyBar")), (True, False))
    page.click(".ibItem button:has-text('↩ この件に返信')")
    page.fill("#typed", "期間は 1 か月で")
    page.click("#send")
    check(f"{who} 「↩ この件に返信」で付け直し、ことばを添えて送る", wait(lambda: len(ASKED) == 2) and ASKED[1].endswith("\n期間は 1 か月で"), True)
    # 済んだ・戻す
    page.wait_for_selector(".ibItem button.go")
    page.click(".ibItem button.go")
    page.wait_for_selector("#sitBody .ibToast")
    check(f"{who} 済んだ: 一覧に戻り、すぐ消えて「戻す」が出る（まだ道具は動かさない）", (cards(page), CMDS), (["処理時間のアラーム"], []))
    page.click("#sitBody .ibToast button")
    page.wait_for_function("document.querySelectorAll('#sitBody > .ibCard').length === 2")
    check(f"{who} 戻すと一覧に戻り、道具は動かさない", (cards(page), CMDS), ([TITLE1, "処理時間のアラーム"], []))
    # 「戻す」は本体が返した残りの秒から、届くまでにかかった時間を引いた分だけ出し、期限が来たらその場で消す。
    # 一覧の読み直し（/api/inbox）を止めたままにしても、押した直後にカードが消えて「戻す」が出て、期限で消えることを見る
    def slow(route):
        r = route.fetch()  # 本体は応答を作り終えている（残りの秒を数えた）
        time.sleep(1.2)  # 配送を遅らせる
        route.fulfill(response=r)
    hold = []
    page.route("**/api/inbox", lambda r: hold.append(r))
    page.route("**/api/inbox/done", slow)
    t_click = time.time()
    page.locator("#sitBody > .ibCard").nth(1).locator("button.done").click()
    page.wait_for_function("ibUndo !== null")
    rest, t_set = page.evaluate("ibUndo.until - Date.now()"), time.time()
    shown = (page.locator("#sitBody .ibToast").count(), cards(page))
    gone = wait(lambda: page.locator("#sitBody .ibToast").count() == 0, ib.UNDO + 1.0)
    for r in hold:
        r.continue_()
    page.unroute("**/api/inbox")
    page.unroute("**/api/inbox/done")
    check(f"{who} 済: 読み直しを待たずに、カードを消して「戻す」を出す", shown, (1, [TITLE1]))
    check(f"{who} 「戻す」は、届くまでの遅れを引いた残りで消える（本体の期限を超えない）",
          (0 < rest <= (ib.UNDO - (t_set - t_click) + 0.3) * 1000 and rest <= (ib.UNDO - 1.2) * 1000) or (rest, round(t_set - t_click, 2)), True)
    check(f"{who} 期限が来たら、読み直しの応答を待たずに「戻す」を消す", gone, True)
    check(f"{who} カードの「済」: 時間がたつとコンテナの中の道具に付けさせ、一覧から消える",
          (wait(lambda: len(CMDS) == 1, 6), wait(lambda: cards(page) == [TITLE1], 8), CMDS[0][-6:-4] if CMDS else None), (True, True, ["done", "2"]))
    # 閉じている間に数だけ取り直したあと、開き直して読み直しが止まっていても、押したカードはその場で消える
    page.click("#sitClose")
    page.evaluate("refreshSitCount()")
    hold2 = []
    page.route("**/api/inbox", lambda r: hold2.append(r))
    page.click("#sitBtn")
    page.locator("#sitBody > .ibCard").first.locator("button.done").click()
    page.wait_for_function("ibUndo !== null")
    after = (cards(page), page.locator("#sitBody .ibToast").count())
    page.click("#sitBody .ibToast button")  # 戻す
    undone = wait(lambda: page.locator("#sitBody .ibToast").count() == 0, 2)  # 読み直しは止まったまま
    for r in hold2:
        r.continue_()
    page.unroute("**/api/inbox")
    check(f"{who} 数だけ取り直したあとでも、押したカードはその場で消え、戻すと一覧に戻る",
          (after, wait(lambda: cards(page) == [TITLE1], 6)), (([], 1), True))
    check(f"{who} 「戻す」を押したら、読み直しを待たずにその場で消す", undone, True)
    # あとで
    page.locator("#sitBody > .ibCard").first.click()
    page.select_option(".ibItem select", "morning")
    page.wait_for_function("!document.querySelector('.ibItem') && document.querySelectorAll('#sitBody > .ibCard').length === 0")
    check(f"{who} あとで: 隠して数を出し、その件への返信もやめる",
          ("あとでに回したもの 1 件" in page.text_content("#sitBody"), page.is_visible("#replyBar")), (True, False))
    # 通知・リンクの場所
    seed()
    page.goto(BASE + "/#i=3")
    page.wait_for_selector(".ibItem h3")
    check(f"{who} /#i=<番号> でその件（3 日より前でも）を開き、URL の印は外す", (page.text_content(".ibItem h3"), page.evaluate("location.hash")), ("古い依頼", ""))
    check(f"{who} …Slack の件には、元を開くリンクを作らない", page.locator(".ibItem a").count(), 0)
    # 通知から新しく開いたとき、話し相手の一覧が届く前に件が開いても、一覧が届いてから返信の状態にする
    held = []
    page.goto("about:blank")
    page.route("**/api/agents", lambda r: held.append(r))
    page.goto(BASE + "/#i=1")
    page.wait_for_selector(".ibItem h3")
    early = page.is_visible("#replyBar")
    wait(lambda: held, 5)
    for r in held:
        r.continue_()
    page.unroute("**/api/agents")
    check(f"{who} 一覧が届く前に件が開いても、届いてからその件への返信の状態にする",
          (early, wait(lambda: page.is_visible("#replyBar") and "判断待ち [1]" in (page.text_content("#replyTo") or ""), 8)), (False, True))
    # 一覧を待つ間に利用者が相手を選んだら、待ち終わってもハブに戻さない
    # （一覧は届き、会話の読み込みが止まっている間に、利用者が一覧から別のセッションを選ぶ）
    page.goto("about:blank")
    held.clear()
    page.route("**/api/history*", lambda r: held.append(r))
    page.goto(BASE + "/#i=1")
    page.wait_for_selector(".ibItem h3")
    page.wait_for_function("[...document.getElementById('agent').options].some(o => o.value === 'w1:p1')")
    page.select_option("#agent", "w1:p1")
    for r in held:
        r.continue_()
    page.unroute("**/api/history*")
    time.sleep(2.0)
    check(f"{who} 一覧を待つ間に利用者が相手を選んだら、待ち終わってもハブに戻さず、件への返信にもしない",
          (page.input_value("#agent"), page.is_visible("#replyBar")), ("w1:p1", False))
    # 通知から開いて、受け箱の読み込みを待つ間に利用者が相手を選んだら、件の中身は出すが、ハブに戻さず返信先も付けない
    page.goto("about:blank")
    held3 = []
    page.route("**/api/inbox", lambda r: held3.append(r))
    page.goto(BASE + "/#i=1")
    page.wait_for_function("[...document.getElementById('agent').options].some(o => o.value === 'w1:p1')")
    wait(lambda: held3, 5)
    page.select_option("#agent", "w1:p1")
    for r in held3:
        r.continue_()
    page.unroute("**/api/inbox")
    page.wait_for_selector(".ibItem h3")
    time.sleep(1.0)
    check(f"{who} 受け箱の読み込みを待つ間に相手を選んだら、件は開くがハブに戻さず返信先も付けない",
          (page.text_content(".ibItem h3"), page.input_value("#agent"), page.is_visible("#replyBar")), (TITLE1, "w1:p1", False))
    page.goto(BASE + "/#i=999")
    page.wait_for_selector("#sitBody .ibCard")
    check(f"{who} 無い番号は案内だけ出し、一覧を開く", "判断待ち [999] は、片づいたか見つかりませんでした" in page.text_content("#sitBody"), True)
    page.goto(BASE + "/")
    page.wait_for_function("document.getElementById('conn').classList.contains('on')")
    page.evaluate("location.hash = '#inbox'")
    opened = wait(lambda: page.is_visible("#situation") and page.locator("#sitBody > .ibCard").count() == 2)
    check(f"{who} /#inbox で受け箱が開く", opened or (page.is_visible("#situation"), page.text_content("#sitBody")[:200]), True)
    # 通知で別の番号を開いて見つからなければ、前の返信先は外れたまま（無関係な件への返信にしない）
    page.locator("#sitBody > .ibCard").first.click()
    wait(lambda: page.is_visible("#replyBar"), 5)
    page.evaluate("location.hash = '#i=999'")
    check(f"{who} 通知の件が見つからなければ、前の返信先を外したままにする",
          (wait(lambda: "判断待ち [999] は" in (page.text_content("#sitBody") or ""), 5), page.is_visible("#replyBar")), (True, False))
    # 読み込み中に受け箱を閉じたら、遅れて届いても件を開かず、返信先も付けない
    hold4 = []
    page.route("**/api/inbox", lambda r: hold4.append(r))
    page.evaluate("location.hash = '#i=1'")
    wait(lambda: hold4, 5)
    page.click("#sitClose")
    for r in hold4:
        r.continue_()
    page.unroute("**/api/inbox")
    time.sleep(1.5)
    check(f"{who} 読み込み中に閉じたら、遅れて届いても件を開かず返信先も付けない", (page.locator(".ibItem").count(), page.is_visible("#replyBar")), (0, False))
    # あとでに回した件も、通知から開ける（一覧の隠す条件に関係なく、1 件だけ本体に聞く）
    req = urllib.request.Request(BASE + "/api/inbox/later", data=json.dumps({"n": 1, "until": "morning"}).encode(), headers={"Content-Type": "application/json"}, method="POST")
    later_ok = json.loads(urllib.request.urlopen(req, timeout=10).read()).get("ok")
    page.evaluate("location.hash = '#i=1'")
    check(f"{who} あとでに回した件も、通知から開ける", (later_ok, wait(lambda: page.locator(".ibItem h3").count() == 1 and page.text_content(".ibItem h3") == TITLE1, 6)), (True, True))
    check(f"{who} …開いた件への返信の状態になる", wait(lambda: page.is_visible("#replyBar") and "判断待ち [1]" in (page.text_content("#replyTo") or ""), 6), True)
    ib.STATE_FILE.unlink(missing_ok=True)
    # 知らせが届いても、会話の欄に 📨 を出さない
    page.click("#sitClose")
    r = notice("判断待ち 1 件: 新しい件")
    time.sleep(1.0)
    check(f"{who} 知らせが届いても、会話の欄に「📨」の行を出さない", (r["ok"], "📨" in page.text_content("#log")), (True, False))
    check(f"{who} スマホの幅で横にはみ出さない", page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), True)


def run_wide(page, who):
    seed()
    page.goto(BASE + "/")
    page.wait_for_function("document.getElementById('conn').classList.contains('on')")
    page.click("#tabSit")
    page.wait_for_selector("#sitBody .ibCard")
    check(f"{who} PC の右の列のタブ「判断待ち」に、判断待ちの数を出す", (page.text_content("#tabSit"), cards(page)), ("判断待ち2", [TITLE1, "処理時間のアラーム"]))
    page.locator("#sitBody > .ibCard").first.click()
    time.sleep(1.5)
    check(f"{who} 押すと中身が開き、返信の状態になる（まだ送らない）", (page.is_visible(".ibItem h3"), page.text_content("#send"), ASKED), (True, "このまま送る", []))


with sync_playwright() as pw:
    b = pw.chromium.launch()
    # 画面の写しの service worker は止める（止めないと、ブラウザによっては page.route が要求を捕まえない）
    run(b.new_context(viewport={"width": 390, "height": 844}, service_workers="block").new_page(), "Chromium:")
    run_wide(b.new_context(viewport={"width": 1400, "height": 900}, service_workers="block").new_page(), "Chromium（PC）:")
    b.close()
    b = pw.webkit.launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, service_workers="block",
                        user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
    run(ctx.new_page(), "WebKit:")
    b.close()

srv.should_exit = True
th.join(5)
ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
