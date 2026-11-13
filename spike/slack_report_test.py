"""Slack から頼まれた件の報告を、同じスレッドにも返す（scripts/slack_report.py・server.py の relay_slack・hubctl の origin）の試験。
本物の Slack・Hermes・docker には触れない（docker は偽物の台本に、ハブの報告は差し替え）。hubctl の確かめは 127.0.0.1:8792 に偽の本体を立てる。
確かめること:
- 送り先: 設定した 1 つの DM だけ。違う DM・チャンネル・Slack 以外の窓口・形の違うスレッド・設定が無い/形が違うときは送らない（相手の位置: コンテナの中の Hermes が環境変数を偽っても、ほかへは送れない）
- 本文: MEDIA:（手元のファイルを添付させる印）を添付にならない形に替える・< > を替えて見た目と行き先の違うリンクを作らせない・制御文字と向きを変える文字を空白にする・字数の上限・報告が無ければ画面で確かめるよう書く
- 送り方: コンテナの中の固定のコマンド（hermes send -t slack:<DM>:<スレッド> -f -）に、本文を標準入力で渡す。失敗は短い理由だけ
- 本体: Slack から頼まれた件だけ、報告（ハブの返答）を送る。ハブが居ない・報告を受け取れないときも、画面で確かめるよう送る。報告の途中は、ハブは次の指示を送れない（REPORTING）
- hubctl: Hermes の Slack の会話から呼ばれたときだけ、その DM とスレッドを本体に伝える。本体が送ると答えたときだけ、そう伝える
使い方: uv run spike/slack_report_test.py"""
import asyncio
import http.server
import importlib
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

TMP = tempfile.mkdtemp()
DM = "D0ABCDEF12"
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HUB="claude", VL_HERMES_KEY="k1", VL_SLACK_REPORT_CHAT=DM)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import slack_report as sr  # noqa: E402

results = []
ESC, RLO, ZWSP = chr(0x1B), chr(0x202E), chr(0x200B)


def check(name, got, want):
    results.append((name, got == want, got, want))


# ---------- 送り先 ----------
ok = {"platform": "slack", "chat": DM, "thread": "1794541221.797599"}
check("送り先: 設定した DM とスレッド", sr.origin(ok), (DM, "1794541221.797599"))
check("送り先: スレッドが無ければ DM に", sr.origin({**ok, "thread": ""}), (DM, ""))
check("相手の位置: 違う DM・チャンネル・Slack 以外の窓口には送らない",
      [sr.origin({**ok, "chat": c}) for c in ("D0ZZZZZZZZ", "C0ABCDEF12", "")] + [sr.origin({**ok, "platform": p}) for p in ("api_server", "telegram", None)],
      [None] * 6)
check("相手の位置: 形の違うスレッド（宛先を書き換える文字・別の書き方）は送らない",
      [sr.origin({**ok, "thread": t}) for t in ("1.2:C0OTHER", "slack:C1", "1794541221", "1.2 x", "1.2\n3.4", 1.5, ["1.2"])], [None] * 7)
check("相手の位置: 形の違う値は送らない", [sr.origin(v) for v in (None, "slack", [ok], {"platform": "slack"})], [None] * 4)
for chat in ("", "C0ABCDEF12", "D0ABC; rm"):
    os.environ["VL_SLACK_REPORT_CHAT"] = chat
    importlib.reload(sr)
    check(f"送り先: 設定が無い・形の違う設定（{chat!r}）なら、どこにも送らない", sr.origin({**ok, "chat": chat}), None)
os.environ["VL_SLACK_REPORT_CHAT"] = DM
importlib.reload(sr)

# ---------- 本文 ----------
t = sr.text_for("lp｜見積", "が完了しました", "内訳を出しました。\nMEDIA:/opt/data/.env と media : ~/x.png と **MEDIA:/a.pdf**")
check("本文: 見出しと報告・改行は残す", (t.startswith("【lp｜見積が完了しました】\n内訳を出しました。\n"),), (True,))
check("相手の位置: MEDIA:（添付の印）を、どの書き方でも添付にならない形に替える",
      (__import__("re").search(r"MEDIA\s*:", t, __import__("re").I) is None, t.count("MEDIA："), "/opt/data/.env" in t), (True, 3, True))
t = sr.text_for("p｜t", "が完了しました", "<https://evil.example|見積書>" + ESC + "[2K" + RLO + "逆" + ZWSP)
check("相手の位置: < > を替えて、見た目と行き先の違うリンクを作らせない・制御文字と向きを変える文字は空白に",
      ("<" in t, ">" in t, ESC in t, RLO in t, ZWSP in t, "＜https://evil.example|見積書＞" in t), (False, False, False, False, False, True))
t = sr.text_for("p｜t", "が完了しました", "あ" * 5000)
check("本文: 字数の上限で切る", (len(t), t[-1]), (sr.LIMIT, "…"))
check("本文: 報告が無ければ、画面で確かめるよう書く", "voice-loop の画面で確かめてください" in sr.text_for("p｜t", "が完了しました", "  "), True)

# ---------- 送り方（偽の docker） ----------
fake = Path(TMP) / "docker"
log = Path(TMP) / "docker.log"
fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {log}.args\ncat > {log}.in\nexit ${{FAKE_RC:-0}}\n")
fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
os.environ["VL_DOCKER"] = str(fake)
r = sr.post("hermes", (DM, "1.2"), "本文\n二行目")
args = (Path(f"{log}.args").read_text().splitlines())
check("送り方: コンテナの中の固定のコマンドに、本文を標準入力で渡す",
      (r, args, Path(f"{log}.in").read_text()),
      ("ok", ["exec", "-i", "-u", "10000:10000", "hermes", "timeout", "--kill-after=5", "30", sr.HERMES, "send", "-q", "-t", f"slack:{DM}:1.2", "-f", "-"], "本文\n二行目"))
sr.post("hermes", (DM, ""), "x")
check("送り方: スレッドが無ければ DM に", Path(f"{log}.args").read_text().splitlines()[-3], f"slack:{DM}")
os.environ["FAKE_RC"] = "1"
check("送り方: 失敗は短い理由だけ", sr.post("hermes", (DM, "1.2"), "x"), "終了コード 1")
del os.environ["FAKE_RC"]
os.environ["VL_DOCKER"] = str(Path(TMP) / "nothing")
check("送り方: docker が無ければ送らない", sr.post("hermes", (DM, "1.2"), "x").startswith("動かせません"), True)

# ---------- 本体 ----------
import server as s  # noqa: E402

posted = []
s.slack_report.post = lambda container, dest, text, wait=45: posted.append((container, dest, text)) or "ok"
s.push_all = lambda *a, **k: asyncio.sleep(0, "none")
asked = []


async def fake_target():
    return {"pane": "hub"}


async def fake_ask(hub, note):
    asked.append(s.REPORTING)
    return ANSWER


async def fake_end(state, reply, about, saved):
    return False


s.HUB.report_target = fake_target
s.HUB.report_begin = lambda headline, about: False
s.HUB.report_end = fake_end
s.ask = fake_ask


async def run_report(d, kind="done"):
    out = await s._report(d, kind)
    await asyncio.sleep(0.05)
    for _ in range(50):
        if not [t for t in s._tasks if not t.done()]:
            break
        await asyncio.sleep(0.02)
    return out


def d_of(slack):
    s.SENT_GEN["w1:p1"] = 7
    return {"pane": "w1:p1", "cwd": "/x/lp", "project": "lp", "title": "見積", "text": "内訳を出して", "slack": slack, "sent_gen": 7}


ANSWER = ("done", "内訳を出しました（全文は画面で）")
posted.clear()
check("本体: Slack から頼まれた件は、ハブの報告を同じスレッドにも送る",
      (asyncio.run(run_report(d_of((DM, "1.2")))), posted),
      ("ok", [(s.hermes.HERMES_CONTAINER, (DM, "1.2"), "【lp｜見積が完了しました】\n内訳を出しました（全文は画面で）")]))
check("本体: 報告はハブが REPORTING の中で作ったもの（その間、次の指示は送れない）", asked[-1], True)
posted.clear()
asyncio.run(run_report(d_of(None)))
check("本体: Slack から頼まれていない件は送らない", posted, [])
async def report_after_new_send():
    """報告がハブの順番を待つ間に、同じセッションへ Slack 以外から次の指示が送られた（世代が進んだ）"""
    d = {**d_of((DM, "1.2")), "sent_gen": 1}
    s.SENT_GEN["w1:p1"] = 1
    s.PANE_LOCKS.clear()
    lock = s.pane_lock("hub")
    await lock.acquire()
    job = asyncio.create_task(run_report(d))
    await asyncio.sleep(0.05)
    s.SENT_GEN["w1:p1"] = 2  # 新しい件（Slack からではない）
    lock.release()
    return await job


posted.clear()
asyncio.run(report_after_new_send())
check("相手の位置: 報告を待つ間に同じセッションへ次の指示が送られたら、ハブの報告（新しい件の結果）は Slack に送らない",
      [(p[1], "内訳を出しました" in p[2], "次の指示が送られました" in p[2]) for p in posted], [((DM, "1.2"), False, True)])
posted.clear()
asyncio.run(run_report(d_of((DM, "1.2"))))  # d_of は、送ろうとした回数を件と同じにする
check("本体: 世代が同じなら、ハブの報告を送る", ["内訳を出しました" in p[2] for p in posted], [True])
A_LP = {"pane": "w1:p1", "cwd": "/x/lp", "kind": "claude", "session": None, "status": "idle", "title": "見積", "name": "", "hub": False}
s.list_agents = lambda: [A_LP]


async def report_while_new_send(herdr_answer, block):
    """報告がハブの順番を待つ間に、同じセッションへ（Slack 以外から）次の指示を送る。herdr は答えを返す前に止める（block）か、すぐ返す"""
    d = d_of((DM, "1.2"))
    gate = threading.Event()

    def fake_herdr(*a, **k):
        if block:
            gate.wait(10)
        return herdr_answer

    s.herdr = fake_herdr
    s.PANE_LOCKS.clear()  # 鍵は作った回のイベントループに結び付くので、回ごとに作り直す
    lock = s.pane_lock("hub")
    await lock.acquire()
    rep = asyncio.create_task(run_report(d))
    await asyncio.sleep(0.05)
    snd = asyncio.create_task(s.dispatch(A_LP, "別の作業", "send"))
    await asyncio.sleep(0.05)
    lock.release()
    await rep  # 新しい指示の送りは、まだ herdr の答えを待っている（block のとき）
    gate.set()
    return await snd


for name, answer, block in (("送って答えを待っている間", {"result": {}}, True), ("送ったが届いたか分からない（herdr が答えない）", {}, False),
                            ("送ったが断られた", {"error": {"code": "x"}}, False)):
    posted.clear()
    sent = asyncio.run(report_while_new_send(answer, block))
    check(f"相手の位置: 報告を待つ間に同じセッションへ次の指示を{name}も、ハブの報告は Slack に送らない",
          [("内訳を出しました" in p[2], "次の指示が送られました" in p[2]) for p in posted], [(False, True)])
s.herdr = lambda *a, **k: {"result": {}}
ANSWER = ("timeout", "")
posted.clear()
asyncio.run(run_report(d_of((DM, "1.2"))))
check("本体: 報告を受け取れなかったときも、画面で確かめるよう送る", ("画面で確かめてください" in posted[0][2] if posted else None), True)


async def no_hub():
    return None


s.HUB.report_target = no_hub
posted.clear()
asyncio.run(run_report(d_of((DM, "1.2")), "blocked"))
check("本体: ハブが居ないときも、何が起きたかを送る", (posted[0][2].startswith("【lp｜見積が承認・入力待ちで止まっています】") if posted else None), True)
s.HUB.report_target = fake_target

from fastapi.testclient import TestClient  # noqa: E402

AGENTS = {"w1:p1": {"pane": "w1:p1", "cwd": "/x/lp", "kind": "claude", "session": None, "status": "idle", "title": "見積", "name": "", "hub": False}}
s.list_agents = lambda: list(AGENTS.values())
s._worker = lambda pane: AGENTS.get(pane)
s.herdr = lambda *a, **k: {"result": {}}


async def no_report(d, kind):
    return None


s.report = no_report
with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    r1 = c.post("/api/hub/send", json={"pane": "w1:p1", "text": "内訳を出して", "origin": ok}).json()
    r2 = c.post("/api/hub/send", json={"pane": "w1:p1", "text": "内訳を出して", "origin": {**ok, "chat": "D0ZZZZZZZZ"}}).json()
    r3 = c.post("/api/hub/send", json={"pane": "w1:p1", "text": "内訳を出して"}).json()
    s.REPORTING = True
    r4 = c.post("/api/hub/send", json={"pane": "w1:p1", "text": "続けて送れ", "origin": ok}).json()
    s.REPORTING = False
# 画面・声から、同じセッションへ直接話しかけて頼んだとき（/ws）。前の Slack の件の報告がハブの順番を待っている間に
direct = []


async def ask_by_pane(agent_, note):
    if agent_["pane"] == "hub":
        asked.append(s.REPORTING)
        return ("done", "内訳を出しました（全文は画面で）")
    direct.append(note)
    return ("done", "画面から頼んだ別の作業の結果")


async def nothing2(*a, **k):
    return None


s.ask = ask_by_pane
s.speak_to = nothing2
posted.clear()
with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    s.PANE_LOCKS.clear()
    lock = s.pane_lock("hub")
    c.portal.call(lock.acquire)
    d_old = d_of((DM, "1.2"))
    fut = c.portal.start_task_soon(run_report, d_old)
    with c.websocket_connect("ws://127.0.0.1:8790/ws") as w:
        w.send_json({"type": "text", "text": "別の作業をして", "pane": "w1:p1", "speaker": 0})
        for _ in range(200):
            m = w.receive_json()
            if m.get("type") == "reply":
                break
    c.portal.call(lock.release)
    fut.result(timeout=10)
check("相手の位置: 報告を待つ間に、画面・声から同じセッションへ直接頼んでも、ハブの報告（新しい件の結果かもしれない）は Slack に送らない",
      (direct, [("内訳を出しました" in p[2], "次の指示が送られました" in p[2]) for p in posted]), (["別の作業をして"], [(False, True)]))
s.ask = fake_ask
check("本体: 設定した DM から頼まれたときだけ、報告を Slack にも送ると答える", (r1.get("slack"), r2.get("slack"), r3.get("slack")), (True, False, False))
check("本体: 報告の途中は、Slack からでも次の指示を送れない", r4.get("reason"), "reporting")

# ---------- hubctl ----------
got = []


class Fake(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        body = json.dumps({"ok": True, "project": "lp", "title": "見積", "was_working": False, "task": "x", "slack": FAKE_SLACK}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


with socket.socket() as probe:
    busy = probe.connect_ex(("127.0.0.1", 8792)) == 0
if busy:
    check("8792 が空いている（hubctl の試験を立てられる）", busy, False)
else:
    srv = http.server.HTTPServer(("127.0.0.1", 8792), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_SESSION_")}
    base.update(VL_HUB_URL="http://127.0.0.1:8792/api/hub", VL_HUB_KEY_FILE="")

    def hubctl(env, *args):
        return subprocess.run(["uv", "run", "--quiet", "--script", str(ROOT / "hubctl"), *args], capture_output=True, text=True, env={**base, **env}, timeout=120)

    slack_env = {"HERMES_SESSION_PLATFORM": "slack", "HERMES_SESSION_CHAT_ID": DM, "HERMES_SESSION_THREAD_ID": "1.2", "HERMES_SESSION_MESSAGE_ID": "3.4"}
    FAKE_SLACK = True
    out = hubctl(slack_env, "send", "w1:p1", "内訳を出して").stdout
    check("hubctl: Slack の会話から呼ばれたら、その DM とスレッドを本体に伝え、Slack にも報告が届くと伝える",
          (got[-1].get("origin"), "この Slack のスレッドにも報告を送ります" in out), ({"platform": "slack", "chat": DM, "thread": "1.2"}, True))
    hubctl({**slack_env, "HERMES_SESSION_THREAD_ID": ""}, "send", "w1:p1", "x")
    check("hubctl: スレッドが無ければ、利用者の発言をスレッドの親にする", got[-1]["origin"]["thread"], "3.4")
    hubctl({**slack_env, "HERMES_SESSION_PLATFORM": "api_server"}, "send", "w1:p1", "x")
    hubctl({}, "send", "w1:p1", "x")
    check("hubctl: Slack 以外の窓口・環境変数が無いときは伝えない", ["origin" in g for g in got[-2:]], [False, False])
    FAKE_SLACK = False
    out = hubctl(slack_env, "send", "w1:p1", "x").stdout
    check("hubctl: 本体が送らないと答えたら、Slack に届くとは言わない", "Slack のスレッド" in out, False)
    FAKE_SLACK = True
    hubctl(slack_env, "start", "lp", "調べて")
    check("hubctl: start でも伝える", got[-1].get("origin", {}).get("chat"), DM)
    srv.shutdown()

ok_n = sum(r[1] for r in results)
for name, passed, got_, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got_!r}\n    期待:   {want!r}"))
print(f"\n{ok_n}/{len(results)} 件が期待どおり")
sys.exit(0 if ok_n == len(results) else 1)
