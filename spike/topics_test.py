"""件（頼んだ件・報告・Slack のスレッド・問いを結ぶ単位。scripts/topics.py・server.py の topic_for・/api/topic・通知と Slack のリンク・
画面の件の URL・static/sw.js）の試験。本体の写しを 127.0.0.1:8791 に立て、本物のブラウザ（Chromium）で開く。声の一覧は 127.0.0.1:8795 の偽物から取る。
本物の herdr・Hermes・Slack・tailscale・配送サービスには触れない（差し替える）。
確かめること:
- 台帳: 件の id の形・続きの置き換え・Slack のスレッドで引く（スレッドの無い依頼は引かない）・セッションで引く・起動し直しても戻る・壊れた行・期限と上限
- 件の分け方（本人の A）: まだ報告の来ていない同じセッションへの依頼・Slack の同じスレッド・画面の「↩ 返信」で報告に返した発言の中の依頼は同じ件、それ以外は新しい件
- リンク: 外向きの URL は VL_PUBLIC_URL（https://ホスト[:ポート] だけ）か tailscale serve。形の違う URL・ホストは使わない
- 通知: 件ごとのタグと、押したら開く場所（/#t=<件>。ほかの形は / にする）。sw.js も、この画面の中の / と /#t=<件> だけを開く・渡す
- Slack: 報告の最後に「画面で開く」（問いなら「画面でこの問いに答える」）のリンク。形の違うリンクは付けない・長い報告を切ってもリンクは残す
- 相手の位置: コンテナ（Hermes）からは /api/topic を使えない。件の URL で開いても、選ぶ・出すだけで、答えも送りもしない。形の違う #t=・ほかのサイトの URL は開かない
使い方: uv run spike/topics_test.py"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

TMP = tempfile.mkdtemp()
TTS_PORT, PORT = 8795, 8791
DM = "D0ABCDEF12"
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HERMES_KEY="k1", VL_HERMES_URL="http://127.0.0.1:9",
                  VL_VOICE_URL=f"http://127.0.0.1:{TTS_PORT}", VL_SLACK_REPORT_CHAT=DM)
os.environ.pop("VL_PUBLIC_URL", None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "spike"))
import topics  # noqa: E402

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


# ---------- 台帳 ----------
a = topics.new("w1:p1", "s1", "lp", "見積を\n出して" + "あ" * 100)
check("台帳: 件の id は t と 16 進 10 桁。題は 1 行・80 字まで", (bool(topics.ID_RE.fullmatch(a)), topics.get(a)["title"][:6], len(topics.get(a)["title"])),
      (True, "見積を 出し", 80))
check("台帳: 形の違う id・無い id・文字列でないものは引けない", [topics.get(x) for x in ("../x", "t" + "0" * 10, None, 1, a + "0")], [None] * 5)
b = topics.new("w2:p1", "s2", "shop", "受注", ("D1", "1.1"))
check("台帳: Slack の同じスレッドで引ける。スレッドの無い（DM の一番上の）依頼・形の違う宛先では引かない",
      (topics.by_slack(("D1", "1.1")), topics.by_slack(("D1", "")), topics.by_slack(("D1", "2.2")), topics.by_slack("D1"), topics.by_slack(None)),
      (b, None, None, None, None))
time.sleep(0.01)
check("台帳: 続きの件は、いまの相手と Slack のスレッドを新しいほうに置き換える。無い件は False",
      (topics.touch(a, "w9:p9", "s9", ("D1", "3.3")), {k: topics.get(a)[k] for k in ("pane", "session", "slack")}, topics.touch("t" + "f" * 10, "x", "y")),
      (True, {"pane": "w9:p9", "session": "s9", "slack": ["D1", "3.3"]}, False))
check("台帳: セッションで一番新しい件を引ける（空のセッションは引かない）", (topics.latest_for_session("s9"), topics.latest_for_session("")), (a, None))
rows = json.loads(topics.TOPICS_FILE.read_text())
rows += [{"id": "../evil", "created": 1, "updated": time.time(), "pane": "x", "session": "x", "project": "x", "title": "x"},
         {"id": "t1111111111", "created": 1, "updated": time.time(), "pane": "x"},
         {"id": "t2222222222", "created": 1, "updated": time.time() - topics.TOPIC_KEEP - 1, "pane": "x", "session": "x", "project": "x", "title": "x"},
         {"id": "t3333333333", "created": 1, "updated": time.time(), "pane": "p", "session": "s", "project": "x", "title": "x", "slack": "D1/1.1"}]
topics.TOPICS_FILE.write_text(json.dumps(rows))
topics.TOPICS.clear()
topics.load()
check("台帳: 起動し直しても戻る。形の違う行・欄の足りない行は飛ばし、期限を過ぎた件は外す。Slack の宛先の形が違えば無いものとする",
      (sorted(topics.TOPICS), topics.get("t3333333333")["slack"], oct(topics.TOPICS_FILE.stat().st_mode & 0o777)),
      (sorted([a, b, "t3333333333"]), None, "0o600"))
topics.TOPICS_FILE.write_text("{壊れた")
topics.TOPICS.clear()
topics.load()
check("台帳: 読めない台帳は空から始める（止まらない）", topics.TOPICS, {})
old = topics.new("p", "s-exp", "x", "期限の切れる件", ("D1", "9.9"))
topics.TOPICS[old]["updated"] -= topics.TOPIC_KEEP + 1  # 保存し直す前に期限を過ぎた
check("台帳: 期限を過ぎた件は、保存し直す前でも引かない・続きにしない（Slack のスレッド・セッションでも）",
      (topics.get(old), topics.by_slack(("D1", "9.9")), topics.latest_for_session("s-exp"), topics.touch(old, "p", "s-exp")), (None, None, None, False))
_max = topics.TOPIC_MAX
topics.TOPIC_MAX = 3
ids = [topics.new("p", "s", "x", f"件{i}") for i in range(5)]
check("台帳: 上限を超えたら古い件から外す", sorted(topics.TOPICS), sorted(ids[2:]))
topics.TOPIC_MAX = _max

# ---------- リンク ----------
os.environ["VL_PUBLIC_URL"] = "https://mac.example.ts.net/"
check("リンク: VL_PUBLIC_URL があれば、それで件を開く URL を作る（末尾の / はそろえる）", topics.link(ids[-1]), f"https://mac.example.ts.net/#t={ids[-1]}")
check("リンク: 件の id の形が違えば作らない", [topics.link(x) for x in ("../x", "", None, "t123")], [None] * 4)
bad_urls = ["http://mac.example.ts.net", "https://mac.example.ts.net/x", "https://evil.example?x=1", "javascript:alert(1)", "https://a b", "https://mac.example.ts.net:443#"]
got = []
for u in bad_urls:
    os.environ["VL_PUBLIC_URL"] = u
    got.append(topics.link(ids[-1]))
check("相手の位置: VL_PUBLIC_URL が https://ホスト[:ポート] の形でなければリンクを付けない（tailscale も読まない）", got, [None] * len(bad_urls))
os.environ.pop("VL_PUBLIC_URL")
RUNS = []


class FakeRun:
    def __init__(self, out):
        self.stdout = out


def fake_status(web):
    def run(cmd, **k):
        RUNS.append(cmd[1:])
        return FakeRun(json.dumps({"Web": web}))
    return run


REAL_WHICH, REAL_RUN = topics.shutil.which, topics.subprocess.run  # 共有のモジュールなので、確かめたら戻す
topics.shutil.which = lambda name: "/bin/tailscale-fake"
topics.subprocess.run = fake_status({"mac.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8790"}}},
                                     "other.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}})
topics._BASE.update(url=None, at=0.0)
first = topics.link(ids[-1])
again = topics.link(ids[-1])
check("リンク: 無ければ tailscale serve からこのサーバーの URL を読む（10 分は覚えて、読み直さない）",
      (first, again, RUNS), (f"https://mac.example.ts.net/#t={ids[-1]}", first, [["serve", "status", "--json"]]))
got = []
for web in ({"evil.example/x?#:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8790"}}}},
            {"mac.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}, [], {"x": "y"}, {"x": {"Handlers": [1]}}):
    topics.subprocess.run = fake_status(web)
    topics._BASE.update(url=None, at=0.0)
    got.append(topics.link(ids[-1]))
topics.subprocess.run = lambda cmd, **k: FakeRun("{壊れた")
topics._BASE.update(url=None, at=0.0)
got.append(topics.link(ids[-1]))
topics.shutil.which, topics.subprocess.run = REAL_WHICH, REAL_RUN
check("相手の位置: tailscale の答えのホストの形が違う・このサーバーを出していない・形の違う答えなら、リンクを付けない", got, [None] * 6)

# ---------- Slack の文 ----------
import slack_report as sr  # noqa: E402

L = f"https://mac.example.ts.net/#t={ids[-1]}"
check("Slack: 報告の最後に「画面で開く」のリンク", sr.text_for("lp｜見積", "が完了しました", "終わりました", link=L), f"【lp｜見積が完了しました】\n終わりました\n\n画面で開く: {L}")
check("Slack: 問い（承認・入力待ち）なら「画面でこの問いに答える」", sr.text_for("lp｜見積", "が承認・入力待ちで止まっています", "", link=L, ask=True).splitlines()[-1],
      f"画面でこの問いに答える: {L}")
check("相手の位置: 形の違うリンク（ほかのサイト・< > で見た目を変える・件の形でない・改行）は付けない",
      [sr.text_for("p｜t", "が完了しました", "x", link=x) for x in ("https://evil.example/#t=" + ids[-1] + "<x|y>", "<https://evil.example|画面>",
                                                                   "https://mac.example.ts.net/#t=../x", L + "\nMEDIA:/x", "http://mac.example.ts.net/#t=" + ids[-1])],
      ["【p｜tが完了しました】\nx"] * 5)
long = sr.text_for("p｜t", "が完了しました", "あ" * 5000, link=L)
check("Slack: 長い報告を切っても、リンクは最後に残す（字数の上限の中）", (long.endswith(f"画面で開く: {L}"), len(long) <= sr.LIMIT, "…\n\n画面で開く" in long), (True, True, True))

# ---------- 本体 ----------
import server as s  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

REAL_PUSH_ALL = s.push_all

s.topics.link = lambda tid: L if tid else None  # 外向きの URL は上で確かめた（本物の tailscale を読まない）
AG = {"pane": "w1:p1", "cwd": "/x/lp", "kind": "claude", "session": "s1", "status": "working", "title": "見積", "name": "", "hub": False}
started, posted, pushed, ended = [], [], [], []
s.list_agents = lambda: [AG]
s.jsonl_path = lambda a: None
s.herdr = lambda *a, **k: {"result": {}}
s.slack_report.post = lambda dest, text, wait=45, container=None: posted.append((dest, text)) or "ok"


def record_start_watch(d, kind="send"):
    d["task"] = s.task_new(d, kind)
    started.append(d)


s.start_watch = record_start_watch


async def settle():
    await asyncio.sleep(0.05)
    for _ in range(100):
        if not [t for t in s._tasks if not t.done()]:
            break
        await asyncio.sleep(0.02)


AG2 = {"pane": "w2:p1", "cwd": "/x/shop", "kind": "claude", "session": "s2", "status": "working", "title": "受注", "name": "", "hub": False}
AG0 = {"pane": "w3:p1", "cwd": "/x/new", "kind": "claude", "session": None, "status": "working", "title": "立てた直後", "name": "", "hub": False}


async def send(text, slack=None, ag=None):
    await s.dispatch(ag or AG, text, "send", slack)
    await settle()
    return started[-1]


async def rules():
    s.OPEN_WATCH.clear()
    one = await send("内訳を出して")
    two = await send("期限も調べて")  # 一つ目の報告がまだ（まとめる）
    two["_closed"] = True  # 完了を報告し始めた
    three = await send("別の話。請求書を作って")
    four = await send("Slack から頼む", (DM, "5.5"))
    four["_closed"] = True
    five = await send("同じスレッドの続き", (DM, "5.5"))
    five["_closed"] = True
    six = await send("DM の一番上から", (DM, ""))
    six["_closed"] = True
    s.hermes.REPLY_TOPIC["topic"] = one["topic"]  # 画面の「↩ 返信」で一つ目の件の報告に返した発言を、ハブが扱っている
    try:
        seven = await send("返信の中で頼んだ")
        seven["_closed"] = True
        eight = await send("返信の間に Slack から", (DM, ""))
        eight["_closed"] = True
        ten = await send("返信の中で別のセッションへ", ag=AG2)
        ten["_closed"] = True
        zero = await send("返信の中でセッション ID の無い相手へ", ag=AG0)
        zero["_closed"] = True
    finally:
        s.hermes.REPLY_TOPIC["topic"] = ""
    nine = await send("同じスレッドを名乗って別のセッションへ", (DM, "5.5"), ag=AG2)
    nine["_closed"] = True
    back = await send("同じスレッドから、前のセッションの続き", (DM, "5.5"))
    return one, two, three, four, five, six, seven, eight, nine, ten, zero, back


one, two, three, four, five, six, seven, eight, nine, ten, zero, back = asyncio.run(rules())
check("件の分け方: まだ報告の来ていない同じセッションへの依頼は同じ件。完了を報告し始めたあとの依頼は新しい件",
      (one["topic"] == two["topic"], three["topic"] != two["topic"], s.topics.get(one["topic"])["title"]), (True, True, "内訳を出して"))
check("件の分け方: Slack の同じスレッドからの依頼は同じ件。スレッドの無い（DM の一番上の）依頼は新しい件",
      (five["topic"] == four["topic"], six["topic"] not in (four["topic"], five["topic"]), s.topics.get(four["topic"])["slack"]), (True, True, [DM, "5.5"]))
check("件の分け方: 画面の「↩ 返信」で報告に返した発言の中の依頼は、その報告の件。その間に Slack から頼まれた依頼には使わない",
      (seven["topic"] == one["topic"], eight["topic"] != one["topic"]), (True, True))
check("相手の位置: Slack の同じスレッド・返信中でも、別のセッション（セッション ID の分からない相手も）への依頼は新しい件。前の件の行き先（セッション）は書き換わらない",
      (nine["topic"] not in (four["topic"], five["topic"]), ten["topic"] != one["topic"], zero["topic"] != one["topic"],
       s.topics.get(four["topic"])["session"], s.topics.get(one["topic"])["session"], s.topics.get(nine["topic"])["session"]),
      (True, True, True, "s1", "s1", "s2"))
check("件の分け方: 同じスレッドから別のセッションにも頼んだあと、前のセッションの続きを頼んだら、前のセッションの件に戻る",
      (back["topic"] == four["topic"], back["topic"] != nine["topic"]), (True, True))
check("件の分け方: 頼んだ件ごとに件が付く（台帳にも載る）", all(s.topics.get(d["topic"]) for d in (one, three, four, six, eight)), True)


async def fake_target():
    return {"pane": "hub"}


async def fake_ask(hub, note):
    return ("done", "内訳は次のとおりです")


async def fake_end(state, reply, about, saved, pane="", topic=""):
    ended.append(topic)
    return True


async def fake_push(title, body, tag, urgency="normal", only=None, url="/"):
    pushed.append((tag, url))
    return "sent"


s.HUB.report_target, s.HUB.report_begin, s.HUB.report_end, s.ask, s.push_all = fake_target, (lambda h, a: True), fake_end, fake_ask, fake_push


async def reported():
    pushed.clear(), posted.clear(), ended.clear()
    await s.report(five, "done")
    await settle()


asyncio.run(reported())
check("報告: ハブの報告の記録に件を渡す。通知は件ごとのタグで、押すとその件を開く。Slack の報告の最後に件のリンク",
      (ended, pushed, [p[1].splitlines()[-1] for p in posted]), ([five["topic"]], [(f"topic-{five['topic']}", f"/#t={five['topic']}")], [f"画面で開く: {L}"]))
check("通知: 件が無ければ今までどおり（開く場所は画面のはじめ）", (s.topic_path(None), s.topic_path("../x"), s.topic_path(five["topic"])), ("/", "/", f"/#t={five['topic']}"))
PAY = []
s.push_subs = lambda: [{"endpoint": "https://push.example/x"}]
s._push_send = lambda subs, payload, urgency: (PAY.append(payload["url"]), (1, []))[1]


async def push_urls():
    out = []
    for u in ("/", f"/#t={five['topic']}", "https://evil.example/", "/#t=../x", f"/x#t={five['topic']}", f"//evil.example/#t={five['topic']}"):
        PAY.clear()
        await REAL_PUSH_ALL("t", "b", "tag", url=u)
        out.append(PAY[0] if PAY else None)
    return out


check("相手の位置: 通知に載せる開く場所は、/ か /#t=<件> だけ（ほかの形は / にする）", asyncio.run(push_urls()),
      ["/", f"/#t={five['topic']}", "/", "/", "/", "/"])

# ---------- 画面の口 ----------
with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    got = c.get("/api/topic", params={"id": one["topic"]}).json()
    gone = [c.get("/api/topic", params={"id": x}).json() for x in ("../x", "t" + "e" * 10, "")]
check("画面の口: 件を引くと、どのセッションの件かと題（と、あれば一番新しい報告・そのセッションの一番新しい件か）だけを返す。形の違う・無い件は gone",
      (got, gone), ({"ok": True, "id": one["topic"], "pane": "w1:p1", "session": "s1", "project": "lp", "title": "内訳を出して", "report": None, "current": False},
                    [{"ok": False, "reason": "gone"}] * 3))
with TestClient(s.app, base_url="http://host.docker.internal:8790") as c:
    r = c.get("/api/topic", params={"id": one["topic"]}, headers={"x-vl-key": "k1"})
check("相手の位置: コンテナ（Hermes）からは、鍵があっても件を引けない（画面だけの口）", (r.status_code, "pane" in r.text), (403, False))

# ---------- ハブの記録 ----------
s.hermes.hermes_log("ai", "lp の件が終わりました", report=True, about="lp｜見積", pane="w1:p1", topic=one["topic"], at=1700000000.25)
s.hermes.hermes_log("ai", "古い報告", report=True, about="lp｜見積", pane="w1:p1", at=1700000000.5)
s.hermes.hermes_log("ai", "偽の件", report=True, about="x", pane="w1:p1", topic="<img src=x>", at=1700000000.75)
hist = {m["at"]: m.get("topic") for m in s.hermes.hermes_history() if m.get("report")}
check("ハブの記録: 報告の件を履歴に出す。件の無い報告（0.29.0 より前）・形の違う件は出さない",
      (hist.get(1700000000.25), hist.get(1700000000.5), hist.get(1700000000.75)), (one["topic"], None, None))
check("ハブの記録: 件の一番新しい報告（記録の時刻と名前）を引ける。形の違う件・報告の無い件は None",
      (s.HUB.topic_report(one["topic"]), s.HUB.topic_report("<x>"), s.HUB.topic_report(two["topic"] if two["topic"] != one["topic"] else three["topic"])),
      ({"at": 1700000000.25, "about": "lp｜見積"}, None, None))
check("ハブの記録: 返信先の報告の件を引ける（無ければ空）",
      (s.hermes.report_topic(1700000000.25), s.hermes.report_topic(1700000000.5), s.hermes.report_topic(1700000000.75), s.hermes.report_topic(True)),
      (one["topic"], "", "", ""))
SEEN = []
real_ask_hermes = s.hermes.ask_hermes
s.hermes.ask_hermes = lambda text: (SEEN.append(s.hermes.reply_topic()), "わかりました")[1]
try:
    asyncio.run(s.hermes.hermes_exchange("続きをお願い", 1700000000.25))
    asyncio.run(s.hermes.hermes_exchange("ふつうの発言"))
finally:
    s.hermes.ask_hermes = real_ask_hermes
check("ハブの記録: 返信した発言を Hermes が扱っている間だけ、その報告の件を続きの件にする（終わったら空に戻す）",
      (SEEN, s.hermes.reply_topic()), ([one["topic"], ""], ""))
asyncio.run(fake_end("done", "x", "lp｜見積", True, "w1:p1", one["topic"]))
rec = [json.loads(x) for x in s.hermes.HERMES_LOG.read_text().splitlines()]


async def real_end():
    return await type(s.HUB).report_end(s.HUB, "done", "件つきの報告", "lp｜見積", True, "w1:p1", one["topic"]), \
        await type(s.HUB).report_end(s.HUB, "done", "形の違う件", "lp｜見積", True, "w1:p1", "<x>")


asyncio.run(real_end())
rec = [json.loads(x) for x in s.hermes.HERMES_LOG.read_text().splitlines()]
check("ハブの記録: 報告の記録に件を書く（形の違う件は書かない）",
      [(r["text"], r.get("topic")) for r in rec if r.get("text") in ("件つきの報告", "形の違う件")], [("件つきの報告", one["topic"]), ("形の違う件", None)])

# ---------- sw.js（通知を押したとき） ----------
SW = (ROOT / "static" / "sw.js").read_text()
SW_CASES = """async ([src, origin, cases]) => {
  const out = [];
  for (const [url, hasWindow] of cases) {
    const log = [], H = {};
    const win = { url: origin + '/', focus: async () => log.push('focus'), postMessage: m => log.push(['post', m.url]) };
    const fake = { location: { origin }, addEventListener: (t, f) => { H[t] = f; }, skipWaiting() {}, registration: {},
                   clients: { matchAll: async () => hasWindow ? [win] : [], openWindow: async u => log.push(['open', u]), claim() {} } };
    new Function('self', 'caches', 'fetch', src)(fake, {}, () => {});
    let p; H.notificationclick({ notification: { close() {}, data: { url } }, waitUntil: x => { p = x; } });
    await p; out.push(log);
  }
  return out;
}"""

# ---------- 画面（本物のブラウザ） ----------
from playwright.sync_api import sync_playwright  # noqa: E402
import uvicorn  # noqa: E402
from question_screens import REAL_CHOICE  # noqa: E402


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
SENT, ASKED = [], []
AGENTS = [{"pane": "w1:p1", "title": "見積", "cwd": "/x/lp", "kind": "claude", "session": "s1", "status": "idle", "name": "", "hub": False},
          {"pane": "w2:p1", "title": "受注", "cwd": "/x/shop", "kind": "claude", "session": "s2", "status": "blocked", "name": "", "hub": False}]
s.list_agents = lambda: [dict(x) for x in AGENTS]
s.pane_shows_trust = lambda pane: False
SLOW = {"n": 0}  # 問いを読むのを 1 回だけ遅らせる（件を開く途中で待たせる）


def read_prompt(pane):
    if SLOW["n"] > 0:
        SLOW["n"] -= 1
        time.sleep(1.5)
    return REAL_CHOICE if pane == "w2:p1" else ""


s.read_prompt = read_prompt
s.read_dialog = lambda pane, *_: (_ for _ in ()).throw(RuntimeError("この試験では読まない"))
s.herdr = lambda *a, **k: (SENT.append(a), {"result": {}})[1]
s.hermes.ask_hermes = lambda text: (ASKED.append(text), "わかりました")[1]
ASK_T = s.topics.new("w2:p1", "s2", "shop", "受注の確認")
NOSESS_T = s.topics.new("w2:p1", "", "shop", "セッション ID の分からない件")
OLD_T = s.topics.new("w2:p1", "s-old", "shop", "前のセッションの件")
NOREP_T = s.topics.new("w1:p1", "s1", "lp", "まだ報告の無い件")
PREV_T = s.topics.new("w2:p1", "s2", "shop", "同じセッションの前の件")
s.topics.TOPICS[PREV_T]["updated"] -= 100  # 問いの件（ASK_T）より前に動いた
s.hermes.hermes_log("ai", "前の件は片づきました", report=True, about="shop｜前の件", pane="w2:p1", topic=PREV_T)
s.topics.link = lambda tid: None

srv = uvicorn.Server(uvicorn.Config(s.app, host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=srv.run, daemon=True).start()
for _ in range(200):
    if srv.started:
        break
    time.sleep(0.05)
BASE = f"http://127.0.0.1:{PORT}"


def logs(page):
    return page.evaluate("[...document.querySelectorAll('#log .sys, #log .msg')].map(e => e.textContent).join('\\n')")


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    posts = []
    page.on("request", lambda r: posts.append(r.url.split(str(PORT), 1)[-1]) if r.method == "POST" else None)
    page.set_viewport_size({"width": 1400, "height": 900})
    page.goto(BASE + "/")
    page.wait_for_function("document.getElementById('conn').classList.contains('on')")
    page.select_option("#agent", "w1:p1")
    page.wait_for_timeout(300)
    page.goto(BASE + f"/#t={ASK_T}")
    page.wait_for_selector('#asks .ask[data-pane="w2:p1"].flash', timeout=8000)
    check("画面: 件の URL で開くと、その件のセッションの問いの欄を見せる（話し相手は替えない・URL の #t= は外す）",
          (page.eval_on_selector("#agent", "e => e.value"), page.evaluate("location.hash")), ("w1:p1", ""))
    page.evaluate("document.querySelectorAll('.flash').forEach(e => e.classList.remove('flash'))")
    page.evaluate(f"location.hash = '#t={PREV_T}'")
    page.wait_for_function("!document.getElementById('replyBar').hidden", timeout=8000)
    check("相手の位置: 同じセッションでも、あとの件の問いで止まっているなら、前の件のリンクでは問いの欄を出さず、前の件の報告を返信先にする",
          (page.evaluate("[...document.querySelectorAll('#asks .ask.flash')].length"), page.eval_on_selector("#replyTo", "e => e.textContent")),
          (0, "↩ shop｜前の件 の報告に返信"))
    page.select_option("#agent", "w1:p1")
    page.wait_for_timeout(300)
    page.evaluate(f"location.hash = '#t={OLD_T}'")
    page.wait_for_function("document.getElementById('replyBar') && !document.getElementById('replyBar').hidden || [...document.querySelectorAll('#log .sys')].some(e => e.textContent.includes('前のセッションの件'))", timeout=8000)
    check("相手の位置: 同じペインでも別のセッションの件なら、いまの問いの欄を出さない（報告を探す）",
          ("前のセッションの件: この件の報告は、まだありません" in logs(page), page.eval_on_selector("#agent", "e => e.value")), (True, "hermes"))
    page.evaluate("document.querySelectorAll('.flash').forEach(e => e.classList.remove('flash'))")  # 前に光らせた欄の光りを消してから
    page.evaluate(f"location.hash = '#t={NOSESS_T}'")
    page.wait_for_function("[...document.querySelectorAll('#log .sys')].some(e => e.textContent.includes('セッション ID の分からない件'))", timeout=8000)
    check("相手の位置: セッション ID の分からない件では、同じペインの問いの欄を出さない",
          page.evaluate("[...document.querySelectorAll('#asks .ask')].some(e => e.classList.contains('flash'))"), False)
    page.select_option("#agent", "w1:p1")
    page.wait_for_timeout(300)
    page.evaluate(f"location.hash = '#t={one['topic']}'")
    page.wait_for_selector(f'#log .msg.ai.flash[data-topic="{one["topic"]}"]', timeout=8000)
    check("画面: 問いが無ければ、ハブの会話を開き、その件の一番新しい報告（同じ件の報告が 2 つ）を「↩ 返信」の状態にする（押すまで送らない）",
          (page.eval_on_selector("#agent", "e => e.value"), page.eval_on_selector("#replyTo", "e => e.textContent"),
           page.eval_on_selector(".msg.ai.flash", "e => e.textContent.includes('件つきの報告')")),
          ("hermes", "↩ lp｜見積 の報告に返信", True))
    page.evaluate(f"location.hash = '#t={NOREP_T}'")
    page.wait_for_function("[...document.querySelectorAll('#log .sys')].some(e => e.textContent.includes('まだ報告の無い件'))", timeout=8000)
    check("画面: 報告がまだ無い件は、そう知らせるだけ。前に選んでいた返信先（別の件の報告）は外す",
          ("まだ報告の無い件: この件の報告は、まだありません" in logs(page), page.eval_on_selector("#replyBar", "e => e.hidden")), (True, True))
    real_hist = s.hermes.TALKER.history
    s.hermes.TALKER.history = lambda *a, **k: (time.sleep(1.5), real_hist(*a, **k))[1]  # ハブの会話を読むのが遅い間に、次の件を開く
    try:
        page.select_option("#agent", "w1:p1")
        page.wait_for_timeout(300)
        page.evaluate(f"location.hash = '#t={one['topic']}'")
        page.wait_for_timeout(300)
        page.evaluate(f"location.hash = '#t={ASK_T}'")
        page.wait_for_timeout(600)
        ask_lit = page.evaluate("[...document.querySelectorAll('#asks .ask.flash')].length")
        page.wait_for_timeout(2000)
    finally:
        s.hermes.TALKER.history = real_hist
    check("画面: 前に開いた件の処理が遅れて戻っても、あとで開いた件の表示を上書きしない（返信先を付けない・報告を光らせない）",
          (ask_lit, page.eval_on_selector("#replyBar", "e => e.hidden"), page.evaluate("document.querySelectorAll('#log .msg.ai.flash').length")), (1, True, 0))
    s.hermes.TALKER.history = lambda *a, **k: (time.sleep(1.5), real_hist(*a, **k))[1]  # 前の件のハブの会話の読み込みが遅れて戻る
    try:
        page.select_option("#agent", "w1:p1")
        page.wait_for_timeout(300)
        page.evaluate(f"location.hash = '#t={one['topic']}'")
        page.wait_for_timeout(300)
        s.hermes.TALKER.history = real_hist
        page.evaluate(f"location.hash = '#t={NOREP_T}'")
        page.wait_for_timeout(2500)
    finally:
        s.hermes.TALKER.history = real_hist
    check("画面: 前に開いた件のハブの会話の読み込みが遅れて戻っても、あとで開いた件の案内を消さない（返信先も付けない）",
          ("まだ報告の無い件: この件の報告は、まだありません" in logs(page), page.eval_on_selector("#replyBar", "e => e.hidden")), (True, True))
    OLDREP_T = s.topics.new("w1:p1", "s1", "lp", "古い報告の件")
    s.hermes.hermes_log("ai", "古い報告の本文", report=True, about="lp｜古い件", pane="w1:p1", topic=OLDREP_T)
    for i in range(45):  # 画面の履歴（直近の 40 件）から押し出す
        s.hermes.hermes_log("ai", f"あとの発言 {i}")
    page.select_option("#agent", "w1:p1")
    page.wait_for_timeout(300)
    page.evaluate(f"location.hash = '#t={OLDREP_T}'")
    page.wait_for_function("!document.getElementById('replyBar').hidden", timeout=8000)
    check("画面: 画面の履歴（直近の 40 件）に出ていない古い報告も、本体が記録から探して返信先にする（そう知らせる）",
          (page.eval_on_selector("#replyTo", "e => e.textContent"), "古い報告の件: この件の報告は古いので画面の履歴には出ていませんが、返信先にしました" in logs(page)),
          ("↩ lp｜古い件 の報告に返信", True))
    SLOW["n"] = 1
    page.select_option("#agent", "w1:p1")
    page.wait_for_timeout(300)
    page.evaluate(f"location.hash = '#t={one['topic']}'")  # 件を開く途中（問いを読む間）に、利用者が相手を選ぶ
    page.wait_for_timeout(300)
    page.select_option("#agent", "w1:p1")
    page.evaluate("document.getElementById('agent').value = 'w1:p1'; document.getElementById('agent').dispatchEvent(new Event('change'))")
    page.wait_for_timeout(2500)
    check("画面: 件を開く途中に利用者が相手を選んだら、件の処理は戻っても相手を替えない・返信先を付けない",
          (page.eval_on_selector("#agent", "e => e.value"), page.eval_on_selector("#replyBar", "e => e.hidden"), SLOW["n"]), ("w1:p1", True, 0))
    page.evaluate(f"location.hash = '#t={one['topic']}'")
    page.wait_for_function("!document.getElementById('replyBar').hidden", timeout=8000)
    s.hermes.hermes_log("ai", "別の件の報告", report=True, about="lp｜別件", pane="w1:p1")
    page.evaluate("loadThread()")
    page.wait_for_function("[...document.querySelectorAll('#log .msg.ai .reply')].length > 0", timeout=8000)
    s.hermes.TALKER.history = lambda *a, **k: (time.sleep(1.5), real_hist(*a, **k))[1]  # 件の会話を読む間に、利用者が別の報告の「↩ 返信」を押す
    try:
        page.evaluate(f"location.hash = '#t={PREV_T}'")
        page.wait_for_timeout(400)
        page.evaluate("[...document.querySelectorAll('#log .msg.ai .reply')].pop().click()")
        chosen = page.eval_on_selector("#replyTo", "e => e.textContent")
        page.wait_for_timeout(2500)
    finally:
        s.hermes.TALKER.history = real_hist
    check("画面: 件を開く途中に利用者が「↩ 返信」で別の報告を選んだら、件の処理は戻っても返信先を上書きしない",
          (chosen != "↩ shop｜前の件 の報告に返信", page.eval_on_selector("#replyTo", "e => e.textContent") == chosen), (True, True))
    page.click("#readBtn")  # 広げて読んでいる間に、通知から件を開く
    page.evaluate("viewer = { back: null, cur: { el: null } }; document.getElementById('viewer').hidden = false; applyInert()")  # 画像を見ている間（の形）
    page.evaluate(f"location.hash = '#t={ASK_T}'")
    page.wait_for_function('[...document.querySelectorAll("#asks .ask.flash")].length === 1', timeout=8000)
    check("画面: 画像を見ている・広げて読んでいる間に件を開いたら、普通の表示に戻して光らせる（押せる）",
          page.evaluate("[document.body.classList.contains('reading'), document.getElementById('viewer').hidden, document.getElementById('asks').closest('[inert]') === null]"),
          [False, True, True])
    check("画面: PC の幅で件を開いてハブに替えたら、左の一覧の印もハブに替える",
          page.evaluate("[...document.querySelectorAll('#sessions .sess.on')].map(e => e.classList.contains('hub'))"), [True])
    SLOW["n"] = 1
    page.evaluate(f"location.hash = '#t={one['topic']}'")  # ハブを選んだまま、問いを読む間に待たせる
    page.wait_for_timeout(300)
    page.evaluate(f"location.hash = '#t={ASK_T}'")
    page.wait_for_timeout(2500)
    check("画面: 問いを読む間に次の件を開いたら、前の件の処理は戻っても返信先を付けない",
          (page.eval_on_selector("#replyBar", "e => e.hidden"), page.evaluate("[...document.querySelectorAll('#asks .ask.flash')].length"), SLOW["n"]), (True, 1, 0))
    page.evaluate("document.querySelectorAll('.flash').forEach(e => e.classList.remove('flash'))")
    SLOW["n"] = 1
    page.evaluate(f"location.hash = '#t={ASK_T}'")  # 問いの件を開き、問いを読む間に待たせる
    page.wait_for_timeout(300)
    page.evaluate(f"location.hash = '#t={one['topic']}'")
    page.wait_for_timeout(2500)
    check("画面: 問いを読む間に次の件を開いたら、前の件の処理は戻っても問いの欄を光らせない（あとで開いた件の返信先は残す）",
          (page.evaluate("[...document.querySelectorAll('#asks .ask.flash')].length"), page.eval_on_selector("#replyTo", "e => e.textContent"), SLOW["n"]),
          (0, "↩ lp｜見積 の報告に返信", 0))
    fetched = []
    page.on("request", lambda r: fetched.append(r.url) if "/api/topic" in r.url else None)
    for bad in ("../x", "t0123456789&x=1", "javascript:alert(1)", "T0123456789"):
        page.evaluate(f"location.hash = {json.dumps('#t=' + bad)}")
        page.wait_for_timeout(200)
    page.evaluate("location.hash = '#t=t0000000000'")
    page.wait_for_function("[...document.querySelectorAll('#log .sys')].some(e => e.textContent.includes('この件は見つかりませんでした'))", timeout=8000)
    check("相手の位置: 形の違う #t= は本体に聞かずに断る。無い件は見つからないと知らせる",
          (len(fetched), logs(page).count("この件のリンクの形が違うので、開けませんでした")), (1, 4))
    page.select_option("#agent", "w1:p1")
    page.wait_for_timeout(300)
    fetched.clear()
    seen = []
    for data in ({"type": "open", "url": "https://evil.example/#t=" + one["topic"]}, {"type": "open", "url": "/#t=../x"}, {"type": "open", "url": 5},
                 {"type": "x", "url": f"/#t={one['topic']}"}, {"type": "open", "url": f"/other?x=1#t={one['topic']}"}, {"type": "open", "url": f"/?x=1#t={one['topic']}"}):
        page.evaluate("d => navigator.serviceWorker.dispatchEvent(new MessageEvent('message', { data: d }))", data)  # 1 つずつ（あとの知らせで上書きされないように）
        page.wait_for_timeout(400)
        seen.append((page.eval_on_selector("#agent", "e => e.value"), len(fetched)))
    check("相手の位置: 通知からの知らせでも、この画面の中の #t=<件> でなければ開かない・本体にも聞かない（ほかのサイトの URL・形の違う件・別の種類の知らせ）",
          (seen, logs(page).count("この件")), ([("w1:p1", 0)] * 6, 0))
    page.evaluate("u => navigator.serviceWorker.dispatchEvent(new MessageEvent('message', { data: { type: 'open', url: u } }))", f"/#t={one['topic']}")
    page.wait_for_function("!document.getElementById('replyBar').hidden", timeout=8000)  # この件の報告は、上で履歴から押し出された（返信先にはする）
    check("画面: 通知を押したとき、開いていた画面は sw.js から渡された件を開く",
          (page.eval_on_selector("#agent", "e => e.value"), page.eval_on_selector("#replyTo", "e => e.textContent")), ("hermes", "↩ lp｜見積 の報告に返信"))
    for target, ready in ((ASK_T, '[...document.querySelectorAll("#asks .ask.flash")].length === 1'), (one["topic"], '!document.getElementById("replyBar").hidden')):
        fresh = browser.new_page()  # 通知・Slack のリンクで、画面を新しく開いたとき（初めの表示では hashchange が起きない）
        fresh.set_viewport_size({"width": 390, "height": 844})
        fresh.goto(BASE + f"/#t={target}")
        opened = True
        try:
            fresh.wait_for_function(ready, timeout=8000)
        except Exception:
            opened = False
        check(f"画面: 件の URL で画面を新しく開いても、その件を開く（{'問いの欄' if target == ASK_T else '報告の返信先'}・URL の #t= は外す）",
              (opened, fresh.evaluate("location.hash")), (True, ""))
        fresh.close()
    fresh = browser.new_page()  # 本体に届かない間に件の URL で開き、つながってから開き直す
    fresh.set_viewport_size({"width": 390, "height": 844})
    fresh.route("**/api/topic*", lambda r: r.abort())
    fresh.goto(BASE + f"/#t={ASK_T}")
    try:
        fresh.wait_for_function("[...document.querySelectorAll('#log .sys')].some(e => e.textContent.includes('つながったらこの件を開きます'))", timeout=8000)
        waited = True
    except Exception:
        waited = False
    fresh.unroute("**/api/topic*")
    fresh.evaluate("catchUp()")  # つながり直したとき（ws.onopen）と同じ取り直し
    try:
        fresh.wait_for_function('[...document.querySelectorAll("#asks .ask.flash")].length === 1', timeout=8000)
        reopened = True
    except Exception:
        reopened = False
    check("画面: 本体に届かない間に件の URL で開いても、件を覚えておき、つながってから開く", (waited, reopened, fresh.evaluate("location.hash")), (True, True, ""))
    fresh.evaluate("""() => {  // 件を開く途中の問いの読み込みに、並んで走る遅い loadAsks（5 秒ごとの更新など）が重なる
      asks.forEach(i => i.el.remove()); asks.clear();
      const of = window.fetch; let n = 0;
      window.fetch = (u, ...a) => {
        if (String(u).startsWith('/api/prompts')) {
          n++;
          if (n === 1) { setTimeout(() => loadAsks(), 0); return new Promise(r => setTimeout(() => r(of(u, ...a)), 300)); }
          if (n === 2) return new Promise(r => setTimeout(() => r(of(u, ...a)), 1500));
        }
        return of(u, ...a);
      };
    }""")
    fresh.evaluate(f"location.hash = '#t={ASK_T}'")
    fresh.wait_for_timeout(2500)
    check("画面: 件を開く途中に並んで走る問いの読み込みがあっても、承認待ちの件は問いの欄を光らせる（報告のほうへ進まない）",
          (fresh.evaluate("[...document.querySelectorAll('#asks .ask.flash')].length"), fresh.eval_on_selector("#replyBar", "e => e.hidden"),
           "この件の報告は" in logs(fresh)), (1, True, False))
    fresh.evaluate("""() => { document.querySelectorAll('.flash').forEach(e => e.classList.remove('flash'));
      const of = window.fetch; let n = 0;  // 問いの一覧の読み込みだけが一度失敗する
      window.fetch = (u, ...a) => (String(u).startsWith('/api/prompts') && ++n === 1) ? Promise.reject(new TypeError('Failed to fetch')) : of(u, ...a);
    }""")
    fresh.evaluate(f"location.hash = '#t={ASK_T}'")
    try:
        fresh.wait_for_function('[...document.querySelectorAll("#asks .ask.flash")].length === 1', timeout=10000)
        lit = True
    except Exception:
        lit = False
    check("画面: 問いの一覧の読み込みだけが失敗しても「問いなし」にせず、報告のほうへ進まない。件を覚えて開き直し、問いの欄を光らせる",
          (lit, "つながったらこの件を開きます" in logs(fresh), fresh.eval_on_selector("#replyBar", "e => e.hidden")), (True, True, True))
    fresh.evaluate("""() => { document.querySelectorAll('.flash').forEach(e => e.classList.remove('flash'));
      const of = window.fetch; let n = 0;  // 件を引く読み込みが、通信が切れて返らないまま止まる
      window.fetch = (u, ...a) => (String(u).startsWith('/api/topic') && ++n === 1) ? new Promise(() => {}) : of(u, ...a);
    }""")
    fresh.evaluate(f"location.hash = '#t={ASK_T}'")
    fresh.wait_for_timeout(500)
    fresh.evaluate("catchUp()")  # つながり直したとき（ws.onopen）と同じ取り直し
    try:
        fresh.wait_for_function('[...document.querySelectorAll("#asks .ask.flash")].length === 1', timeout=8000)
        lit = True
    except Exception:
        lit = False
    check("画面: 件を引く読み込みが止まったままつながり直しても、件を覚えていて開き直す", (lit, fresh.evaluate("pendingTopic")), (True, None))
    fresh.evaluate("""() => { agents = []; agentsSig = '';
      const of = window.fetch; let n = 0;  // 報告の件を開くときに、話し相手の一覧の読み込みだけが一度失敗する
      window.fetch = (u, ...a) => (String(u).startsWith('/api/agents') && ++n === 1) ? Promise.reject(new TypeError('Failed to fetch')) : of(u, ...a);
    }""")
    fresh.evaluate(f"location.hash = '#t={one['topic']}'")
    try:
        fresh.wait_for_function("!document.getElementById('replyBar').hidden", timeout=10000)
        replied = True
    except Exception:
        replied = False
    check("画面: 話し相手の一覧を読めなかったときも「ハブが居ない」とせず、件を覚えて開き直す",
          (replied, "ハブが居ません" in logs(fresh)), (True, False))
    fresh.close()
    fresh = browser.new_page()  # 一覧と声は読めて、件を引く口だけが一度失敗した（つながり直しは起きない）
    fresh.set_viewport_size({"width": 390, "height": 844})
    tries = []
    fresh.route("**/api/topic*", lambda r: (tries.append(1), r.abort() if len(tries) == 1 else r.continue_()))
    fresh.goto(BASE + f"/#t={ASK_T}")
    try:
        fresh.wait_for_function('[...document.querySelectorAll("#asks .ask.flash")].length === 1', timeout=10000)
        reopened = True
    except Exception:
        reopened = False
    check("画面: 件を引く口だけが一度失敗しても、つながっていれば開き直す", (reopened, len(tries) >= 2), (True, True))
    fresh.close()
    sw = page.evaluate(SW_CASES, [SW, BASE, [[f"/#t={one['topic']}", False], ["https://evil.example/", False], ["/#t=../x", False], [f"/x#t={one['topic']}", False],
                                             [f"/?a=1#t={one['topic']}", False], [f"/#t={one['topic']}", True], ["/", True], ["https://evil.example/#t=" + one["topic"], True]]])
    check("sw.js: 通知を押したとき開くのは / か /#t=<件> だけ。開いている画面があれば寄せて件を渡す（ほかのサイト・形の違う場所は / にする）", sw,
          [[["open", f"{BASE}/#t={one['topic']}"]], [["open", f"{BASE}/"]], [["open", f"{BASE}/"]], [["open", f"{BASE}/"]], [["open", f"{BASE}/"]],
           [["post", f"{BASE}/#t={one['topic']}"], "focus"], ["focus"], ["focus"]])
    check("相手の位置: 件の URL で開いても、答えも送りもしない（herdr にも Hermes にも何も送らない・答える口を呼ばない）",
          ([x for x in SENT if x[:2] in (("agent", "send-keys"), ("agent", "prompt"))], ASKED, [u for u in posts if u.startswith(("/api/answer", "/api/hub", "/api/attach"))]),
          ([], [], []))
    browser.close()

srv.should_exit = True
ok_n = sum(r[1] for r in results)
for name, passed, got_, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got_!r}\n    期待:   {want!r}"))
print(f"\n{ok_n}/{len(results)} 件が期待どおり")
sys.exit(0 if ok_n == len(results) else 1)
