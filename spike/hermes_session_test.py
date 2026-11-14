"""ハブ（Hermes）の会話を日ごとに新しくすること・Hermes に渡す指示を毎回同じ文にすること・hubctl sessions と read の試験。
Hermes の API と herdr には触れない（差し替える）。使い方: uv run spike/hermes_session_test.py"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HERMES_KEY="k1", VL_HERMES_SESSION="hub")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import server as s  # noqa: E402
from origin_guard import problem  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


c = TestClient(s.app, base_url="http://127.0.0.1:8790")
at = lambda text: datetime.strptime(text, "%Y-%m-%d %H:%M")

# ---------- 会話の名前（毎朝4時に変わる・画面で新しくする。会話ごとに乱数の印で、前に使った名前へは戻らない） ----------
import re  # noqa: E402


def restart():
    """本体を起動し直したことにする（覚えている印を忘れる。状態のファイルは残る）。"""
    s.hermes._session_mem.clear()


name = lambda t: s.hermes.hermes_session(at(t))
shape = lambda n, day: bool(re.fullmatch(f"hub-{day}-[0-9a-f]{{6}}", n))
a1, a2 = name("2026-10-26 03:59"), name("2026-10-26 04:00")
check("4時より前は前の日の会話、4時からはその日の会話（名前は 元-日付-印）", (shape(a1, "20261025"), shape(a2, "20261026"), a1 != a2), (True, True, True))
check("同じ日のうちは同じ名前で続く", name("2026-10-26 23:59"), a2)
restart()
check("本体を起動し直しても、状態のファイルがあれば同じ会話で続ける", name("2026-10-26 12:00"), a2)
used = {a1, a2}
b1 = s.hermes.hermes_session_new(at("2026-10-26 10:00"))
b2 = s.hermes.hermes_session_new(at("2026-10-26 11:00"))
check("新しくするたびに、前に使っていない名前になる", (shape(b1, "20261026"), shape(b2, "20261026"), len(used | {b1, b2})), (True, True, 4))
used |= {b1, b2}
check("新しくしたあとは、その名前で続く（起動し直しても）", (name("2026-10-26 12:00"), (restart(), name("2026-10-26 12:00"))[1]), (b2, b2))
for bad in ('{"day": "20261026", "token": "zzzzzz"}', '{"day": "20261026", "n": 2}', '{"day": "20261026", "token": ""}', "[1, 2]", "壊れた"):
    s.hermes.HERMES_SESSION_FILE.write_text(bad)
    restart()
    n = name("2026-10-26 12:00")
    check(f"状態のファイルが壊れていたら、前に使った名前へ戻らず新しい会話にする（{bad}）", (shape(n, "20261026"), n in used), (True, False))
    used.add(n)
s.hermes.HERMES_SESSION_FILE.unlink()
restart()
n = name("2026-10-26 12:00")
check("状態のファイルが消えても、前に使った名前（その日の最初の会話も）へは戻らない", (shape(n, "20261026"), n in used), (True, False))
used.add(n)
c1 = name("2026-10-27 04:00")
check("次の日の4時には、新しい印の会話", (shape(c1, "20261027"), c1 in used), (True, False))
s.hermes.HERMES_SESSION_FILE.unlink()
s.hermes.HERMES_SESSION_FILE.mkdir()
restart()
d1 = name("2026-10-28 04:00")
check("状態を書けないときも、動いている間は同じ名前で続ける", (shape(d1, "20261028"), name("2026-10-28 09:00")), (True, d1))
s.hermes.HERMES_SESSION_FILE.rmdir()
restart()
hour = s.hermes.HERMES_RESET_HOUR
s.hermes.HERMES_RESET_HOUR = None
offshape = lambda n: bool(re.fullmatch("hub-[0-9a-f]{6}", n))
o1 = name("2026-10-26 12:00")
check("日ごとに変えない設定（off）でも、名前は 元-印（元の名前だけの会話は使わない）", offshape(o1), True)
o2 = s.hermes.hermes_session_new(at("2026-10-26 12:00"))
restart()
check("off で新しくすると新しい印になり、日をまたいでも、起動し直しても続く", (offshape(o2), o2 != o1, name("2026-10-30 12:00")), (True, True, o2))
s.hermes.HERMES_SESSION_FILE.unlink()
restart()
o3 = name("2026-10-30 12:00")
check("off で新しくしたあと状態が消えて起動し直しても、捨てた会話（前の名前）へ戻らない", (offshape(o3), o3 in (o1, o2)), (True, False))
s.hermes.HERMES_SESSION_FILE.write_text("壊れた")
restart()
check("off で状態が壊れていても、前の名前へ戻らない", name("2026-10-30 12:00") in (o1, o2, o3), False)
s.hermes.HERMES_SESSION_FILE.unlink()
restart()
s.hermes.HERMES_RESET_HOUR = hour

# 状態を読めず書けもしない間は、仮の印で続け（ハブを止めない）、その間は「新しくする」を断る。直ってから起動し直すと、
# 保存されている会話（利用者が最後に選んだ会話）に戻る。利用者が捨てた会話（新しくする前の会話・前の日の会話）へは戻らない
f = s.hermes.HERMES_SESSION_FILE
before_reset = name("2026-10-26 12:00")
chosen = s.hermes.hermes_session_new(at("2026-10-26 12:00"))  # 利用者が新しくした（before_reset は捨てた会話）
restart()
f.chmod(0)
f.parent.chmod(0o500)
try:
    temp = name("2026-10-26 12:00")
    refused = c.post("/api/hermes/session/new").json()
    still = name("2026-10-26 13:00")
finally:
    f.parent.chmod(0o700)
    f.chmod(0o600)
restart()
check("読めず書けない間は仮の会話で続け、「新しくする」は断る。直って起動し直すと、最後に選んだ会話に戻り、捨てた会話には戻らない",
      (temp not in (before_reset, chosen), refused, still == temp, name("2026-10-26 14:00"), name("2026-10-26 14:00") == before_reset),
      (True, {"ok": False, "reason": "state"}, True, chosen, False))
f.unlink()
restart()

# 一覧の読み出し（別のスレッド）が古い印を読んだところで止まり、そのあいだに「新しくする」が済んでも、古い印で上書きしない
import threading  # noqa: E402

real_file = s.hermes.HERMES_SESSION_FILE
s.write_json(real_file, {"day": "20261026", "token": "aaaaaa"})
restart()
read_done, release = threading.Event(), threading.Event()


class SlowPath(type(real_file)):
    def read_text(self, *a, **k):
        text = super().read_text(*a, **k)
        read_done.set()
        release.wait(5)  # 読み終えたところで止まる
        return text


s.hermes.HERMES_SESSION_FILE = SlowPath(str(real_file))
seen = {}
reader = threading.Thread(target=lambda: seen.update(old=s.hermes.hermes_session(at("2026-10-26 12:00"))))
reader.start()
read_done.wait(5)
resetter = threading.Thread(target=lambda: seen.update(new=s.hermes.hermes_session_new(at("2026-10-26 12:00"))))
resetter.start()
time.sleep(0.3)
release.set()
reader.join(5)
resetter.join(5)
s.hermes.HERMES_SESSION_FILE = real_file
check("一覧の読み出しと「新しくする」が重なっても、新しくした印で続ける（古い印で上書きしない）",
      (seen.get("old"), name("2026-10-26 12:00") == seen.get("new"), json.loads(real_file.read_text())["token"] == seen.get("new", "")[-6:]),
      ("hub-20261026-aaaaaa", True, True))
real_file.unlink()
restart()
check("話し相手の一覧の Hermes の項目に、会話を新しくする時刻が出る（off なら空）",
      (next(a for a in c.get("/api/agents").json() if a.get("hermes"))["reset_hour"],), (4,))

# Hermes に送るときは、いまの会話の名前を付ける
sent = []


class Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.body).encode()


def fake_urlopen(req, timeout=None):
    sent.append({"session": req.get_header("X-hermes-session-id"), "body": json.loads(req.data)})
    return Resp({"choices": [{"message": {"content": "はい"}, "finish_reason": "stop"}]})


s.urllib.request.urlopen = fake_urlopen
s.hermes.ask_hermes("こんにちは")
check("Hermes に送るときは、いまの会話の名前（元の名前＋日付）を付ける", sent[-1]["session"], s.hermes.hermes_session())
r = c.post("/api/hermes/session/new").json()
s.hermes.ask_hermes("もう一度")
check("画面で新しくすると、次の発言から新しい会話の名前で送る", (r["ok"], sent[-1]["session"], r["session"] != sent[-2]["session"]), (True, r["session"], True))
check("新しくしたことは、画面の履歴に知らせとして残る", s.hermes.hermes_history()[-1], {"role": "note", "text": "ハブの会話を新しくしました（Hermes の記憶と手順書は残ります）"})
check("話し相手の一覧の Hermes の項目に、いまの会話の名前が出る", next(a for a in c.get("/api/agents").json() if a.get("hermes"))["session"], r["session"])
s.hermes.HERMES_SESSION_FILE.unlink()
s.hermes.HERMES_SESSION_FILE.mkdir()
before = s.hermes.hermes_session()  # 覚えている印で続く
check("状態を書けないときは新しくせず、そう返す", (c.post("/api/hermes/session/new").json(), s.hermes.hermes_session()), ({"ok": False, "reason": "state"}, before))
s.hermes.HERMES_SESSION_FILE.rmdir()
key = s.hermes.HERMES_KEY
s.hermes.HERMES_KEY = ""
check("Hermes がつながっていなければ断る", c.post("/api/hermes/session/new").json(), {"ok": False, "reason": "off"})
s.hermes.HERMES_KEY = key
check("ハブ（コンテナ）からは、鍵があっても新しくする口を使えない",
      problem({"host": "127.0.0.1:8790", "x-vl-key": "k1"}, "POST", "/api/hermes/session/new", container=True) is not None, True)

# ---------- Hermes に渡す指示は毎回同じ文（発言には何も足さない） ----------
s.hermes.HERMES_DIR.mkdir(parents=True, exist_ok=True)
(s.hermes.HERMES_DIR / "hub.md").write_text("ハブへの指示（試験）")
first, second = s.hermes.hermes_messages("こんにちは"), s.hermes.hermes_messages("こんにちは")
check("指示（system）は毎回 1 字も変わらない（キャッシュを書き直させない）", (first[0] == second[0], first[0]["content"]), (True, "ハブへの指示（試験）"))
check("発言には、本体の案内を足さない", first[-1], {"role": "user", "content": "こんにちは"})

# ---------- sessions: 最終更新の時刻と、直近の依頼がどこから来たか ----------
LOG = Path(TMP) / "w.jsonl"
A = {"pane": "w9:p1", "title": "テスト", "cwd": "/x/proj", "kind": "claude", "session": "sess-1", "status": "idle", "name": "", "hub": False}
s.list_agents = lambda: [dict(A)]
s.jsonl_path = lambda a: LOG


def session_log(user, reply):
    LOG.write_text("\n".join(json.dumps(d, ensure_ascii=False) for d in [
        {"type": "user", "uuid": "u1", "message": {"content": user}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}}]) + "\n")


def row():
    return c.get("/api/hub/sessions").json()[0]


s.TASKS.clear()
session_log("README の 誤字を直して", "直しました")
s.task_new({"pane": "w9:p1", "project": "proj", "title": "テスト", "text": "README の  誤字を直して "}, "send")
r = row()
check("ハブが送った依頼なら voice-loop（空白の数の違いは無視）", (r["asked_by"], r["updated_at"] == datetime.fromtimestamp(LOG.stat().st_mtime).strftime("%m/%d %H:%M")), ("voice-loop", True))
session_log("ついでに CHANGELOG も直して", "直しました")
check("ハブを通さずに頼まれた依頼なら other", row()["asked_by"], "other")
s.task_new({"pane": "w8:p1", "project": "other", "title": "別", "text": "ついでに CHANGELOG も直して"}, "send")
check("同じ文でも、別のセッションに送ったものは数えない", row()["asked_by"], "other")
old = time.time() - 4 * 86400
os.utime(LOG, (old, old))
check("頼んだ件の台帳が残る3日より古い依頼は、分からない（None）", row()["asked_by"], None)
s.task_new({"pane": "w9:p1", "project": "proj", "title": "テスト", "text": "README の誤字を直して"}, "send")
s.TASKS.clear()
s._load_tasks()
session_log("README の誤字を直して", "直しました")
check("台帳を読み直しても（本体の再起動）、送った文の指紋は残る", row()["asked_by"], "voice-loop")
check("台帳のファイルに、送った文の指紋が書かれる", '"fp"' in s.TASKS_FILE.read_text(), True)

# ---------- read: 長いターンでも結論（末尾）を残す ----------
long_reply = "途中経過。" * 600 + "【結論】マージしてよいです"
session_log("どうなった？", long_reply)
default = c.get("/api/hub/read", params={"pane": "w9:p1", "n": 1}).json()["messages"][-1]["text"]
full = c.get("/api/hub/read", params={"pane": "w9:p1", "n": 1, "full": "true"}).json()["messages"][-1]["text"]
check("read（既定）: 長い返答でも末尾の結論が残り、長さは 1,600 字ほどまで", (default.endswith("【結論】マージしてよいです"), len(default) <= 1600), (True, True))
check("read --full: 末尾の結論が残る", full.endswith("【結論】マージしてよいです"), True)
short = "短い返答です"
session_log("どう？", short)
check("read: 短い返答はそのまま", c.get("/api/hub/read", params={"pane": "w9:p1", "n": 1}).json()["messages"][-1]["text"], short)

ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
