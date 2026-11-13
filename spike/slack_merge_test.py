"""同じセッションに続けて頼んだ件を、まとめて 1 つの報告にする（server.py の merge_open・chain_dest・relay_slack・scan_task、scripts/slack_report.py）の試験。
本物の Slack・Hermes・docker・herdr には触れない（Slack への送りと herdr とハブは差し替え）。
確かめること:
- 前の件の完了を報告する前に、同じセッションへ次の件を送ったら、次の件にまとめる。報告は 1 回、一番新しい Slack のスレッドに、まとめた依頼を並べて送る
- 行き先のスレッドが変わるときは、前のスレッドに「次の依頼のスレッドで報告します」と一言送る。Slack と voice-loop が混ざっても、Slack のスレッドに届く
- 声と画面だけの件は、今までどおり（まとめない・ハブへの知らせも変えない）
- 本文を載せるのは、記録で、まとめた指示がその順に見つかり、そのあとに別の発言が無いときだけ。作業中に届いた発言（queued_command）も数える。裏の作業の完了の知らせは数えない
- 相手の位置: 途中に端末から別の発言・画面からの直接の発言・重なった送り・別のセッションがあれば本文を載せない。完了の報告を始めた件・見張りを終えた件にはまとめない。まとめと報告が入れ違わない
- 送り先は、slack_report.origin が決めた DM だけ（偽った送り元は、まとめても Slack の届け先にならない）
使い方: uv run spike/slack_merge_test.py"""
import asyncio
import json
import os
import sys
import tempfile
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


def check(name, got, want):
    results.append((name, got == want, got, want))


T1, T2, T3 = (DM, "1.1"), (DM, "2.2"), (DM, "3.3")

# ---------- 文 ----------
t = sr.text_for("lp｜見積", "が完了しました", "両方やりました", asks=[("内訳を出して", sr.OTHER_THREAD), ("期限も調べて", sr.HERE)])
check("文: まとめた報告は、見出しに件数、依頼を古い順に並べ、空行のあとに本文",
      t, "【lp｜見積が完了しました】（2 件まとめて）\n・内訳を出して（前のスレッドから）\n・期限も調べて（このスレッドから）\n\n両方やりました")
t = sr.text_for("p｜t", "が完了しました", "x", asks=[(f"依頼{i}" + "あ" * 80, sr.HERE) for i in range(7)])
check("文: 並べる依頼は新しい 5 件まで（ほかの件数を書く）・1 件は 50 字で切る",
      (t.count("\n・依頼"), "・ほか 2 件" in t, "依頼0" in t, "依頼6" in t, max(len(x) for x in t.splitlines())), (5, True, False, True, 1 + 50 + len("（このスレッドから）")))
t = sr.text_for("p｜t", "が完了しました", "x", asks=[("MEDIA:/opt/data/.env を <https://evil.example|見て>", sr.FROM_SCREEN)])
check("相手の位置: 依頼の文も、添付の印とリンクの形を替える", ("MEDIA:" in t, "MEDIA：" in t, "<" in t, "＜https://evil.example|見て＞" in t), (False, True, False, True))
t = sr.pointer_text("lp｜見積", "期限も調べて。" + "い" * 80 + " MEDIA:/x <a|b>")
check("文: 前のスレッドへの一言は、行き先と次の依頼（50 字まで）だけ",
      (t.startswith("【lp｜見積 への依頼は、次の依頼とまとめて、そちらのスレッドで報告します】\n次の依頼：「期限も調べて。"), len(t.splitlines()[1]) <= len("次の依頼：「」") + 50,
       "MEDIA:" in t, "<" in t), (True, True, False, False))

# ---------- 記録の読み方 ----------
import server as s  # noqa: E402


def U(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def Q(text, mode="prompt", kind="human", **extra):
    """作業中に届いた発言（Claude Code は user の行でなく、付け足しとして残す）"""
    return {"type": "attachment", "attachment": {"type": "queued_command", "prompt": text, "commandMode": mode, "origin": {"kind": kind}, **extra}}


def AI(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def TOOL():
    return {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}


def log_of(*rows) -> Path:
    p = Path(tempfile.mkstemp(suffix=".jsonl", dir=TMP)[1])
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return p


def append(p: Path, *rows):
    with p.open("a") as f:
        f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


check("記録: 作業中に送った指示（付け足しとして残る）も、この件の指示として見つける（1 件だけのとき）",
      s.only_this_task(log_of(Q("内訳を出して"), TOOL(), AI("出しました")), 0, "内訳を出して"), True)
check("相手の位置: この件の指示のあと、作業中に端末から打たれた発言があれば、本文を載せない",
      s.only_this_task(log_of(U("内訳を出して"), Q("別の案件のことをして"), AI("両方やりました")), 0, "内訳を出して"), False)
check("相手の位置: 作業中に別のエージェントから届いた発言も、別の発言として数える",
      s.only_this_task(log_of(U("内訳を出して"), Q("相手からの依頼", kind="peer"), AI("x")), 0, "内訳を出して"), False)
check("記録: 裏の作業の完了の知らせ（task-notification の付け足し）は数えない",
      [s.only_this_task(log_of(U("内訳を出して"), q, AI("x")), 0, "内訳を出して")
       for q in (Q("<task-notification>..</task-notification>", mode="task-notification", kind="task-notification"),
                 {"type": "attachment", "attachment": {"type": "queued_command", "prompt": "x", "commandMode": "task-notification"}})], [True, True])
check("記録: ほかの付け足し（日付・環境など）は数えない",
      s.only_this_task(log_of(U("内訳を出して"), {"type": "attachment", "attachment": {"type": "date", "date": "2026-11-13"}}, AI("x")), 0, "内訳を出して"), True)
check("相手の位置: 形の読めない付け足しがあれば、本文を載せない",
      s.only_this_task(log_of(U("内訳を出して"), {"type": "attachment", "attachment": "?"}, AI("x")), 0, "内訳を出して"), False)
check("記録: 作業中に届いた発言の文が部品の並びでも読む",
      s.only_this_task(log_of(Q([{"type": "text", "text": "内訳を"}, {"type": "image"}, {"type": "text", "text": "出して"}]), AI("x")), 0, "内訳を 出して"), True)
check("記録: まとめた指示がその順に見つかり、あとに別の発言が無ければ本文を載せる（2 件目は作業中に届いた形）",
      s.only_these(log_of(U("内訳を出して"), TOOL(), Q("期限も調べて"), AI("両方やりました")), 0, ["内訳を出して", "期限も調べて"]), True)
check("相手の位置: まとめた指示の間に、端末から別の発言があれば本文を載せない",
      s.only_these(log_of(U("内訳を出して"), Q("別の案件のことをして"), Q("期限も調べて"), AI("x")), 0, ["内訳を出して", "期限も調べて"]), False)
check("相手の位置: まとめた指示の順が違えば本文を載せない",
      s.only_these(log_of(Q("期限も調べて"), U("内訳を出して"), AI("x")), 0, ["内訳を出して", "期限も調べて"]), False)
check("相手の位置: まとめた指示の一部しか見つからなければ本文を載せない",
      s.only_these(log_of(U("内訳を出して"), AI("x")), 0, ["内訳を出して", "期限も調べて"]), False)

# ---------- 本体（送る・まとめる・報告する） ----------
posted, notes, started = [], [], []
AG = {"pane": "w1:p1", "cwd": "/x/lp", "kind": "claude", "session": "s1", "status": "working", "title": "見積", "name": "", "hub": False}
LOG = {"p": None}
s.list_agents = lambda: [AG]
s.jsonl_path = lambda a: LOG["p"] if a.get("session") == "s1" else None
s.herdr = lambda *a, **k: {"result": {}}
s.slack_report.post = lambda dest, text, wait=45, container=None: posted.append((dest, text)) or "ok"
s.push_all = lambda *a, **k: asyncio.sleep(0, "none")
REPLY = {"v": ("done", "両方やりました（ハブの報告）")}


async def fake_target():
    return {"pane": "hub"}


async def fake_ask(hub, note):
    notes.append(note)
    return REPLY["v"]


async def fake_end(state, reply, about, saved):
    return False


s.HUB.report_target = fake_target
s.HUB.report_begin = lambda headline, about: False
s.HUB.report_end = fake_end
s.ask = fake_ask


def record_start_watch(d, kind="send"):
    d["task"] = s.task_new(d, kind)  # 見張りは動かさない（報告は試験から直接呼ぶ）
    started.append(d)


s.start_watch = record_start_watch


def fresh():
    for m in (s.OPEN_WATCH, s.SENT_GEN, s.WATCH_GEN, s.INFLIGHT, s.LAST_DONE):
        m.clear()
    posted.clear()
    notes.clear()
    started.clear()
    s.PANE_LOCKS.clear()
    s.REPORT_IDLE = None
    LOG["p"] = log_of(U("前からの話"), AI("前からの返事"))


async def settle():
    await asyncio.sleep(0.05)
    for _ in range(100):
        if not [t for t in s._tasks if not t.done()]:
            break
        await asyncio.sleep(0.02)


async def send(text, slack=None, row=None):
    """ハブ経由で送る。row は、届いた指示が記録に残る形（既定は user の行）"""
    await s.dispatch(AG, text, "send", slack)
    append(LOG["p"], (row or U)(text))
    await settle()
    return started[-1]


async def finish(d, reply_rows=(AI("両方やりました"),), kind="done"):
    append(LOG["p"], *reply_rows)
    d["path"] = str(LOG["p"])  # 見張りが、送ったときと同じセッションなら覚える
    await s.report(d, kind)
    await settle()


def heads(texts):
    return [x.splitlines()[0] for x in texts]


# 1) Slack の 2 つのスレッドから、同じセッションへ続けて（2 件目は作業中に届く）
async def two_threads():
    fresh()
    a = await send("内訳を出して", T1)
    first = list(posted)
    b = await send("期限も調べて", T2, Q)
    pointer = list(posted)
    posted.clear()
    await finish(b)
    return a, b, (first, pointer)


a, b, pointer = asyncio.run(two_threads())
check("まとめる: 1 件目では何も送らず、2 件目を送った時点で、前のスレッドに次の依頼のスレッドで報告すると一言送る（1 回だけ）",
      (pointer[0], [(p[0], "そちらのスレッドで報告します" in p[1], "次の依頼：「期限も調べて」" in p[1]) for p in pointer[1]]), ([], [(T1, True, True)]))
check("まとめる: 報告は 1 回、一番新しいスレッドに、まとめた依頼を並べて本文ごと送る",
      [(p[0], p[1]) for p in posted],
      [(T2, "【lp｜見積が完了しました】（2 件まとめて）\n・内訳を出して（前のスレッドから）\n・期限も調べて（このスレッドから）\n\n両方やりました（ハブの報告）")])
check("まとめる: ハブへの知らせに、まとめた 2 件の指示と、最初の指示から読む数（吹き出し 2 つ）を書く",
      ("「内訳を出して」「期限も調べて」" in notes[-1], "-n 2 --full" in notes[-1], "どの指示の結果かが分かるように" in notes[-1]), (True, True, True))
check("まとめる: 前の件は次の件にまとめた印、次の件の台帳はまとめて報告すると書く",
      (a.get("_merged_into") is b, [e["text"] for e in b["chain"]], "まとめて報告します" in s.TASKS[b["task"]]["note"]), (True, ["内訳を出して"], True))


# 2) Slack のあとに voice-loop から（前の件の Slack のスレッドに届く。一言は送らない）
async def slack_then_screen():
    fresh()
    await send("内訳を出して", T1)
    b = await send("期限も調べて", None, Q)
    await finish(b)


asyncio.run(slack_then_screen())
check("混ざる: Slack のあとに voice-loop から頼んだら、Slack のスレッドに、voice-loop からの依頼とまとめて送る（行き先は変わらないので一言は無い）",
      [(p[0], p[1].splitlines()[1:3], "両方やりました（ハブの報告）" in p[1]) for p in posted],
      [(T1, ["・内訳を出して（このスレッドから）", "・期限も調べて（voice-loop から）"], True)])


# 3) voice-loop のあとに Slack から
async def screen_then_slack():
    fresh()
    await send("内訳を出して")
    b = await send("期限も調べて", T2, Q)
    await finish(b)


asyncio.run(screen_then_slack())
check("混ざる: voice-loop のあとに Slack から頼んだら、Slack のスレッドに、前の依頼の一行も添えて送る",
      [(p[0], p[1].splitlines()[1:3]) for p in posted], [(T2, ["・内訳を出して（voice-loop から）", "・期限も調べて（このスレッドから）"])])


# 4) 声と画面だけ: 今までどおり
async def screen_only():
    fresh()
    await send("内訳を出して")
    b = await send("期限も調べて", None, Q)
    await finish(b)
    return b


b = asyncio.run(screen_only())
check("声と画面だけの件は今までどおり: Slack に送らない・ハブへの知らせはこの件だけ・読むのは 1 つ",
      (posted, "あなたが送った指示「期限も調べて」" in notes[-1], "内訳を出して" in notes[-1], "-n 1 --full" in notes[-1], "まとめて" in s.TASKS[b["task"]]["note"]),
      ([], True, False, True, False))


# 5) 同じスレッドから 2 回
async def same_thread():
    fresh()
    pre = None
    await send("内訳を出して", T1)
    pre = list(posted)
    b = await send("期限も調べて", T1, Q)
    await finish(b)
    return pre


pre = asyncio.run(same_thread())
check("同じスレッドから続けて頼んだら、一言は送らず、そのスレッドに 1 回",
      (pre, [(p[0], p[1].splitlines()[1:3]) for p in posted]), ([], [(T1, ["・内訳を出して（このスレッドから）", "・期限も調べて（このスレッドから）"])]))


# 6) 3 つのスレッド
async def three_threads():
    fresh()
    await send("一つ目", T1)
    await send("二つ目", T2, Q)
    c = await send("三つ目", T3, Q)
    ptr = list(posted)
    posted.clear()
    await finish(c)
    return ptr


ptr = asyncio.run(three_threads())
check("3 件: 行き先が移るたびに、前の行き先に一言（1 つ目と 2 つ目のスレッドに 1 回ずつ）",
      [(p[0], "次の依頼：「" + ("二つ目" if p[0] == T1 else "三つ目") + "」" in p[1]) for p in ptr], [(T1, True), (T2, True)])
check("3 件: 報告は 3 つ目のスレッドに 1 回、3 件を並べる",
      [(p[0], p[1].splitlines()[:4]) for p in posted],
      [(T3, ["【lp｜見積が完了しました】（3 件まとめて）", "・一つ目（前のスレッドから）", "・二つ目（前のスレッドから）", "・三つ目（このスレッドから）"])])


# 7) 2 件目が、前の件の答えのあとに別のターンとして届いた（作業中でなかった）: 読む数を 4 にする
async def separate_turns():
    fresh()
    await send("内訳を出して", T1)
    append(LOG["p"], AI("内訳を出しました"))
    b = await send("期限も調べて", T2)
    await finish(b, (AI("期限は月末です"),))


asyncio.run(separate_turns())
check("読む数: 2 件目が別のターンなら、最初の指示から 4 つ（指示・返事・指示・返事）を読ませる。本文も送る",
      ("-n 4 --full" in notes[-1], [("両方やりました（ハブの報告）" in p[1]) for p in posted if p[0] == T2]), (True, [True]))


# ---------- 相手の位置 ----------
def body_of(p):
    return "両方やりました（ハブの報告）" in p[1]


async def terminal_between():
    fresh()
    await send("内訳を出して", T1)
    append(LOG["p"], Q("端末から別の作業を頼む"))
    b = await send("期限も調べて", T2, Q)
    await finish(b)


asyncio.run(terminal_between())
check("相手の位置: まとめた指示の間に端末から別の発言があれば、本文を載せない（依頼は並べる）",
      [(p[0], body_of(p), "記録で確かめられませんでした" in p[1], "・期限も調べて（このスレッドから）" in p[1]) for p in posted if p[0] == T2], [(T2, False, True, True)])


async def screen_direct_between():
    fresh()
    await send("内訳を出して", T1)
    s.SENT_GEN["w1:p1"] += 2  # 画面・声から同じセッションへ直接話した（/ws は送る前と順番を取ったあとに 2 回進める）
    b = await send("期限も調べて", T2, Q)
    await finish(b)


asyncio.run(screen_direct_between())
check("相手の位置: まとめた指示の間に、画面・声から直接の発言があれば（送った回数が続いていない）、記録が整って見えても本文を載せない",
      [(body_of(p), "voice-loop の画面で確かめてください" in p[1]) for p in posted if p[0] == T2], [(False, True)])


async def overlapped_first():
    fresh()
    s.INFLIGHT["w1:p1"] = 1  # 別の送りが途中（1 件目が重なった件）
    await send("内訳を出して", T1)
    s.INFLIGHT["w1:p1"] = 0
    b = await send("期限も調べて", T2, Q)
    await finish(b)


asyncio.run(overlapped_first())
check("相手の位置: まとめた前の件が重なって送られていたら、本文を載せない", [body_of(p) for p in posted if p[0] == T2], [False])


async def other_session():
    fresh()
    a = await send("内訳を出して", T1)
    AG["session"] = "s2"  # 端末でセッションが作り直された
    b = await send("期限も調べて", T2)
    AG["session"] = "s1"
    return a, b


a, b = asyncio.run(other_session())
check("相手の位置: 前の件と別のセッションに送った件は、まとめない（前の件は今までどおり引き継ぎとして扱う）",
      (a.get("_merged_into"), b.get("chain")), (None, None))


async def closed_prev(mark):
    fresh()
    a = await send("内訳を出して", T1)
    mark(a)
    b = await send("期限も調べて", T2)
    return a, b, list(posted)


for name, mark in (("完了の報告を始めた", lambda a: a.update(_closed=True)), ("見張りを終えた", lambda a: a.update(_closed=True))):
    a, b, ps = asyncio.run(closed_prev(mark))
    check(f"相手の位置: {name}件には、まとめない（一言も送らない）", (a.get("_merged_into"), b.get("chain"), ps), (None, None, []))


async def report_starts_closed():
    fresh()
    a = await send("内訳を出して", T1)
    job = asyncio.create_task(s.report(a, "done"))  # 報告を始める（ハブの順番を待つ）
    await asyncio.sleep(0)
    closed = a.get("_closed")
    b = await send("期限も調べて", T2)
    await job
    await settle()
    return closed, b


closed, b = asyncio.run(report_starts_closed())
check("入れ違わない: 完了の報告を始めた時点で、まとめない印が立つ（そのあとに来た件は、まとめずに別に報告）", (closed, b.get("chain")), (True, None))


async def finally_closes():
    fresh()
    a = await send("内訳を出して", T1)
    real = s._watch_delegation

    async def quick(d):
        return None
    s._watch_delegation = quick
    try:
        await s.watch_delegation(a)
    finally:
        s._watch_delegation = real
    b = await send("期限も調べて", T2)
    return a, b


a, b = asyncio.run(finally_closes())
check("入れ違わない: 見張りが終わった件は、台帳から外れ、まとめない", (a.get("_closed"), b.get("chain")), (True, None))


async def forged_origin():
    """コンテナの中の Hermes が、別の DM と偽って続けて送った。偽った送り元は届け先にならない"""
    fresh()
    await send("内訳を出して", T1)
    b = await s.dispatch(AG, "期限も調べて", "send", sr.origin({"platform": "slack", "chat": "D0ZZZZZZZZ", "thread": "9.9"}))
    append(LOG["p"], Q("期限も調べて"))
    await settle()
    d = started[-1]
    await finish(d)
    return b


b = asyncio.run(forged_origin())
check("相手の位置: 別の DM と偽って続けて送っても、そこには送らない（届け先は、設定した DM の前のスレッドのまま。偽った件は voice-loop からの依頼として並ぶ）",
      (b.get("slack"), sorted({p[0] for p in posted}), [p[1].splitlines()[2] for p in posted]), (False, [T1], ["・期限も調べて（voice-loop から）"]))


# 前の件の見張りが、一覧を読む間に次の件にまとめられた（入れ違い）: 前の件は報告しない
def raced_watch(merge):
    fresh()
    p = log_of(U("内訳を出して"), AI("出しました"))
    LOG["p"] = p
    d = {"pane": "w1:p1", "cwd": "/x/lp", "project": "lp", "title": "見積", "text": "内訳を出して", "offset": 0, "was_working": True,
         "gen": 1, "sent_gen": 1, "overlap": False, "slack": T1, "session": "s1"}
    s.WATCH_GEN["w1:p1"] = 1
    s.SENT_GEN["w1:p1"] = 1
    d["task"] = s.task_new(d, "send")
    reported = []
    real_list, real_async, real_report, real_to = s.list_agents, s.asyncio, s.report, s.WATCH_TIMEOUT
    s.WATCH_TIMEOUT = 1  # 報告のあとの続きを見張る時間（この試験では 1 秒）

    def agents():
        if merge and not d.get("_merged_into"):  # 一覧を読む間に、次の件が送られてまとめられた
            s.WATCH_GEN["w1:p1"] = 2
            d["_merged_into"] = {"text": "期限も調べて"}
        return [{**AG, "status": "idle"}]

    async def rec_report(d_, kind):
        reported.append(kind)
    fast = types.ModuleType("fast_asyncio")
    fast.__dict__.update(asyncio.__dict__)
    fast.sleep = lambda t, *a: asyncio.sleep(t / 1000, *a)
    s.list_agents, s.asyncio, s.report = agents, fast, rec_report

    async def go():
        await asyncio.wait_for(s._watch_delegation(d), 10)
        await settle()
    try:
        asyncio.run(go())
    finally:
        s.list_agents, s.asyncio, s.report, s.WATCH_TIMEOUT = real_list, real_async, real_report, real_to
    return reported, s.TASKS[d["task"]]["status"], s.TASKS[d["task"]]["note"], list(posted)


check("入れ違わない: 一覧を読む間に次の件にまとめられたら、前の件は報告せずに引き上げる（Slack にも送らない）",
      raced_watch(True), ([], "replaced", "同じセッションへの次の指示と、まとめて報告します", []))
check("比べ: まとめられなければ、前の件はそのまま報告する", raced_watch(False)[0], ["done"])


# 途中経過（承認・入力待ち）も、まとめた届け先に送る
async def blocked_chain():
    fresh()
    await send("内訳を出して", T1)
    b = await send("期限も調べて", None, Q)
    AG["status"] = "blocked"
    try:
        await finish(b, (), "blocked")
    finally:
        AG["status"] = "working"


asyncio.run(blocked_chain())
check("まとめる: 途中経過（承認・入力待ち）も、まとめた届け先のスレッドに送る",
      [(p[0], p[1].splitlines()[0]) for p in posted], [(T1, "【lp｜見積が承認・入力待ちで止まっています】（2 件まとめて）")])

ok_n = sum(r[1] for r in results)
for name, passed, got_, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got_!r}\n    期待:   {want!r}"))
print(f"\n{ok_n}/{len(results)} 件が期待どおり")
sys.exit(0 if ok_n == len(results) else 1)
