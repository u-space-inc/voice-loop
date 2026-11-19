"""判断待ちの受け箱（画面の 💡。scripts/hermes_inbox.py・/api/inbox・済んだ・戻す・あとで・件への返信）の試験。
Hermes の置き場は一時のフォルダ、コンテナの中の記録の道具は差し替える（docker・常駐の 8790・本物の Hermes には触れない）。
画面での出し方は spike/inbox_ui_test.py。
相手の位置から見ること（相手は外の文面に乗せられた Hermes で、置き場の記録と見張りの状態をいつでも書ける）:
- 置き場は読むだけ（どの操作のあとも、置き場のファイルは 1 つも変わらない）。記録・フォルダが symlink・ほかの名前からも指されている・大きすぎるなら読まない
- 文は 1 行・上限の字数で切り、制御文字・向きを変える文字は空白にする。形の合わない行は飛ばす
- 「元を開く」は本体が口と id の形から作る（Gmail は 16 進の id だけ。Hermes の文の中のリンクは使わない）
- 「済んだ」は押してから決めた秒のあいだ手元に置くだけ（戻すと何もしない）。そのあとでコンテナの中の道具に付けさせ、記録に印が見えたときだけ済みとする
- 「あとで」は本体の状態フォルダ（0600）に覚え、時刻が来たら一覧の上に戻す
- 件への返信は、記録にある件だけ。返信の台帳に載せ、返信先の問いは空（承認に答える・閉じるには使えない）。ことばが空でも送れる（中身と案を聞く文にする）
- 口はどれも画面だけ（コンテナからは使えない）
使い方: uv run spike/inbox_test.py"""
import asyncio
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

TMP = tempfile.mkdtemp()
HOME = Path(TMP) / "hermes"
(HOME / "voice-loop").mkdir(parents=True)
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HERMES_KEY="k1", VL_HERMES_URL="http://127.0.0.1:9", VL_HERMES_HOME=str(HOME))
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from fastapi.testclient import TestClient  # noqa: E402

import server as s  # noqa: E402
from origin_guard import problem  # noqa: E402

ib = s.hermes.hermes_inbox


async def nothing(*a, **k):
    return None


s.boot_ensure_hub = s.keep_gateway_token = s.watch_blocked = nothing  # 起動の裏の仕事は動かさない（ハブ・herdr・docker に触れない）
s.HUB.watchers = lambda: []
ITEMS = HOME / "voice-loop" / "items.jsonl"
STATUS = HOME / "voice-loop" / "teams-status.json"
results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


NOW = time.time()
JST = dt.timezone(dt.timedelta(hours=9))


def add(n, kind="judge", source="mail", rid=None, title=None, at=None, **kw):
    return {"op": "add", "at": NOW - 600 if at is None else at, "source": source, "id": rid or f"{n:016x}", "project": kw.pop("project", "shop"),
            "kind": kind, "title": title or f"件{n}", "note": kw.pop("note", f"要ること{n}"), "prepared": kw.pop("prepared", ""), "n": n, **kw}


def write(rows):
    ITEMS.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def snapshot():
    return sorted((str(p.relative_to(HOME)), p.stat().st_mtime_ns, p.stat().st_size) for p in HOME.rglob("*") if p.is_file())


def ns(d):
    return [x["n"] for x in d["items"]]


# ---------- 一覧 ----------
today_start = dt.datetime.fromtimestamp(NOW, JST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
write([
    add(1, at=NOW - 3600, title="新しい判断待ち"),
    add(2, at=NOW - 7200),
    add(3, at=NOW - 5 * 86400, title="古い判断待ち"),
    add(4, at=NOW - 1800), {"op": "done", "at": NOW - 100, "n": 4, "note": "", "by": "user"},
    add(5, at=NOW - 6 * 86400), {"op": "done", "at": NOW - 5000, "n": 5, "by": "ai", "note": "済"}, {"op": "reopen", "at": NOW - 300, "n": 5, "note": "まだ"},
    add(6, kind="fyi", at=max(today_start + 1, NOW - 60), title="今日の宛て"),
    add(7, kind="fyi", at=today_start - 3600, title="昨日の宛て"),
    add(8, kind="info", at=NOW - 60),
    add(9, kind="baseline", at=NOW - 60),
    "壊れた行",
    {"op": "add", "at": "きのう", "source": "mail", "id": "x", "kind": "judge", "title": "時刻が文字", "n": 10},
    {"op": "add", "at": NOW, "source": "mail", "id": "x", "kind": "judge", "title": "番号が真偽", "n": True},
    add(11, at=NOW - 120, title="改行\nと\x1b[31m色‮と" + "長" * 200, project={"x": 1}, note="あ" * 900),
])
ITEMS.write_text(ITEMS.read_text().replace('"壊れた行"', "{壊れた"))
before = snapshot()
d = ib.inbox(NOW)
check("片づいていない判断待ちを新しい順（戻したものは戻した時刻から）。済・情報・既読扱い・形の合わない行は出さない", ns(d), [11, 5, 1, 2, 3])
check("記録してから 3 日より前のものは old の印を付ける（消さない）", [x["old"] for x in d["items"]], [False, False, False, False, True])
check("今日届いた宛ての知らせ（対応不要）は別に出し、昨日のものは出さない", [x["n"] for x in d["fyi"]], [6])
x11 = d["items"][0]
check("文は 1 行・上限の字数で切り、制御文字・向きを変える文字は空白にする",
      (len(x11["title"]), "\n" in x11["title"] or "\x1b" in x11["title"] or "‮" in x11["title"], x11["title"].startswith("改行 と [31m色 と"), len(x11["note"])),
      (80, False, True, 600))
check("案件の欄が文字でなくても、文字にそろえて出す", isinstance(x11["project"], str), True)
check("見張りが止まっていなければ帯は出さない", d["watch"], [])

write([add(12, at=NOW - 60, moved="きのう"), add(13, at=NOW - 120, moved=float("inf") if False else 1e30), add(14, at=NOW - 180)])
d = ib.inbox(NOW)
check("追加の行に書かれた戻した印（moved）の値は使わない（壊れた値 1 つで一覧全体を読めなくしない）", (ns(d), d["problem"]), ([12, 13, 14], ""))
ITEMS.write_text("")

ITEMS.write_text("[" * 50000 + "\n" + json.dumps(add(15, at=NOW - 60)) + "\n")
d = ib.inbox(NOW)
check("入れ子が深すぎる行（RecursionError）も、形の違う行として飛ばす（一覧全体を落とさない）", (ns(d), d["problem"]), ([15], ""))
STATUS.write_text("[" * 50000)
check("…見張りの状態のファイルが入れ子が深すぎても、帯を出さずに落ちない", ib.inbox(NOW)["watch"], [])
STATUS.unlink()


class Gone(dict):  # 「ある」と答えたあとで、別の処理が外した（一覧は別のスレッドで作るので、戻す・付けると重なる）
    def __contains__(self, k):
        return True


keep_p, ib.PENDING = ib.PENDING, Gone()
try:
    d = ib.inbox(NOW)
    check("一覧を作る間に、戻す・付けるが予約を外しても落ちない（予約は 1 回だけ取って見る）", ns(d), [15])
except KeyError:
    check("一覧を作る間に、戻す・付けるが予約を外しても落ちない（予約は 1 回だけ取って見る）", "KeyError", [15])
ib.PENDING = keep_p
ITEMS.write_text("")

# ---------- 置き場を安全に読む ----------
write([add(1, at=NOW - 3600)])
real = ITEMS.read_bytes()
ITEMS.unlink()
other = Path(TMP) / "other.jsonl"
other.write_bytes(real)
ITEMS.symlink_to(other)
d = ib.inbox(NOW)
check("記録が symlink なら読まない（理由を出す）", (d["items"], bool(d["problem"])), ([], True))
ITEMS.unlink()
os.link(other, ITEMS)
check("記録がほかの名前からも指されていれば読まない", bool(ib.inbox(NOW)["problem"]), True)
ITEMS.unlink()
vl = HOME / "voice-loop"
vl.rename(HOME / "vl-real")
(HOME / "vl-real" / "items.jsonl").write_bytes(real)
vl.symlink_to(HOME / "vl-real")
check("記録のフォルダが symlink なら読まない", bool(ib.inbox(NOW)["problem"]), True)
vl.unlink()
(HOME / "vl-real").rename(vl)
ITEMS.write_bytes(b"x" * (ib.ITEMS_MAX + 1))
check("大きすぎる記録は読まない", bool(ib.inbox(NOW)["problem"]), True)
ITEMS.unlink()
check("記録がまだ無ければ、理由なしで空", (ib.inbox(NOW)["items"], ib.inbox(NOW)["problem"]), ([], ""))
ITEMS.write_bytes(real)

# ---------- 元を開くリンク ----------
L = ib.link
check("Gmail のリンクは 16 進の id のときだけ本体が作る",
      [L({"source": "mail", "id": "19a2b3c4d5e6f708"}), L({"source": "mail", "id": "javascript:alert(1)"}), L({"source": "mail", "id": "../../x"}), L({"source": "mail", "id": "ABCDEF0123456789"})],
      [{"url": "https://mail.google.com/mail/u/0/#all/19a2b3c4d5e6f708", "label": "Gmail で開く"}, None, None, None])
check("Teams は Teams の入口だけ、Slack・録音・予定は作らない（Hermes の文の中のリンクは使わない）",
      [L({"source": "teams", "id": "0123456789abcdef", "note": "https://evil.example/"}), L({"source": "slack", "id": "C1/1.2"}), L({"source": "plaud", "id": "p"}),
       L({"source": "cal", "id": "c"})],
      [{"url": "https://teams.microsoft.com/v2/", "label": "Teams を開く"}, None, None, None])

# ---------- 見張りの帯 ----------
STATUS.write_text(json.dumps({"orgs": {"org-a": {"ok": True}, "org-b": {"ok": False, "reason": "一覧の形が読めない\x1b[2J" + "長" * 300}}, "at": NOW}))
w = ib.inbox(NOW)["watch"]
check("Teams の見張りが止まっていれば帯を出す（理由は文字として切る。組織の id は出さない）",
      (len(w), w[0]["title"], "\x1b" in w[0]["reason"], len(w[0]["reason"]) <= 160, "org-b" in json.dumps(w, ensure_ascii=False)), (1, "Teams の見張りが止まっています", False, True, False))
STATUS.write_text("{壊れた")
check("状態のファイルが壊れていれば帯は出さない", ib.inbox(NOW)["watch"], [])
STATUS.unlink()

# ---------- 口（済んだ・戻す・あとで） ----------
CMDS = []


def fake_run(cmd, capture_output=True, timeout=None, **k):
    CMDS.append(cmd)
    n = int(cmd[cmd.index("done") + 1])
    if FAKE.get("fail"):
        return subprocess.CompletedProcess(cmd, 1, b"", b"Error: No such container: hermes\n")
    if not FAKE.get("silent"):  # コンテナの中の道具が、記録に印を書き足すのにあたる
        with ITEMS.open("a") as f:
            f.write(json.dumps({"op": "done", "at": time.time(), "n": n, "note": ib.DONE_NOTE, "by": "user"}, ensure_ascii=False) + "\n")
    return subprocess.CompletedProcess(cmd, 0, f"片づけた印を付けました [{n}]\n".encode(), b"")


FAKE = {}
ib.subprocess = types.SimpleNamespace(run=fake_run, TimeoutExpired=subprocess.TimeoutExpired)
ib.hermes_skills.docker_bin = lambda: "/usr/local/bin/docker"
ib.UNDO = 0.4
write([add(21, at=NOW - 60, title="済ませる件"), add(22, at=NOW - 120), add(23, at=NOW - 180), add(24, kind="fyi", at=NOW - 60), add(25, at=NOW - 240)])
before = snapshot()

with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    get = lambda: c.get("/api/inbox").json()
    post = lambda path, body: c.post(path, content=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    check("一覧の口", ns(get()), [21, 22, 23, 25])
    r = post("/api/inbox/done", {"n": 21}).json()
    d = get()
    check("済んだ: すぐ一覧から外し、戻せる間の印を返す（まだ道具は動かさない）", (r["ok"], ns(d), [p["n"] for p in d["pending"]], CMDS), (True, [22, 23, 25], [21], []))
    again = post("/api/inbox/done", {"n": 21}).json()
    check("…戻せる残りの秒を返す（押し直しても延びない。画面は端末の時計に頼らずこの秒で消す）", (0 < r["left"] <= ib.UNDO, again["left"] <= r["left"]), (True, True))
    check("…戻す: 取り消すと一覧に戻り、道具は動かさない", (post("/api/inbox/undo", {"n": 21}).json()["ok"], ns(get()), CMDS), (True, [21, 22, 23, 25], []))
    time.sleep(ib.UNDO + 0.4)
    check("…戻したものは、時間がたっても付けない", (CMDS, ns(get())), ([], [21, 22, 23, 25]))
    # 2 つの画面から同時に「済んだ」、記録を読んでいる間に一方が「戻す」: 戻したあとに、遅れた「済んだ」が予約をよみがえらせない
    import threading as _th
    ib.UNDO = 0.8
    real_records = ib.records
    ib.records = lambda: (time.sleep(0.5), real_records())[1]
    outs = {}
    ta = _th.Thread(target=lambda: outs.update(a=post("/api/inbox/done", {"n": 25}).json()))
    tb = _th.Thread(target=lambda: outs.update(b=post("/api/inbox/done", {"n": 25}).json()))
    ta.start(); time.sleep(0.1); tb.start()
    ta.join()
    outs["undo"] = post("/api/inbox/undo", {"n": 25}).json()
    tb.join()
    ib.records = real_records
    time.sleep(ib.UNDO + 0.6)
    check("同時の「済んだ」と「戻す」が入れ違っても、戻したあとに予約がよみがえらない（済んだ・戻すは 1 つずつ受ける）",
          (outs["a"]["ok"], outs["undo"]["ok"], CMDS, 25 in ns(get())), (True, True, [], True))
    ib.UNDO = 1.0  # 戻してすぐ押し直したら、前の予約で早く付けない（押し直した時刻から数え直す）
    t0 = time.time()
    post("/api/inbox/done", {"n": 21})
    time.sleep(0.1)
    post("/api/inbox/undo", {"n": 21})
    time.sleep(0.4)
    post("/api/inbox/done", {"n": 21})
    time.sleep(max(0, t0 + 1.25 - time.time()))
    early = list(CMDS)
    time.sleep(max(0, t0 + 2.0 - time.time()))
    check("戻してすぐ押し直したら、押し直した時刻から数え直す（前の予約では付けない）", (early, len(CMDS)), ([], 1))
    ib.UNDO = 0.4
    cmd = CMDS[0] if CMDS else []
    check("時間がたつと、コンテナの中で Hermes の利用者として記録の道具に印を付けさせる",
          (cmd[:4], cmd[-8:], "HERMES_HOME=/opt/data" in cmd, "hermes" in cmd),
          (["/usr/local/bin/docker", "exec", "-u", "10000:10000"], ["/opt/hermes/.venv/bin/python", ib.TOOL, "done", "21", "--by", "user", "--note", ib.DONE_NOTE], True, True))
    check("…付いたら一覧から消え、戻すは間に合わない", (ns(get()), post("/api/inbox/undo", {"n": 21}).json()), ([22, 23, 25], {"ok": False, "reason": "late"}))
    FAKE["fail"] = True
    post("/api/inbox/done", {"n": 22})
    time.sleep(ib.UNDO + 0.6)
    d = get()
    check("付けられなければ一覧に戻し、理由を画面に出す", (ns(d), d.get("fail", {}).get("n"), "No such container" in d.get("fail", {}).get("why", "")), ([22, 23, 25], 22, True))
    FAKE.clear()
    FAKE["silent"] = True
    post("/api/inbox/done", {"n": 22})
    time.sleep(ib.UNDO + 0.6)
    d = get()
    check("道具が「付けた」と答えても、記録に印が見えなければ済みにしない", (ns(d), d.get("fail", {}).get("why")), ([22, 23, 25], "記録に印が見当たりません"))
    FAKE.clear()
    check("済んだ: 済み・宛ての知らせ・無い番号は断る",
          [post("/api/inbox/done", {"n": v}).json().get("reason") for v in (21, 24, 999)], ["gone", "gone", "gone"])
    check("番号の形が違えば断る（文字・真偽・0・負・大きすぎ・無い）",
          [post(p, b).status_code for p in ("/api/inbox/done", "/api/inbox/undo") for b in ({"n": "22"}, {"n": True}, {"n": 0}, {"n": -3}, {"n": 2**31}, {})], [400] * 12)
    # あとで
    opts = get()["later_options"]
    r = post("/api/inbox/later", {"n": 23, "until": "morning"}).json()
    d = get()
    st = ib.STATE_FILE
    check("あとで: 選んだ時刻まで一覧から隠し、数だけ出す", (r["ok"], ns(d), d["later"]), (True, [22, 25], 1))
    check("…覚えるのは本体の状態フォルダ（利用者だけが読める 0600）で、置き場ではない", (st.parent == Path(TMP) / "state", oct(st.stat().st_mode & 0o777)), (True, "0o600"))
    m = json.loads(st.read_text())
    m["later"]["23"] = time.time() - 1
    st.write_text(json.dumps(m))
    d = get()
    check("…時刻が来たら一覧の上に戻し、戻った印を付ける", (ns(d)[0], d["items"][0]["back"]), (23, True))
    one = c.get("/api/inbox/item?n=23").json()
    check("通知で開く 1 件は、あとでに回していても返す（一覧の隠す条件に関係なく）", (one["ok"], one["item"]["n"]), (True, 23))
    check("…済み・宛ての知らせ・無い番号・形の違う番号は返さない",
          [c.get(f"/api/inbox/item?n={v}").json().get("reason") for v in ("21", "24", "999")] + [c.get(f"/api/inbox/item?n={v}").status_code for v in ("0", "-1", "x", "1" * 11)],
          ["gone", "gone", "gone", 400, 400, 400, 400])
    check("あとで: 決めた選び方でなければ・済みの件なら断る",
          [post("/api/inbox/later", b).json().get("reason") for b in ({"n": 25, "until": "next-year"}, {"n": 25, "until": 3}, {"n": 21, "until": "morning"})],
          ["bad_request", "bad_request", "gone"])
    check("…選び方は今夜（19 時より前だけ）・明日の朝・来週の月曜", ([o["key"] for o in ib.later_options(today_start + 10 * 3600)], [o["key"] for o in ib.later_options(today_start + 20 * 3600)]),
          (["tonight", "morning", "week"], ["morning", "week"]))
    # あとでから戻った古い件は、別の件をあとでにしても、戻った時刻で新しさを数え続ける（3 日より前に畳み直さない）
    keep_rows = ITEMS.read_text()
    write([add(51, at=NOW - 5 * 86400, title="古いが戻った件"), add(52, at=NOW - 60)])
    st.write_text(json.dumps({"later": {"51": time.time() - 5}}))
    before_old = [x["old"] for x in get()["items"] if x["n"] == 51]
    post("/api/inbox/later", {"n": 52, "until": "morning"})
    after_old = [x["old"] for x in get()["items"] if x["n"] == 51]
    check("あとでから戻った件は、別の件をあとでにしても新しいまま（戻った時刻を残す）", (before_old, after_old), ([False], [False]))
    ITEMS.write_text(keep_rows)
    check("どの操作のあとも、置き場のファイルは本体が書いていない（印はコンテナの中の道具だけが書く）",
          [p for p in snapshot() if p[0] != "voice-loop/items.jsonl"] == [p for p in before if p[0] != "voice-loop/items.jsonl"], True)
    lines = [json.loads(l) for l in ITEMS.read_text().splitlines()]
    check("…記録に増えたのは、道具が書いた済みの印だけ", [r["op"] for r in lines[5:]], ["done"])

# ---------- 通知を押したときに開く場所 ----------
PAY = []
s._push_send = lambda subs, payload, urgency: (PAY.append(payload["url"]), (1, []))[1]
SUB = [{"endpoint": "https://fcm.googleapis.com/fcm/send/x", "keys": {"p256dh": "a", "auth": "b"}}]
for u in ("/", "/#inbox", "/#i=273", "/#t=t0123456789", "/#i=0", "/#i=01", "/#i=12345678901", "/#evil", "/#inbox2", "https://evil.example/#inbox", "//evil.example/", "/#t=t012", "/?x=1#inbox"):
    asyncio.run(s.push_all("t", "b", "tag", only=SUB, url=u))
check("通知の開き先は、画面のはじめ・受け箱・判断待ちの 1 件・件の形だけ（ほかは画面のはじめ）",
      PAY, ["/", "/#inbox", "/#i=273", "/#t=t0123456789"] + ["/"] * 9)
for f in ("static/sw.js", "static/index.html"):
    check(f"{f} も同じ形だけを受ける", "#(t=t[0-9a-f]{10}|inbox|i=[1-9][0-9]{0,9})$/" in (ROOT / f).read_text(), True)

# ---------- コンテナからは使えない ----------
H = {"host": "127.0.0.1:8790", "x-vl-key": "k1"}
check("受け箱の口は、コンテナ（鍵あり）からは使えない",
      [problem(H, m, p, container=True) is not None for m, p in (("GET", "/api/inbox"), ("GET", "/api/inbox/item"), ("POST", "/api/inbox/done"), ("POST", "/api/inbox/undo"), ("POST", "/api/inbox/later"))],
      [True] * 5)

# ---------- 件への返信 ----------
SENT = []
s.hermes.ask_hermes = lambda text: SENT.append(text) or "答えです"
h = s.hermes
write([add(31, title="本番環境へのメンバー追加", project="survey"), add(32, kind="info"), add(33, kind="fyi", title="宛ての件")])
reply, why, saved = asyncio.run(h.hermes_exchange("", item=31))
row = h.HERMES_TURNS[-1]
check("ことばなしの返信: 件の前置きと、中身と案を聞く文を送る",
      (why, SENT[-1]), ("", f"〔返信: 判断待ち [31]「本番環境へのメンバー追加」（survey）〕\n{h.ITEM_EMPTY}"))
check("…台帳には返信として載り、返信先の問いは空（承認に答える・閉じるには使えない）",
      (row[1], row[4], h.TALKER.replying(row[0]), h.TALKER.reply_ask(row[0])), ("", "判断待ち [31]", "", {"pane": "", "fp": "", "session": ""}))
check("…本体の守りも、返信の発言として見る（承認の答え・閉じる許可に使わない）", (s.hub_replying(row[0]) is not None, s.hub_reply_ask(row[0])),
      (True, {"pane": "", "fp": "", "session": ""}))
asyncio.run(h.hermes_exchange("期間は 1 か月で", item=33))
check("ことばを添えた返信は、そのことばを送る（宛ての知らせの件にも返せる）", SENT[-1], "〔返信: 判断待ち [33]「宛ての件」（shop）〕\n期間は 1 か月で")
hist = [m for m in h.hermes_history() if m["role"] == "me"][-2:]
check("会話の履歴: 件への返信の印を付け、ことばなしは「（ことばなし）」と出す",
      [(m["text"], m.get("reply_item"), m.get("reply_to")) for m in hist], [("（ことばなし）", 31, "判断待ち [31]"), ("期間は 1 か月で", 33, "判断待ち [33]")])
n0 = len(SENT)
outs = [asyncio.run(h.hermes_exchange("はい", item=v)) for v in (32, 999, -1)] + [asyncio.run(h.hermes_exchange("はい", 1700000000.5, "", 31))]
check("記録に無い・情報の件・形の違う番号・報告と件の両方を指す返信は、送らずに断る",
      (len(SENT) - n0, [o[1].startswith("返信先の判断待ちが見つかりませんでした") for o in outs]), (0, [True] * 4))
check("…断った発言は台帳に空で載る（前の問い・確認を失効させる）", h.HERMES_TURNS[-1][1], "")
check("本体は、画面の発言の件の番号を、正の整数だけ受け取る（違う形は -1 にして、頭に断らせる）",
      [s.reply_item_of(d) for d in ({}, {"reply_item": None}, {"reply_item": 31}, {"reply_item": "31"}, {"reply_item": True}, {"reply_item": 0}, {"reply_item": 1.0})],
      [None, None, 31, -1, -1, -1, -1])

ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
