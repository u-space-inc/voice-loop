"""代理（Hermes）からの知らせ（hubctl notice）の試験。本体の口（受ける・一覧・消す）と、コンテナから使える口の範囲、hubctl を確かめる。
常駐の 8790・本物のコンテナには触れない。hubctl の確かめには、この試験の中で 127.0.0.1:8792 に本体の写しを立てる。
画面での出し方（文字として出す・数・消す）は spike/pc_ui_test.py の「知らせ」で確かめる。
相手の位置から見ること: コンテナ（鍵あり）から使えるのは知らせを足す口だけで、一覧・消す口は断る。題と一言は 1 行・上限の字数で切り、
制御文字・向きを変える文字は空白にする。1 時間に受ける数の上限（消しても戻らない）。プッシュ通知には題だけを載せ、一言は載せない。
使い方: uv run spike/notice_test.py"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HUB="hermes", VL_HERMES_KEY="k1", VL_HERMES_URL="http://127.0.0.1:9",
                  VL_HERMES_HOME=f"{TMP}/hermes")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import server as s  # noqa: E402
from origin_guard import problem  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


pushes = []


async def fake_push(title, body, tag, urgency="normal", only=None):
    pushes.append((title, body, tag))
    return "sent"


class FakeClient:
    def __init__(self):
        self.got = []

    async def send(self, **msg):
        self.got.append(msg)


s.push_all = fake_push
c = TestClient(s.app, base_url="http://127.0.0.1:8790")
post = lambda body: c.post("/api/hub/notice", json=body)
reset = lambda: s.NOTICES_FILE.unlink(missing_ok=True)

# ---------- 受ける ----------
reset()
fc = FakeClient()
s.CLIENTS.add(fc)
r = post({"title": "山田さんから見積もりの返事", "text": "返信の下書きを Gmail に作りました。金額の判断が要ります"})
check("知らせを受け、番号を返す", (r.status_code, r.json().get("ok"), r.json().get("n")), (200, True, 1))
check("プッシュ通知には送り手と題だけ（一言は載せない）", pushes[-1], ("Hermes から", "山田さんから見積もりの返事", "notice-1"))
check("開いている画面には、題だけを送る", fc.got[-1], {"type": "agent_notice", "n": 1, "title": "山田さんから見積もりの返事", "sender": "Hermes"})
s.CLIENTS.discard(fc)
rows = c.get("/api/notices").json()
check("一覧に残る（送り手・題・一言）", (rows["sender"], [(x["n"], x["title"], x["text"]) for x in rows["rows"]]),
      ("Hermes", [(1, "山田さんから見積もりの返事", "返信の下書きを Gmail に作りました。金額の判断が要ります")]))
post({"title": "二つ目"})
check("一覧は新しい順・一言は省ける", [(x["n"], x["text"]) for x in c.get("/api/notices").json()["rows"]], [(2, ""), (1, "返信の下書きを Gmail に作りました。金額の判断が要ります")])

# ---------- 形をそろえる ----------
reset()
r = post({"title": "一行目\n二行目\t\x00末尾‮逆向き​", "text": "あ" * 500})
row = c.get("/api/notices").json()["rows"][0]
check("改行・タブ・制御文字・向きを変える文字は空白にして 1 行にする", row["title"], "一行目 二行目 末尾 逆向き")
check("一言は 400 字で切る（末尾に …）", (len(row["text"]), row["text"][-1]), (400, "…"))
post({"title": "題" * 100})
check("題は 60 字で切る", len(c.get("/api/notices").json()["rows"][0]["title"]), 60)
bad = [post(b) for b in ({}, {"title": ""}, {"title": " \n\t "}, {"title": 1}, {"title": ["a"]}, {"title": "ok", "text": 3}, {"title": "ok", "text": None})]
check("題が無い・空・文字でない、一言が文字でないときは断る（400）", [x.status_code for x in bad], [400] * 7)
check("…そして断ったものは残さない", len(c.get("/api/notices").json()["rows"]), 2)
check("本文が JSON の物でなければ断る", c.post("/api/hub/notice", content=b'"text"', headers={"Content-Type": "application/json"}).status_code in (400, 422), True)

# ---------- 数の上限 ----------
reset()
for i in range(s.NOTICE_PER_HOUR):
    post({"title": f"件{i}"})
over = post({"title": "多すぎ"}).json()
check("1 時間に受ける数を超えたら断る", (over.get("ok"), over.get("reason"), over.get("max")), (False, "too_many", s.NOTICE_PER_HOUR))
c.post("/api/notices/dismiss", json={"all": True})
check("消しても、1 時間の数は戻らない", post({"title": "消したあと"}).json().get("reason"), "too_many")
d = json.loads(s.NOTICES_FILE.read_text())
d["sent"] = [t - 3700 for t in d["sent"]]
s.write_json(s.NOTICES_FILE, d)
check("1 時間たてば、また受ける", post({"title": "一時間後"}).json().get("ok"), True)

# ---------- 残す数と日数・壊れた記録 ----------
reset()
now = time.time()
s.write_json(s.NOTICES_FILE, {"next": 200, "rows": [{"n": i, "title": f"t{i}", "text": "", "at": now - 100 + i} for i in range(1, 61)]
                              + [{"n": 61, "title": "古い", "text": "", "at": now - 8 * 86400}, {"n": "x"}, "壊れ", {"n": 62, "title": 5, "text": "", "at": now}],
                              "sent": ["x", now - 10]})
rows = c.get("/api/notices").json()["rows"]
check("残すのは新しい 50 件まで・7 日を過ぎたもの・形の違うものは落とす", (len(rows), rows[0]["n"], rows[-1]["n"]), (50, 60, 11))
check("番号は前の続き", post({"title": "続き"}).json().get("n"), 200)
s.NOTICES_FILE.write_text("{壊れた")
check("記録が壊れていても、一覧は空として読み、受け直せる", (c.get("/api/notices").json()["rows"], post({"title": "直った"}).json().get("n")), ([], 1))
s.write_json(s.NOTICES_FILE, {"next": True, "rows": [{"n": 5, "title": "a", "text": "", "at": now}]})
check("番号が壊れていても、残っている番号の次から振る", post({"title": "b"}).json().get("n"), 6)

# ---------- 消す ----------
reset()
for t in ("一", "二", "三"):
    post({"title": t})
check("× で 1 件消す", (c.post("/api/notices/dismiss", json={"n": 2}).json(), [x["n"] for x in c.get("/api/notices").json()["rows"]]),
      ({"ok": True, "left": 2}, [3, 1]))
check("番号が数でない・真偽値・無いときは断る", [c.post("/api/notices/dismiss", json=b).status_code for b in ({"n": "1"}, {"n": True}, {}, {"all": "yes"})], [400] * 4)
check("すべて消す", (c.post("/api/notices/dismiss", json={"all": True}).json(), c.get("/api/notices").json()["rows"]), ({"ok": True, "left": 0}, []))

# ---------- コンテナから使える口 ----------
H = {"host": "127.0.0.1:8790", "x-vl-key": "k1"}
check("コンテナ（鍵あり）から知らせを足せる", problem(H, "POST", "/api/hub/notice", container=True), None)
check("コンテナからは一覧・消すは使えない", [problem(H, m, p, container=True) is not None for m, p in
                                       (("GET", "/api/notices"), ("POST", "/api/notices/dismiss"), ("GET", "/api/hub/notice"))], [True, True, True])
check("鍵が無い・違うコンテナからは、知らせも断る", [problem(h, "POST", "/api/hub/notice", container=True) is not None for h in
                                          ({"host": "127.0.0.1:8790"}, {"host": "127.0.0.1:8790", "x-vl-key": "k2"})], [True, True])
check("よそのページ（Origin）からは、知らせも一覧も断る", [problem({"host": "127.0.0.1:8790", "origin": "https://evil.example"}, m, p) is not None
                                                for m, p in (("POST", "/api/hub/notice"), ("GET", "/api/notices"), ("POST", "/api/notices/dismiss"))], [True] * 3)

# ---------- Claude Code のハブのとき ----------
s.HUB, was = s.ClaudeBrain(), s.HUB
reset()
post({"title": "ハブからの知らせ"})
check("ハブが Claude Code なら、送り手は「ハブ」", (pushes[-1][0], c.get("/api/notices").json()["sender"]), ("ハブ から", "ハブ"))
s.HUB = was

# ---------- hubctl（127.0.0.1:8792 に本体の写しを立てる） ----------
import uvicorn  # noqa: E402

with socket.socket() as probe:
    busy = probe.connect_ex(("127.0.0.1", 8792)) == 0
if busy:
    check("8792 が空いている（hubctl の試験を立てられる）", busy, False)
else:
    reset()
    srv = uvicorn.Server(uvicorn.Config(s.app, host="127.0.0.1", port=8792, log_level="warning", lifespan="off"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    end = time.time() + 10
    while not srv.started and time.time() < end:
        time.sleep(0.05)
    env = {**os.environ, "VL_HUB_URL": "http://127.0.0.1:8792/api/hub", "VL_HUB_KEY_FILE": ""}
    hub = lambda *a: subprocess.run(["uv", "run", "--quiet", "--script", str(ROOT / "hubctl"), *a], capture_output=True, text=True, env=env, timeout=120)
    r = hub("notice", "見積もりの返事", "下書きを作りました。", "判断が要ります")
    check("hubctl notice: 知らせて、番号と届け方を返す", (r.returncode, "知らせました [1]" in r.stdout, "プッシュ通知も送りました" in r.stdout), (0, True, True))
    check("…一言は残りの語をつないだもの", c.get("/api/notices").json()["rows"][0]["text"], "下書きを作りました。 判断が要ります")
    r = hub("notice", "")
    check("hubctl notice: 題が空なら断り、理由を言う", (r.returncode != 0, "題が空です" in r.stderr), (True, True))
    check("hubctl の説明に notice がある", "hubctl notice <題>" in hub("--help").stdout, True)
    srv.should_exit = True
    th.join(5)

ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
