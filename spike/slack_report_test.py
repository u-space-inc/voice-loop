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
import types
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
r = sr.post((DM, "1.2"), "本文\n二行目", container="hermes")
args = (Path(f"{log}.args").read_text().splitlines())
check("送り方: コンテナの中の固定のコマンドに、本文を標準入力で渡す",
      (r, args, Path(f"{log}.in").read_text()),
      ("ok", ["exec", "-i", "-u", "10000:10000", "hermes", "timeout", "--kill-after=5", "30", sr.HERMES, "send", "-q", "-t", f"slack:{DM}:1.2", "-f", "-"], "本文\n二行目"))
sr.post((DM, ""), "x", container="hermes")
check("送り方: スレッドが無ければ DM に", Path(f"{log}.args").read_text().splitlines()[-3], f"slack:{DM}")
os.environ["FAKE_RC"] = "1"
check("送り方: 失敗は短い理由だけ", sr.post((DM, "1.2"), "x", container="hermes"), "終了コード 1")
del os.environ["FAKE_RC"]
os.environ["VL_DOCKER"] = str(Path(TMP) / "nothing")
check("送り方: docker が無ければ送らない", sr.post((DM, "1.2"), "x", container="hermes").startswith("動かせません"), True)

# ---------- 本体 ----------
import server as s  # noqa: E402

posted = []
CUR = {"path": None}  # いまのセッションの記録（偽の jsonl_path が返す）
A_LP = {"pane": "w1:p1", "cwd": "/x/lp", "kind": "claude", "session": "s1", "status": "idle", "title": "見積", "name": "", "hub": False}
s.list_agents = lambda: [A_LP]
s.jsonl_path = lambda a: Path(CUR["path"]) if CUR["path"] and a.get("session") == "s1" else None
s.slack_report.post = lambda dest, text, wait=45, container=None: posted.append((s.slack_report.CONTAINER, dest, text)) or "ok"
s.push_all = lambda *a, **k: asyncio.sleep(0, "none")
asked = []


async def fake_target():
    return {"pane": "hub"}


async def no_hub_target():
    return None


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


def transcript(*turns):
    """Claude Code のセッション記録（JSONL）の形で書く。turns は ("user" | "assistant", 文)"""
    p = Path(tempfile.mkstemp(suffix=".jsonl", dir=TMP)[1])
    rows = [{"type": "user", "message": {"role": "user", "content": t}} if k == "user" else
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": t}]}} for k, t in turns]
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return str(p)


def d_of(slack, path="ok", text="内訳を出して"):
    s.SENT_GEN["w1:p1"] = 7
    if path == "ok":
        path = transcript(("user", "内訳を出して"), ("assistant", "内訳を出しました"))
    CUR["path"] = path
    return {"pane": "w1:p1", "cwd": "/x/lp", "project": "lp", "title": "見積", "text": "内訳を出して", "slack": slack, "sent_gen": 7,
            "offset": 0, "path": path, "session": "s1"}


ANSWER = ("done", "内訳を出しました（全文は画面で）")
posted.clear()
check("本体: Slack から頼まれた件は、ハブの報告を同じスレッドにも送る",
      (asyncio.run(run_report(d_of((DM, "1.2")))), posted),
      ("ok", [("hermes", (DM, "1.2"), "【lp｜見積が完了しました】\n内訳を出しました（全文は画面で）")]))
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
# 端末に直接打たれた発言（本体を通らない）は、セッションの記録で見る
for name, path, want in (
        ("この件の指示のあとに、端末から別の発言があれば", transcript(("user", "内訳を出して"), ("assistant", "内訳を出しました"), ("user", "別の案件の作業をして"),
                                                     ("assistant", "別の案件の結果")), "記録で確かめられませんでした"),
        ("記録でこの件の指示を見つけられなければ", transcript(("user", "ほかの作業"), ("assistant", "ほかの結果")), "記録で確かめられませんでした"),
        ("記録が無ければ", None, "記録で確かめられませんでした"),
        ("指示のあとに <system-reminder> で始まる発言があれば（画面の履歴では飛ばす形でも）",
         transcript(("user", "内訳を出して"), ("assistant", "内訳を出しました"), ("user", "<system-reminder>別の作業をして"), ("assistant", "別の結果")),
         "記録で確かめられませんでした"),
        ("指示のあとに <task-notification> や <bash-input> で始まる行があれば",
         transcript(("user", "内訳を出して"), ("user", "<task-notification>x</task-notification>"), ("assistant", "内訳を出しました")), "記録で確かめられませんでした"),
        ("記録を読めなければ（読む直前に消えた・読めない場所）", TMP, "記録で確かめられませんでした"),
        ("記録にあるのが、先頭は同じで続きの違う指示なら",
         transcript(("user", "内訳を出して" + "。" * 70 + "別の案件のほう"), ("assistant", "別の結果")), "記録で確かめられませんでした")):
    posted.clear()
    asyncio.run(run_report(d_of((DM, "1.2"), path)))
    check(f"相手の位置: {name}、ハブの報告は Slack に送らない", [("内訳を出しました（全文" in p[2], want in p[2]) for p in posted], [(False, True)])

# 端末でセッションが作り直された（同じ場所に別のセッション）・記録に読めない行がある
posted.clear()
d_new = d_of((DM, "1.2"))
A_LP["session"] = "s2"  # 報告を待つ間に、端末で作り直された。ハブが読むのは新しいセッション
asyncio.run(run_report(d_new))
A_LP["session"] = "s1"
check("相手の位置: 報告を待つ間に端末でセッションが作り直されたら、ハブの報告は Slack に送らない",
      [("内訳を出しました（全文" in p[2], "記録で確かめられませんでした" in p[2]) for p in posted], [(False, True)])
broken = Path(transcript(("user", "内訳を出して"), ("assistant", "内訳を出しました")))
broken.write_text(broken.read_text() + '{"type": "user", "message": {"content": "書きかけ\n')
posted.clear()
asyncio.run(run_report(d_of((DM, "1.2"), str(broken))))
check("相手の位置: 指示のあとに読めない行（壊れた・書きかけ）があれば、ハブの報告は Slack に送らない",
      [("内訳を出しました（全文" in p[2], "記録で確かめられませんでした" in p[2]) for p in posted], [(False, True)])

# 道具の結果と、サブエージェントの行は数えない（この件の作業の一部）
tr = Path(transcript(("user", "内訳を出して"), ("assistant", "内訳を出しました")))
rows = [json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}),
        json.dumps({"type": "user", "isSidechain": True, "message": {"role": "user", "content": "サブエージェントへの依頼"}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "内訳を出しました"}]}})]
tr.write_text(tr.read_text() + "\n".join(rows) + "\n")
posted.clear()
asyncio.run(run_report(d_of((DM, "1.2"), str(tr))))
check("本体: 指示のあとが道具の結果とサブエージェントの行だけなら、ハブの報告を送る", ["内訳を出しました（全文" in p[2] for p in posted], [True])

# 同じセッションへの前の件と一つの返答にまとまり、報告を省くとき（LAST_DONE が同じ）も、この件のスレッドに知らせる
def merged_watch():
    tpath = Path(transcript(("user", "内訳を出して"), ("assistant", "前の件とまとめて片づけました")))
    d = {**d_of((DM, "1.2"), str(tpath)), "gen": 5, "was_working": False, "task": None}
    s.WATCH_GEN["w1:p1"] = 5
    s.LAST_DONE["w1:p1"] = (s.last_turn_id(tpath), s.last_exchange(s.read_lines(tpath, tail_bytes=600_000))[1])
    agent_ = {**A_LP}
    real_list_, real_path_, real_async = s.list_agents, s.jsonl_path, s.asyncio
    fast_ = types.ModuleType("fast_asyncio2")
    fast_.__dict__.update(asyncio.__dict__)
    fast_.sleep = lambda t, *a: asyncio.sleep(t / 1000, *a)
    s.list_agents, s.jsonl_path, s.asyncio = (lambda: [agent_]), (lambda a: tpath), fast_

    async def go():
        await s._watch_delegation(d)
        await asyncio.sleep(0.05)
        for _ in range(50):
            if not [t for t in s._tasks if not t.done()]:
                break
            await asyncio.sleep(0.02)
    try:
        asyncio.run(go())
    finally:
        s.list_agents, s.jsonl_path, s.asyncio = real_list_, real_path_, real_async


async def broken_watch():
    real = s._watch_delegation

    async def boom(d):
        raise RuntimeError("壊れた")
    s._watch_delegation = boom
    try:
        task_id = s.task_new({**d_of((DM, "1.2")), "pane": "w1:p1"}, "send")
        await s.watch_delegation({**d_of((DM, "1.2")), "task": task_id})
        await asyncio.sleep(0.05)
        for _ in range(50):
            if not [t for t in s._tasks if not t.done()]:
                break
            await asyncio.sleep(0.02)
    finally:
        s._watch_delegation = real


posted.clear()
asyncio.run(broken_watch())
check("本体: 見張りが途中で失敗したときも、Slack のスレッドに知らせる", [("途中で失敗しました" in p[2], "画面で確かめてください" in p[2]) for p in posted], [(True, True)])

# 報告の途中で失敗したとき: Slack に送る前の失敗なら定型文を 1 通、送ったあとの失敗なら重ねない
async def report_in_watch(d):
    real = s._watch_delegation

    async def one_report(d_):
        await s.report(d_, "done")
    s._watch_delegation = one_report
    try:
        await s.watch_delegation(d)
        await asyncio.sleep(0.05)
        for _ in range(50):
            if not [t for t in s._tasks if not t.done()]:
                break
            await asyncio.sleep(0.02)
    finally:
        s._watch_delegation = real


async def boom_target():
    raise RuntimeError("ハブに届かない")


s.HUB.report_target = boom_target
posted.clear()
d_fail = d_of((DM, "1.2"))
d_fail["task"] = s.task_new(d_fail, "send")
asyncio.run(report_in_watch(d_fail))
s.HUB.report_target = fake_target
check("本体: 完了の報告を作る途中（Slack に送る前）で失敗したら、定型文を 1 通送る（台帳は先に完了になっていても）",
      [("報告の途中で失敗しました" in p[2], "画面で確かめてください" in p[2]) for p in posted], [(True, True)])


class FakeClient:
    voice = False
    visible = True
    speaker = None

    async def send(self, **k):
        return None


async def boom_speak(*a, **k):
    raise RuntimeError("読み上げで失敗")


real_speak = s.speak_to
s.speak_to = boom_speak
fc = FakeClient()
s.CLIENTS.add(fc)
ANSWER = ("done", "内訳を出しました（全文は画面で）")
posted.clear()
d_after = d_of((DM, "1.2"))
d_after["task"] = s.task_new(d_after, "send")
asyncio.run(report_in_watch(d_after))
s.CLIENTS.discard(fc)
s.speak_to = real_speak
check("本体: Slack に送ったあとで失敗しても、重ねて送らない", [("内訳を出しました（全文" in p[2], "失敗しました" in p[2]) for p in posted], [(True, False)])

# 途中経過（承認・入力待ち）を Slack に送ったあとで見張りが失敗したら、その失敗も知らせる
async def blocked_then_boom(d):
    real = s._watch_delegation

    async def run(d_):
        await s.report(d_, "blocked")
        raise RuntimeError("記録を読めない")
    s._watch_delegation = run
    try:
        await s.watch_delegation(d)
        await asyncio.sleep(0.05)
        for _ in range(50):
            if not [t for t in s._tasks if not t.done()]:
                break
            await asyncio.sleep(0.02)
    finally:
        s._watch_delegation = real


A_LP["status"] = "blocked"
posted.clear()
d_blk = d_of((DM, "1.2"))
d_blk["task"] = s.task_new(d_blk, "send")
asyncio.run(blocked_then_boom(d_blk))
A_LP["status"] = "idle"
check("本体: 途中経過（承認・入力待ち）を送ったあとで見張りが失敗したら、その失敗も知らせる",
      sorted(("承認・入力待ちで止まっています" in p[2], "見張りが途中で失敗しました" in p[2]) for p in posted), [(False, True), (True, False)])  # 送る順は裏の処理で前後する

# 立てた直後に送った件（送ったときにセッション ID が無い）: 決まった ID の記録の最初の発言がこの件の指示なら、その ID で確かめて本文を送る
def spawned_watch(first_user):
    tpath = Path(transcript(("user", first_user), ("assistant", "内訳を出しました")))
    d = {**d_of((DM, "1.2"), str(tpath)), "session": None, "gen": 9, "was_working": False, "task": None}
    s.WATCH_GEN["w1:p1"] = 9
    s.LAST_DONE.pop("w1:p1", None)
    real_path_, real_async, real_to = s.jsonl_path, s.asyncio, s.WATCH_TIMEOUT
    s.WATCH_TIMEOUT = 1  # 報告のあとの続きを見張る時間（この試験では 1 秒）
    fast_ = types.ModuleType("fast_asyncio3")
    fast_.__dict__.update(asyncio.__dict__)
    fast_.sleep = lambda t, *a: asyncio.sleep(t / 1000, *a)
    s.jsonl_path, s.asyncio = (lambda a: tpath if a.get("session") == "s1" else None), fast_

    async def go():
        await s._watch_delegation(d)
        await asyncio.sleep(0.05)
        for _ in range(50):
            if not [t for t in s._tasks if not t.done()]:
                break
            await asyncio.sleep(0.02)
    try:
        asyncio.run(go())
    finally:
        s.jsonl_path, s.asyncio, s.WATCH_TIMEOUT = real_path_, real_async, real_to
    return d


posted.clear()
d_sp = spawned_watch("内訳を出して")
check("相手の位置: 送ったときにセッション ID が分からなかった件は、あとから ID を覚え直さず、本文を送らない（定型文だけ）",
      (d_sp.get("session"), ["内訳を出しました（全文" in p[2] for p in posted]), (None, [False]))

# Slack から立てるときは、最初の指示を送る前に、セッション ID が見えるまで少し待つ
calls = {"n": 0}


def agents_late_session():
    calls["n"] += 1
    return [{**A_LP, "session": "s1" if calls["n"] >= 3 else None}]


real_list_ = s.list_agents
s.list_agents = agents_late_session
got_wait = asyncio.run(s.spawned_agent("w1:p1", wait=5))
calls["n"] = 0
got_now = asyncio.run(s.spawned_agent("w1:p1", wait=0))
s.list_agents = real_list_
check("本体: Slack から立てたときは、セッション ID が見えるまで（上限まで）待ってから送る。Slack からでなければ待たない",
      (got_wait.get("session"), got_now.get("session")), ("s1", None))

# 送ったときに ID の無かった件: 作業中に見えたセッションが、待つ間に同じ文の指示で始まる別のセッションに入れ替わっても、新しいほうは覚えない
def swapped_watch():
    orig = Path(transcript(("user", "内訳を出して"), ("assistant", "途中")))
    repl = Path(transcript(("user", "内訳を出して"), ("assistant", "別のセッションの結果")))
    d = {**d_of((DM, "1.2"), str(orig)), "session": None, "gen": 11, "was_working": False, "task": None}
    s.WATCH_GEN["w1:p1"] = 11
    s.LAST_DONE.pop("w1:p1", None)
    seq = iter([{**A_LP, "session": "orig", "status": "working"}] * 2)
    real_list_, real_path_, real_async, real_to = s.list_agents, s.jsonl_path, s.asyncio, s.WATCH_TIMEOUT

    def agents():
        nxt = next(seq, None)
        return [nxt or {**A_LP, "session": "repl", "status": "idle"}]
    s.list_agents = agents
    s.jsonl_path = lambda a: {"orig": orig, "repl": repl}.get(a.get("session"))
    s.WATCH_TIMEOUT = 1
    fast_ = types.ModuleType("fast_asyncio4")
    fast_.__dict__.update(asyncio.__dict__)
    fast_.sleep = lambda t, *a: asyncio.sleep(t / 1000, *a)
    s.asyncio = fast_

    async def go():
        await s._watch_delegation(d)
        await asyncio.sleep(0.05)
        for _ in range(50):
            if not [t for t in s._tasks if not t.done()]:
                break
            await asyncio.sleep(0.02)
    try:
        asyncio.run(go())
    finally:
        s.list_agents, s.jsonl_path, s.asyncio, s.WATCH_TIMEOUT = real_list_, real_path_, real_async, real_to
    return d


posted.clear()
d_sw = swapped_watch()
check("相手の位置: 送ったときに ID の無かった件で、作業中に見えたセッションが同じ文で始まる別のセッションに入れ替わっても、新しいほうを覚えず本文も送らない",
      (d_sw.get("session"), ["別のセッションの結果" in p[2] or "内訳を出しました（全文" in p[2] for p in posted]), (None, [False]))

# ハブが居ないときの完了の報告（定型文）のあとで見張りが失敗しても、失敗を重ねて送らない
async def done_without_hub_then_boom(d):
    real = s._watch_delegation

    async def run(d_):
        await s.report(d_, "done")
        raise RuntimeError("次の読み取りで失敗")
    s._watch_delegation = run
    try:
        await s.watch_delegation(d)
        await asyncio.sleep(0.05)
        for _ in range(50):
            if not [t for t in s._tasks if not t.done()]:
                break
            await asyncio.sleep(0.02)
    finally:
        s._watch_delegation = real


s.HUB.report_target = no_hub_target
posted.clear()
d_nh = d_of((DM, "1.2"))
d_nh["task"] = s.task_new(d_nh, "send")
asyncio.run(done_without_hub_then_boom(d_nh))
s.HUB.report_target = fake_target
check("本体: ハブが居ないときの完了の知らせ（定型文）のあとで見張りが失敗しても、失敗を重ねない",
      [("完了しました" in p[2], "失敗しました" in p[2]) for p in posted], [(True, False)])

posted.clear()
merged_watch()
check("本体: 前の件と一つの返答にまとまって報告を省くときも、この件の Slack のスレッドに知らせる（本文は載せない）",
      [(p[1], "まとめて片づきました" in p[2], "画面で確かめてください" in p[2]) for p in posted], [((DM, "1.2"), True, True)])

# 報告をせずに見張りが終わるとき（次の指示に引き継がれた・閉じられた・時間切れ）も、スレッドに知らせる
fast = types.ModuleType("fast_asyncio")
fast.__dict__.update(asyncio.__dict__)
_real_sleep = asyncio.sleep
fast.sleep = lambda t, *a: _real_sleep(t / 1000, *a)


async def watch_end(setup):
    d = {**d_of((DM, "1.2")), "gen": 3, "was_working": False, "task": None}
    s.WATCH_GEN["w1:p1"] = 3
    setup(d)
    real, s.asyncio = s.asyncio, fast
    try:
        await s._watch_delegation(d)
        await _real_sleep(0.05)
        for _ in range(50):
            if not [t for t in s._tasks if not t.done()]:
                break
            await _real_sleep(0.02)
    finally:
        s.asyncio = real


real_list, real_timeout = s.list_agents, s.WATCH_TIMEOUT
for name, setup, want in (
        ("同じセッションへの次の指示に引き継がれた", lambda d: s.WATCH_GEN.__setitem__("w1:p1", 4), "次の指示に引き継がれました"),
        ("ハブが閉じた", lambda d: (setattr(s, "list_agents", lambda: []), s.CLOSED.add("w1:p1")), "ハブが閉じました"),
        ("時間切れ", lambda d: setattr(s, "WATCH_TIMEOUT", 0), "見張りをやめました")):
    posted.clear()
    asyncio.run(watch_end(setup))
    s.list_agents, s.WATCH_TIMEOUT = real_list, real_timeout
    s.CLOSED.discard("w1:p1")
    check(f"本体: 報告をせずに見張りが終わるとき（{name}）も、Slack のスレッドに知らせる（結果の本文は載せない）",
          [(p[1], want in p[2], "画面で確かめてください" in p[2]) for p in posted], [((DM, "1.2"), True, True)])

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

AGENTS = {"w1:p1": A_LP}
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
SLACK_GEN = []
_real_start_watch = s.start_watch


SLACK_OVERLAP = []


def record_start_watch(d, kind="send"):
    SLACK_GEN.append(d.get("sent_gen"))
    SLACK_OVERLAP.append(d.get("overlap"))
    d["task"] = s.task_new(d, kind)  # 見張りは動かさない（この試験では報告を直接呼ぶ）


s.start_watch = record_start_watch
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
# 逆の順: 画面の依頼がセッションの順番を待つ間に、Slack から同じセッションへ頼み、そのあとで画面の依頼が届く
direct.clear()
posted.clear()
with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    s.PANE_LOCKS.clear()
    wlock = s.pane_lock("w1:p1")
    c.portal.call(wlock.acquire)
    with c.websocket_connect("ws://127.0.0.1:8790/ws") as w:
        w.send_json({"type": "text", "text": "画面からの別の作業", "pane": "w1:p1", "speaker": 0})
        for _ in range(200):
            if w.receive_json().get("type") == "status":
                break
        r_slack = c.portal.call(s.dispatch, A_LP, "内訳を出して", "send", (DM, "1.2"))
        c.portal.call(wlock.release)
        for _ in range(200):
            if w.receive_json().get("type") == "reply":
                break
    d_slack = {"pane": "w1:p1", "cwd": "/x/lp", "project": "lp", "title": "見積", "text": "内訳を出して", "slack": (DM, "1.2"),
               "sent_gen": SLACK_GEN[-1]}
    c.portal.call(run_report, d_slack)
check("相手の位置: 画面の依頼が順番を待つ間に Slack から頼み、そのあとで画面の依頼が届いても、ハブの報告は Slack に送らない",
      (direct, [("内訳を出しました" in p[2], "次の指示が送られました" in p[2]) for p in posted]), (["画面からの別の作業"], [(False, True)]))
# 重なった送り: 先に送り始めた別の指示（Slack 以外）が herdr に受けられる前に、Slack の件を送る。先の指示があとから届いても、本文は載せない
gate_a = threading.Event()


def herdr_slow_a(*a, **k):
    if "先の作業" in a:
        gate_a.wait(10)
        return {}  # 届いたか分からない
    return {"result": {}}


async def overlapped_dispatch():
    s.herdr = herdr_slow_a
    a_job = asyncio.create_task(s.dispatch(A_LP, "先の作業", "send"))
    await asyncio.sleep(0.1)
    rb = await s.dispatch(A_LP, "内訳を出して", "send", (DM, "1.2"))
    gate_a.set()
    await a_job
    return rb, dict(s.INFLIGHT)


SLACK_GEN.clear()
SLACK_OVERLAP.clear()
posted.clear()
rb, inflight_after = asyncio.run(overlapped_dispatch())
# Slack の件（2 つ目に start_watch に来たもの。1 つ目は先の作業）の記録のまま、報告を作る
asyncio.run(run_report({"pane": "w1:p1", "cwd": "/x/lp", "project": "lp", "title": "見積", "text": "内訳を出して", "slack": (DM, "1.2"),
                        "sent_gen": SLACK_GEN[0], "overlap": SLACK_OVERLAP[0]}))
check("相手の位置: 先に送り始めた別の指示が届く前に Slack の件を送ったら、重なった件として、ハブの報告は Slack に送らない（先の指示があとから届いて答えが無くても）",
      (rb.get("slack"), SLACK_OVERLAP, [("内訳を出しました" in p[2], "次の指示が送られました" in p[2]) for p in posted], inflight_after.get("w1:p1")),
      (True, [True], [(False, True)], 0))
s.herdr = lambda *a, **k: {"result": {}}

OVERLAPS = []
_rsw = s.start_watch


def record_overlap(d, kind="send"):
    OVERLAPS.append(d.get("overlap"))
    _rsw(d, kind)


s.start_watch = record_overlap


async def two_in_a_row():
    await s.dispatch(A_LP, "一つ目", "send")
    await s.dispatch(A_LP, "二つ目", "send", (DM, "1.2"))


asyncio.run(two_in_a_row())
check("本体: 前の送りが herdr に受けられてから送った件は、重なった件にしない（ふつうに続けて頼んだとき）", OVERLAPS[-2:], [False, False])

# 画面・声からの直接の発言が返事を待っている間に、Slack から同じセッションへ頼んだら、重なった件
gate_ws = threading.Event()


async def ask_slow(agent_, note):
    if agent_["pane"] == "hub":
        return ("done", "内訳を出しました（全文は画面で）")
    await asyncio.to_thread(gate_ws.wait, 10)
    return ("done", "画面から頼んだ作業の結果")


s.ask = ask_slow
with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    s.PANE_LOCKS.clear()
    with c.websocket_connect("ws://127.0.0.1:8790/ws") as w:
        w.send_json({"type": "text", "text": "画面から頼む", "pane": "w1:p1", "speaker": 0})
        for _ in range(200):
            if w.receive_json().get("type") == "status":
                break
        for _ in range(100):
            if s.INFLIGHT.get("w1:p1"):
                break
            __import__("time").sleep(0.02)
        c.portal.call(s.dispatch, A_LP, "内訳を出して", "send", (DM, "1.2"))
        gate_ws.set()
        for _ in range(200):
            if w.receive_json().get("type") == "reply":
                break
check("相手の位置: 画面・声からの発言が返事を待っている間に Slack から頼んだ件は、重なった件にする（途中の数は戻る）",
      (OVERLAPS[-1], s.INFLIGHT.get("w1:p1")), (True, 0))
# 取り消し: 画面からの依頼を「止める」で取り消しても、送る処理は裏で続く。その間に Slack から頼んだ件は重なった件
gate_ws2 = threading.Event()


async def ask_slow2(agent_, note):
    if agent_["pane"] == "hub":
        return ("done", "内訳を出しました（全文は画面で）")
    await asyncio.to_thread(gate_ws2.wait, 10)
    return ("done", "画面から頼んで止めた作業の結果")


s.ask = ask_slow2
with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    s.PANE_LOCKS.clear()
    with c.websocket_connect("ws://127.0.0.1:8790/ws") as w:
        w.send_json({"type": "text", "text": "画面から頼んで止める", "pane": "w1:p1", "speaker": 0})
        for _ in range(200):
            if w.receive_json().get("type") == "status":
                break
        for _ in range(100):
            if s.INFLIGHT.get("w1:p1"):
                break
            __import__("time").sleep(0.02)
        w.send_json({"type": "stop"})
        for _ in range(200):
            if w.receive_json().get("type") == "stopped":
                break
        still = s.INFLIGHT.get("w1:p1")
        c.portal.call(s.dispatch, A_LP, "内訳を出して", "send", (DM, "1.2"))
        gate_ws2.set()
    for _ in range(200):
        if not s.INFLIGHT.get("w1:p1"):
            break
        __import__("time").sleep(0.02)
check("相手の位置: 画面からの依頼を止めても、送る処理が終わるまでは途中に数え、その間に Slack から頼んだ件は重なった件にする",
      (still, OVERLAPS[-1], s.INFLIGHT.get("w1:p1")), (1, True, 0))

gate_d = threading.Event()


def herdr_slow_c(*a, **k):
    if "取り消される作業" in a:
        gate_d.wait(10)
    return {"result": {}}


async def cancelled_dispatch():
    s.herdr = herdr_slow_c
    job = asyncio.create_task(s.dispatch(A_LP, "取り消される作業", "send"))
    await asyncio.sleep(0.1)
    job.cancel()
    await asyncio.sleep(0.05)
    mid = s.INFLIGHT.get("w1:p1")
    await s.dispatch(A_LP, "内訳を出して", "send", (DM, "1.2"))
    gate_d.set()
    for _ in range(200):
        if not s.INFLIGHT.get("w1:p1"):
            break
        await asyncio.sleep(0.02)
    return mid


mid = asyncio.run(cancelled_dispatch())
s.herdr = lambda *a, **k: {"result": {}}
check("相手の位置: ハブ経由の送りの待ちが取り消されても、herdr が受けるまでは途中に数え、その間の Slack の件は重なった件にする",
      (mid, OVERLAPS[-1], s.INFLIGHT.get("w1:p1")), (1, True, 0))
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
