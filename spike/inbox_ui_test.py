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
    op = "forget" if "forget" in cmd else "reopen" if "reopen" in cmd else "done"
    n = int(cmd[cmd.index(op) + 1])
    row = {"op": op, "at": time.time(), "n": n} if op == "forget" else \
        {"op": "reopen", "at": time.time(), "n": n, "note": cmd[cmd.index("--note") + 1]} if op == "reopen" else \
        {"op": "done", "at": time.time(), "n": n, "note": cmd[cmd.index("--note") + 1], "by": "user"}
    if "--unneeded" in cmd:
        row.update(unneeded=True, why=cmd[cmd.index("--why") + 1])
    with ITEMS.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return subprocess.CompletedProcess(cmd, 0, f"[{n}]\n".encode(), b"")


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
    (HOME / "voice-loop" / "teams-status.json").write_text(json.dumps({"orgs": {"o": {"ok": False, "reason": "parse"}}, "at": time.time()}))
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
    check(f"{who} カードには記録の番号を小さく出す（今日の宛ての畳みの行にも）",
          (page.eval_on_selector_all("#sitBody > .ibCard .meta .ibNo", "els => els.map(e => e.textContent)"),
           page.eval_on_selector_all("#sitBody .ibFyi .meta .ibNo", "els => els.map(e => e.textContent)")), (["[1]", "[2]"], ["[4]"]))
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
    bad = page.evaluate("""() => { const keep = replyTarget; const out = [Infinity, 1e400, 2 ** 53, 0.5, -3, null].map(i => { replyTarget = { item: i, about: 'x' }; return JSON.stringify(replyFields()); });
        replyTarget = keep; return out; }""")
    check(f"{who} 返信先の番号が扱えない数なら -1 を送って本体に断らせる（null にしてふつうの発言にしない）", bad, ['{"reply_item":-1}'] * 6)
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
    # 「済」を押した直後に別の件を開いても、その件の中身の上に「戻す」を出し、戻せる
    page.locator("#sitBody > .ibCard").nth(0).locator("button.done").click()
    page.wait_for_function("ibUndos.size === 1")
    page.locator("#sitBody > .ibCard").first.click()  # 残っている別の件（[2]）を開く
    page.wait_for_selector(".ibItem h3")
    in_item = (page.locator('#sitBody .ibToast[data-n="1"]').count(), page.text_content(".ibItem h3"))
    page.click('#sitBody .ibToast[data-n="1"] button')
    page.wait_for_function("ibUndos.size === 0")
    page.click(".ibItem .ibBack")
    page.wait_for_function("document.querySelectorAll('#sitBody > .ibCard').length === 2")
    check(f"{who} 済の直後に別の件を開いても「戻す」が出て、戻せる", (in_item, cards(page), CMDS), ((1, "処理時間のアラーム"), [TITLE1, "処理時間のアラーム"], []))
    # 5 秒以内に 2 枚続けて「済」を押しても、件ごとに戻せる（先に押した件の「戻す」が消えない）
    page.locator("#sitBody > .ibCard").nth(0).locator("button.done").click()
    page.wait_for_function("ibUndos.size === 1")
    page.locator("#sitBody > .ibCard").nth(0).locator("button.done").click()
    page.wait_for_function("ibUndos.size === 2")
    toasts = page.eval_on_selector_all("#sitBody .ibToast", "els => els.map(e => e.dataset.n)")
    page.click('#sitBody .ibToast[data-n="1"] button')
    page.click('#sitBody .ibToast[data-n="2"] button')
    page.wait_for_function("document.querySelectorAll('#sitBody > .ibCard').length === 2")
    check(f"{who} 2 枚続けて済にしても、件ごとに「戻す」が出て、先に押した件も戻せる", (sorted(toasts), cards(page), CMDS), (["1", "2"], [TITLE1, "処理時間のアラーム"], []))
    # 「済」を押す前に始まった一覧の読み直しが遅れて届いても、消したカードを戻さない
    stash = []

    def first_old(route):
        if not stash:
            stash.append((route, route.fetch()))  # 本体は「済」の前の一覧を作り終えている。届けるのを止めておく
        else:
            route.continue_()
    page.route("**/api/inbox", first_old)
    page.evaluate("void loadSituation()")
    wait(lambda: stash, 5)
    page.locator("#sitBody > .ibCard").nth(0).locator("button.done").click()
    page.wait_for_function("ibUndos.size === 1")
    wait(lambda: cards(page) == ["処理時間のアラーム"], 4)
    stash[0][0].fulfill(response=stash[0][1])  # 古い一覧を、いま届ける
    page.unroute("**/api/inbox")
    time.sleep(0.8)
    stale = cards(page)
    page.click('#sitBody .ibToast[data-n="1"] button')
    page.wait_for_function("document.querySelectorAll('#sitBody > .ibCard').length === 2")
    check(f"{who} 「済」の前に始まった読み直しが遅れて届いても、消したカードを戻さない", (stale, CMDS), (["処理時間のアラーム"], []))

    def race(path, act, after):
        """操作の POST の応答を待つ間に読み直しを始め、本体が操作の前の一覧で答えたものを、操作のあとの読み直しより後に届ける。"""
        held_post, stash2 = [], []

        def old_list(route):
            if not stash2:
                stash2.append((route, route.fetch()))
            else:
                route.continue_()
        page.route(f"**{path}", lambda r: held_post.append(r))
        act()
        wait(lambda: held_post, 5)
        page.route("**/api/inbox", old_list)
        page.evaluate("void loadSituation()")
        wait(lambda: stash2, 5)
        held_post[0].continue_()
        page.unroute(f"**{path}")
        after()
        stash2[0][0].fulfill(response=stash2[0][1])
        page.unroute("**/api/inbox")
        time.sleep(0.8)
        return cards(page)
    # あとで: 応答を待つ間に始まった読み直しが最後に届いても、隠した件を戻さない
    page.locator("#sitBody > .ibCard").first.click()
    got = race("/api/inbox/later", lambda: page.select_option(".ibItem select.ibLater", "morning"),
               lambda: wait(lambda: cards(page) == ["処理時間のアラーム"], 5))
    check(f"{who} あとでの応答を待つ間に始まった読み直しが最後に届いても、隠した件を戻さない", got, ["処理時間のアラーム"])
    ib.STATE_FILE.unlink(missing_ok=True)  # 次の確かめのために元へ戻す（読み直しを繰り返して、2 枚に戻るまで待つ）
    restored = wait(lambda: page.evaluate("loadSituation().then(() => document.querySelectorAll('#sitBody > .ibCard').length)") == 2, 10)
    if not restored:
        print("DBG restore:", page.evaluate("({gen: ibGen, sel: ibSel, extra: ibExtra && ibExtra.n, undos: [...ibUndos.keys()], body: $('sitBody').textContent.slice(0, 300)})"), flush=True)
    # 戻す: 応答を待つ間に始まった読み直し（まだ済の予約がある一覧）が最後に届いても、戻した件を消さない
    keep_undo, ib.UNDO = ib.UNDO, 8.0
    page.locator("#sitBody > .ibCard").first.locator("button.done").click()
    page.wait_for_function("ibUndos.size === 1")
    got = race("/api/inbox/undo", lambda: page.click('#sitBody .ibToast[data-n="1"] button'),
               lambda: wait(lambda: cards(page) == [TITLE1, "処理時間のアラーム"], 5))
    ib.UNDO = keep_undo
    check(f"{who} 戻すの応答を待つ間に始まった読み直しが最後に届いても、戻した件を消さない", (got, CMDS), ([TITLE1, "処理時間のアラーム"], []))
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
    page.wait_for_function("ibUndos.size > 0")
    rest, t_set = page.evaluate("Math.max(...[...ibUndos.values()].map(u => u.until)) - Date.now()"), time.time()
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
    page.wait_for_function("ibUndos.size > 0")
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
    page.select_option(".ibItem select.ibLater", "morning")
    page.wait_for_function("!document.querySelector('.ibItem') && document.querySelectorAll('#sitBody > .ibCard').length === 0")
    check(f"{who} あとで: 隠して数を出し、その件への返信もやめる",
          ("あとでに回したもの 1 件" in page.text_content("#sitBody"), page.is_visible("#replyBar")), (True, False))
    # 不要だった（見張りの手本）: 件の中身で理由を選ぶと、すぐ消えて「戻す」が出る。時間がたつと不要の印。「その他」に手本が並び、「外す」で外せる
    seed()
    page.goto(BASE + "/#i=2")
    page.wait_for_selector(".ibItem h3")
    opts = page.eval_on_selector_all(".ibItem select.ibUnneeded option", "els => els.map(e => [e.value, e.textContent])")
    check(f"{who} 件の中身に「不要だった ▾」と、本体が決めた理由の選び方を出す",
          opts, [["", "不要だった ▾"], ["notice", "周知・お知らせだった"], ["test", "試験・dev の通知だった"], ["alarm", "既知のアラームだった"],
                 ["notmine", "自分の件ではなかった"], ["none", "理由は選ばない"]])
    page.select_option(".ibItem select.ibUnneeded", "alarm")
    page.wait_for_selector("#sitBody .ibToast")
    check(f"{who} 不要だった: 一覧に戻り、すぐ消えて「不要にしました…（見張りの手本にします）」と「戻す」を出す（まだ道具は動かさない）",
          (page.text_content("#sitBody .ibToast span"), cards(page), CMDS), ("不要にしました「処理時間のアラーム」（見張りの手本にします）", [TITLE1], []))
    check(f"{who} …時間がたつと、不要の印と理由をコンテナの中の道具に付けさせる",
          (wait(lambda: len(CMDS) == 1, 6), CMDS[0][-8:] if CMDS else None),
          (True, ["2", "--by", "user", "--note", ib.UNNEEDED_NOTE, "--unneeded", "--why", "既知のアラーム"]))
    page.evaluate("loadSituation()")
    page.wait_for_function("document.querySelectorAll('#sitOther .lesson').length === 1")
    page.evaluate("document.getElementById('sitOther').open = true")
    lesson = page.text_content("#sitOther .lesson")
    check(f"{who} 「その他」に見張りの手本として、番号・題・理由を出す（畳みの見出しにも数）",
          ("[2]" in lesson, "処理時間のアラーム" in lesson, "理由: 既知のアラーム" in lesson, "見張りの手本 1" in page.text_content("#sitOther > summary"), "ハブから" in lesson),
          (True, True, True, True, False))
    page.click("#sitOther .lesson button.lessonOut")
    gone = wait(lambda: page.locator("#sitOther .lesson").count() == 0, 6)
    check(f"{who} 「外す」で手本から外し（コンテナの中の道具）、外したと出す",
          (gone, CMDS[-1][-2:], "[2] を手本から外しました（済の印はそのままです）" in page.text_content("#sitBody")), (True, ["forget", "2"], True))
    with ITEMS.open("a") as f:  # 画面で押していない手本（ハブに言った・置き場に書かれた）
        f.write(json.dumps({"op": "done", "at": time.time(), "n": 1, "by": "user", "unneeded": True, "why": "周知・お知らせ"}, ensure_ascii=False) + "\n")
    page.evaluate("loadSituation()")
    page.wait_for_function("document.querySelectorAll('#sitOther .lesson').length === 1")
    check(f"{who} 画面で押していない手本には「ハブから」と出し、題は文字として出す",
          ("ハブから" in page.text_content("#sitOther .lesson"), page.evaluate("window.__xss === undefined"), page.locator("#sitBody img").count()), (True, True, 0))
    # 「外す」の前に始まった読み直しが遅れて届いても、外した手本を戻さない
    stash_l = []

    def hold_first(route):
        if not stash_l:
            stash_l.append((route, route.fetch()))  # 本体は「外す」の前の一覧を作り終えている。届けるのを止めておく
        else:
            route.continue_()
    page.route("**/api/inbox", hold_first)
    page.evaluate("void loadSituation()")
    wait(lambda: stash_l, 5)
    page.click("#sitOther .lesson button.lessonOut")
    gone_l = wait(lambda: page.locator("#sitOther .lesson").count() == 0, 6)
    stash_l[0][0].fulfill(response=stash_l[0][1])  # 古い一覧を、いま届ける
    page.unroute("**/api/inbox")
    time.sleep(0.8)
    check(f"{who} 「外す」の前に始まった読み直しが遅れて届いても、外した手本を戻さない", (gone_l, page.locator("#sitOther .lesson").count()), (True, 0))
    # AI が片づけたもの（層 3）: 根拠つきで出し、「戻す」で判断待ちに戻す
    seed()
    with ITEMS.open("a") as f:
        f.write(json.dumps({"op": "done", "at": time.time(), "n": 2, "by": "ai", "note": "本体の確かめ（新しい会話）: 控え／根拠の時刻 2026-11-20 18:00／<b>返した</b>"}, ensure_ascii=False) + "\n")
    page.goto(BASE + "/#inbox")
    page.wait_for_selector("#ibAi .aiClosed")
    row = page.text_content("#ibAi .aiClosed")
    check(f"{who} AI が片づけたもの: 開いた畳みで、題と根拠を文字として出す（判断待ちの一覧からは外れる）",
          ("処理時間のアラーム" in row, "根拠: 本体の確かめ（新しい会話）: 控え" in row, "<b>返した</b>" in row, page.locator("#ibAi b").count(), cards(page)),
          (True, True, True, 0, [TITLE1]))
    page.click("#ibAi .aiClosed button")
    back = wait(lambda: cards(page) == ["処理時間のアラーム", TITLE1] or cards(page) == [TITLE1, "処理時間のアラーム"], 6)
    check(f"{who} 「戻す」で判断待ちに戻し（コンテナの中の道具）、畳みから外して一覧に戻す",
          (back, page.locator("#ibAi").count(), CMDS[-1][-4:], "[2] を判断待ちに戻しました" in page.text_content("#sitBody")),
          (True, 0, ["reopen", "2", "--note", ib.REOPEN_NOTE], True))
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
    # 通知から開いて話し相手の一覧を待っている間に、💡 で閉じたら、待ち終わっても返信先を付けない
    page.goto("about:blank")
    held5 = []
    page.route("**/api/agents", lambda r: held5.append(r))
    page.goto(BASE + "/#i=1")
    page.wait_for_selector(".ibItem h3")
    wait(lambda: held5, 5)
    page.evaluate("$('sitBtn').click()")  # 💡 で閉じる（数の表示でボタンの幅が動いても押し外さないよう、ボタンそのものを押す）
    check(f"{who} 💡 で受け箱を閉じた", page.is_visible("#situation"), False)
    for r in held5:
        r.continue_()
    page.unroute("**/api/agents")
    time.sleep(1.5)
    check(f"{who} 一覧を待つ間に 💡 で閉じたら、待ち終わっても件への返信にしない", (page.is_visible("#situation"), page.is_visible("#replyBar")), (False, False))
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
    # 件（/#t=）を読み込んでいる途中に受け箱を開いたら、件の読み込みと開き直しをやめる（遅れて届いた件の報告を返信先にしない）
    held_t = []
    page.route("**/api/topic*", lambda r: held_t.append(r))
    page.evaluate("location.hash = '#t=t0123456789'")
    wait(lambda: held_t, 5)
    page.evaluate("location.hash = '#inbox'")
    stopped = page.evaluate("pendingTopic === null")
    for r in held_t:
        r.continue_()
    page.unroute("**/api/topic*")
    check(f"{who} 件の読み込み中に受け箱を開いたら、件の読み込みをやめる", stopped, True)
    # あとでに回した件も、通知から開ける（一覧の隠す条件に関係なく、1 件だけ本体に聞く）
    req = urllib.request.Request(BASE + "/api/inbox/later", data=json.dumps({"n": 1, "until": "morning"}).encode(), headers={"Content-Type": "application/json"}, method="POST")
    later_ok = json.loads(urllib.request.urlopen(req, timeout=10).read()).get("ok")
    page.evaluate("location.hash = '#i=1'")
    check(f"{who} あとでに回した件も、通知から開ける", (later_ok, wait(lambda: page.locator(".ibItem h3").count() == 1 and page.text_content(".ibItem h3") == TITLE1, 6)), (True, True))
    check(f"{who} …開いた件への返信の状態になる", wait(lambda: page.is_visible("#replyBar") and "判断待ち [1]" in (page.text_content("#replyTo") or ""), 6), True)
    # 通知で開いた一覧に出ていない件が、ほかで片づいたら、読み直したときに古い中身を出さない
    with ITEMS.open("a") as fh:
        fh.write(json.dumps({"op": "done", "at": time.time(), "n": 1, "note": "", "by": "ai"}) + "\n")
    page.evaluate("loadSituation()")
    check(f"{who} 通知で開いた件がほかで片づいたら、読み直したときに中身を出さない",
          wait(lambda: "片づいたか見つかりませんでした" in (page.text_content(".ibItem") or "") and page.locator(".ibItem h3").count() == 0, 6), True)
    ITEMS.write_text("".join(l + "\n" for l in ITEMS.read_text().splitlines() if '"op": "done", "at"' not in l or '"n": 1,' not in l))
    # 通知の 1 件を本体に聞いている間に、利用者が別のカードを選んだら、遅れて届いた結果で上書きしない
    page.click(".ibItem .ibBack")
    held_i = []
    page.route("**/api/inbox/item*", lambda r: held_i.append(r))
    page.evaluate("location.hash = '#i=1'")
    wait(lambda: held_i, 5)
    page.locator("#sitBody > .ibCard").first.click()  # 一覧に出ている別の件（[2]）
    for r in held_i:
        r.continue_()
    page.unroute("**/api/inbox/item*")
    time.sleep(1.0)
    check(f"{who} 通知の 1 件を待つ間に別のカードを選んだら、遅れた結果で表示も返信先も替えない",
          (page.text_content(".ibItem h3"), "判断待ち [2]" in (page.text_content("#replyTo") or "")), ("処理時間のアラーム", True))
    ib.STATE_FILE.unlink(missing_ok=True)
    # 通知で開いた一覧外の件を確かめ直す答えが遅れて届いても、その間に済んだ・戻すをして開き直した件を消さない
    seed()
    req = urllib.request.Request(BASE + "/api/inbox/later", data=json.dumps({"n": 1, "until": "morning"}).encode(), headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=10).read()
    page.evaluate("location.hash = '#i=1'")
    wait(lambda: page.locator(".ibItem h3").count() == 1 and page.text_content(".ibItem h3") == TITLE1, 6)
    held_d, stash_i = [], []

    def item_old(route):
        if not stash_i:
            stash_i.append(route)  # 済の前に始まった確かめ直し。本体に届けるのも、答えを返すのも止めておく
        else:
            route.continue_()
    keep_undo2, ib.UNDO = ib.UNDO, 8.0  # 応答を止める間に「戻す」の残り（本体の残り − 待った時間）が尽きないように
    page.route("**/api/inbox/done", lambda r: held_d.append(r))
    page.click(".ibItem button.go")
    wait(lambda: held_d, 5)
    page.route("**/api/inbox/item*", item_old)
    page.evaluate("void loadSituation()")
    page.wait_for_function("true")  # 止めた要求の処理（item_old）を動かす
    wait(lambda: stash_i or page.evaluate("0"), 5)
    held_d[0].continue_()
    page.unroute("**/api/inbox/done")
    page.wait_for_function("ibUndos.size === 1")
    old_resp = stash_i[0].fetch()  # 本体は「済」の予約中なので、ここで作る答えは ok:false（古い答え）
    stale_ok = old_resp.json().get("ok")
    page.click('#sitBody .ibToast[data-n="1"] button')
    page.wait_for_function("ibUndos.size === 0")
    page.evaluate("location.hash = '#i=1'")
    reopened = wait(lambda: page.locator(".ibItem h3").count() == 1 and page.text_content(".ibItem h3") == TITLE1, 6)
    stash_i[0].fulfill(response=old_resp)
    page.unroute("**/api/inbox/item*")
    time.sleep(0.8)
    ib.UNDO = keep_undo2
    check(f"{who} 確かめ直しの古い答えが遅れて届いても、済んだ・戻すのあとで開き直した件を消さない",
          (stale_ok, reopened, page.locator(".ibItem h3").count() == 1 and page.text_content(".ibItem h3") == TITLE1, CMDS), (False, True, True, []))
    ib.STATE_FILE.unlink(missing_ok=True)
    page.click(".ibItem .ibBack")
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
