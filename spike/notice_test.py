"""代理（Hermes）からの知らせ（hubctl notice）の試験。本体の口（受ける。一覧・消す口は 0.35.2 で無くした）と、コンテナから使える口の範囲、hubctl を確かめる。
常駐の 8790・本物のコンテナには触れない。hubctl の確かめには、この試験の中で 127.0.0.1:8792 に本体の写しを立てる。
画面での扱い（一覧に出さない・受け箱を取り直す）は spike/pc_ui_test.py の「知らせ」で確かめる。
相手の位置から見ること: コンテナ（鍵あり）から使えるのは知らせを足す口だけ。知らせは鳴らすだけで残さない。題と一言は 1 行・上限の字数で切り、
制御文字・向きを変える文字・対になっていないサロゲートは空白にする。1 時間に受ける数の上限（時計が戻っても外れない）。
状態フォルダの記録が壊れていても・書き換えられていても、受けるときと同じ形にそろえ直してから使う。プッシュ通知には題だけを載せ、一言は載せない。
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
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HERMES_KEY="k1", VL_HERMES_URL="http://127.0.0.1:9",
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
urls = []  # 通知を押したときに開く場所


async def fake_push(title, body, tag, urgency="normal", only=None, url="/"):
    pushes.append((title, body, tag))
    urls.append(url)
    return "sent"


class FakeClient:
    def __init__(self):
        self.got = []

    async def send(self, **msg):
        self.got.append(msg)


s.push_all = fake_push
c = TestClient(s.app, base_url="http://127.0.0.1:8790")
# 本文は \uXXXX の形で送る（対になっていないサロゲートも、相手が送るのと同じ形で届く）
post = lambda body: c.post("/api/hub/notice", content=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
reset = lambda: s.NOTICES_FILE.unlink(missing_ok=True)

# ---------- 受ける ----------
reset()
fc = FakeClient()
s.CLIENTS.add(fc)
title_of = lambda body: (post(body).status_code, pushes[-1][1], fc.got[-1]["title"])  # 受けた題が、通知と開いている画面にどう出るか
r = post({"title": "山田さんから見積もりの返事", "text": "返信の下書きを Gmail に作りました。金額の判断が要ります"})
check("知らせを受け、番号を返す", (r.status_code, r.json().get("ok"), r.json().get("n")), (200, True, 1))
check("プッシュ通知には送り手と題だけ（一言は載せない）", pushes[-1], ("Hermes から", "山田さんから見積もりの返事", "notice-1"))
check("開いている画面には、題だけを送る", fc.got[-1], {"type": "agent_notice", "n": 1, "title": "山田さんから見積もりの返事", "sender": "Hermes"})
saved = s.NOTICES_FILE.read_text()
check("知らせは残さない（記録にあるのは、次の番号と受けた時刻だけ。題も一言も書かない）",
      (sorted(json.loads(saved)), "山田" in saved, "下書き" in saved, json.loads(saved)["next"]), (["next", "sent"], False, False, 2))
check("一言は省ける・番号は続く", post({"title": "二つ目"}).json().get("n"), 2)
check("一覧と消す口は無い（0.35.2 から）", [c.get("/api/notices").status_code, c.post("/api/notices/dismiss", json={"all": True}).status_code], [404, 404])

# ---------- 形をそろえる ----------
reset()
check("改行・タブ・制御文字・向きを変える文字は空白にして 1 行にする", title_of({"title": "一行目\n二行目\t\x00末尾\u202e逆向き\u200b", "text": "あ" * 500}),
      (200, "一行目 二行目 末尾 逆向き", "一行目 二行目 末尾 逆向き"))
st, pt, ft = title_of({"title": "題" * 100})
check("題は 60 字で切る（通知も、開いている画面も）", (st, len(pt), len(ft), pt[-1]), (200, 60, 60, "…"))
n_push, n_got = len(pushes), len(fc.got)
bad = [post(b) for b in ({}, {"title": ""}, {"title": " \n\t "}, {"title": 1}, {"title": ["a"]}, {"title": "ok", "text": 3}, {"title": "ok", "text": None})]
check("題が無い・空・文字でない、一言が文字でないときは断る（400）", [x.status_code for x in bad], [400] * 7)
check("…そして断ったものは、鳴らさない・番号も進めない", (len(pushes) - n_push, len(fc.got) - n_got, post({"title": "次"}).json().get("n")), (0, 0, 3))
check("本文が JSON の物でなければ断る", c.post("/api/hub/notice", content=b'"text"', headers={"Content-Type": "application/json"}).status_code in (400, 422), True)

# ---------- 通知を押したときに開く場所（0.31.0。受け箱 /#inbox、判断待ちが 1 つなら /#i=<番号>） ----------
reset(); urls.clear()
post({"title": "番号なし"}); post({"title": "1 件", "items": [273]}); post({"title": "2 件", "items": [12, 13]}); post({"title": "同じ番号", "items": [7, 7]})
check("通知を押すと受け箱、判断待ちが 1 つならその件（同じ番号は 1 つに数える）", urls, ["/#inbox", "/#i=273", "/#inbox", "/#i=7"])
n_push = len(pushes)
bad = [post({"title": "x", "items": v}) for v in ("273", [0], [-1], [True], [1.5], ["12"], [2**31], list(range(1, s.NOTICE_ITEMS + 2)))]
check("判断待ちの番号の形が違えば受けず、理由を言う（文字・0・負・真偽・小数・大きすぎ・多すぎ）",
      [(r.status_code, r.json().get("reason")) for r in bad], [(400, "bad_items")] * 8)
check("…断った知らせは鳴らさない", len(pushes) - n_push, 0)
many = post({"title": "21 件", "items": list(range(1, 22))}).json()
check("見張りの 1 回で 21 件以上の判断待ちを添えても受け、受け箱を開く（20 件を超えても知らせが断られない）", (many.get("ok"), urls[-1]), (True, "/#inbox"))

# ---------- 数の上限 ----------
reset()
for i in range(s.NOTICE_PER_HOUR):
    post({"title": f"件{i}"})
n_push = len(pushes)
over = post({"title": "多すぎ"}).json()
check("1 時間に受ける数を超えたら断り、鳴らさない", (over.get("ok"), over.get("reason"), over.get("max"), len(pushes) - n_push), (False, "too_many", s.NOTICE_PER_HOUR, 0))
d = json.loads(s.NOTICES_FILE.read_text())
d["sent"] = [t - 3700 for t in d["sent"]]
s.write_json(s.NOTICES_FILE, d)
check("1 時間たてば、また受ける", post({"title": "一時間後"}).json().get("ok"), True)

# ---------- 壊れた記録・仕込まれた記録（相手の位置: 状態フォルダの記録を書き換えられた・壊れたとき） ----------
reset()
now = time.time()
s.write_json(s.NOTICES_FILE, {"next": 200, "rows": [{"n": i, "title": f"t{i}", "text": "", "at": now - 100 + i} for i in range(1, 61)], "sent": ["x", now - 10]})
check("前の版の記録（知らせの行つき）からも、番号は前の続き・行は書き残さない",
      (post({"title": "続き"}).json().get("n"), sorted(json.loads(s.NOTICES_FILE.read_text())), len(json.loads(s.NOTICES_FILE.read_text())["sent"])), (200, ["next", "sent"], 2))
# 前の版（0.35.0 まで）が残した題・一言は、起動のときに取り除く（次の知らせを待たない。上限に届いていて受けられないときも）
old_sent = [now - 60 * i for i in range(1, s.NOTICE_PER_HOUR + 1)]
s.write_json(s.NOTICES_FILE, {"next": 77, "rows": [{"n": 76, "title": "前の版の題", "text": "前の版の一言", "at": now - 60}], "sent": old_sent + [now - 7200, "x"]})
refused = post({"title": "上限のあと"}).json().get("reason")
check("前提: 上限に届いていると受けられず、前の版の題・一言は記録に残ったまま", (refused, "前の版の題" in s.NOTICES_FILE.read_text()), ("too_many", True))
tidied = s.notices_tidy()
left = json.loads(s.NOTICES_FILE.read_text())
check("起動のときの片づけ: 題・一言を取り除き、番号と直近 1 時間の時刻は残す（上限は外れない）",
      (tidied, sorted(left), left["next"], left["sent"] == old_sent, "前の版" in s.NOTICES_FILE.read_text(), post({"title": "片づけのあと"}).json().get("reason")),
      (True, ["next", "sent"], 77, True, False, "too_many"))
check("…片づいた記録は、書き直さない", (s.notices_tidy(), json.loads(s.NOTICES_FILE.read_text()) == left), (False, True))
s.NOTICES_FILE.unlink()
check("…記録が無ければ、作らない", (s.notices_tidy(), s.NOTICES_FILE.exists()), (False, False))
for junk in ("{壊れた", '["rows"]', '{"next": 3, "sent": [], "rows": "題"}', '{"next": 3, "sent": [], "memo": "<b>x</b>"}'):
    s.NOTICES_FILE.write_text(junk)
    s.notices_tidy()
    check(f"…壊れた・余計な項目のある記録 {junk[:20]} も、番号と時刻だけに書き直す", sorted(json.loads(s.NOTICES_FILE.read_text())), ["next", "sent"])
s.NOTICES_FILE.write_text('{"next": 5, "sent": [], "pad": "' + "x" * (s.NOTICE_FILE_MAX + 10) + '"}')
check("…大きすぎる記録も、番号と時刻だけに書き直す（落ちない）", (s.notices_tidy(), s.NOTICES_FILE.stat().st_size < 100), (True, True))
import asyncio as _aio  # noqa: E402
s.write_json(s.NOTICES_FILE, {"next": 9, "rows": [{"n": 8, "title": "起動の前の題", "text": "", "at": now}], "sent": []})


async def _boot():
    async with s.lifespan(s.app):
        pass


keep = (s.boot_ensure_hub, s.keep_gateway_token, s.watch_blocked, s.HUB.watchers)
async def _nop():
    return None
s.boot_ensure_hub = s.keep_gateway_token = s.watch_blocked = _nop
s.HUB.watchers = lambda: []
_aio.run(_boot())
s.boot_ensure_hub, s.keep_gateway_token, s.watch_blocked, s.HUB.watchers = keep
check("本体の起動で、片づけが 1 回動く", ("起動の前の題" in s.NOTICES_FILE.read_text(), json.loads(s.NOTICES_FILE.read_text())["next"]), (False, 9))
s.NOTICES_FILE.write_text("{壊れた")
check("記録が壊れていても、受け直せる（番号は 1 から）", post({"title": "直った"}).json().get("n"), 1)
for bad_doc in ({"rows": None, "sent": 7}, {"rows": "x", "sent": {"a": 1}, "next": "9"}, {"next": True}, {"next": -3}, {"next": 1.5}, [], "文字", None):
    s.write_json(s.NOTICES_FILE, bad_doc)
    r = post({"title": "a"})
    check(f"形の違う記録 {str(bad_doc)[:30]} でも、受けるは落ちず、番号は 1 から", (r.status_code, r.json().get("n")), (200, 1))
s.NOTICES_FILE.write_text('{"next": 3, "sent": [Infinity, NaN, -Infinity]}')
check("有限でない時刻は数えず、受ける", (post({"title": "a"}).status_code, len(json.loads(s.NOTICES_FILE.read_text())["sent"])), (200, 1))
s.NOTICES_FILE.write_text('{"next": 7, "pad": "' + "x" * (s.NOTICE_FILE_MAX + 10) + '"}')
check("大きすぎる記録は読まず、空として扱う", post({"title": "大きすぎのあと"}).json().get("n"), 1)
big = "1" + "0" * 400
s.NOTICES_FILE.write_text('{"next": 3, "sent": [' + big + ', -' + big + ']}')
check("巨大な整数の時刻は数えず、受けるは落ちない", (post({"title": "巨大のあと"}).status_code, len(json.loads(s.NOTICES_FILE.read_text())["sent"])), (200, 1))
s.NOTICES_FILE.write_text('{"next": ' + "9" * 4300 + ', "sent": []}')
r = post({"title": "続き"})
check("番号の続きが巨大なら、1 から振り直す（落ちない）", (r.status_code, r.json().get("n")), (200, 1))
s.write_json(s.NOTICES_FILE, {"next": s.NOTICE_N_MAX, "sent": []})
check("番号が上限に達したら、受けずに断る（画面で扱える整数を超えない）", post({"title": "上限"}).json().get("reason"), "too_many")

# ---------- 対になっていない文字（相手の位置: 不正な文字で本体を落とす・上限を使わずに繰り返す） ----------
reset()
check("対になっていないサロゲートは空白にして受ける（500 にしない）", title_of({"title": "前\ud800後", "text": "\udfff一言"}), (200, "前 後", "前 後"))
check("対になった文字（絵文字）はそのまま", title_of({"title": "📨 届いた"}), (200, "📨 届いた", "📨 届いた"))
check("サロゲートだけの題は、空として断る", post({"title": "\ud800"}).status_code, 400)

# ---------- 時計が戻っても（相手の位置: 上限を外す） ----------
reset()
for i in range(s.NOTICE_PER_HOUR):
    post({"title": f"件{i}"})
d = json.loads(s.NOTICES_FILE.read_text())
d["sent"] = [t + 60 for t in d["sent"]]  # 時計が 1 分戻ったのと同じ（記録の時刻が、いまより先になる）
s.write_json(s.NOTICES_FILE, d)
check("時計が戻っても、受けた数を数え続ける", post({"title": "戻したあと"}).json().get("reason"), "too_many")
d["sent"] = [t + 2 * 86400 for t in d["sent"]]
s.write_json(s.NOTICES_FILE, d)
check("1 日より先の未来の時刻は数えない（止まったままにならない）", post({"title": "ずっと先"}).json().get("ok"), True)
s.CLIENTS.discard(fc)

# ---------- コンテナから使える口 ----------
H = {"host": "127.0.0.1:8790", "x-vl-key": "k1"}
check("コンテナ（鍵あり）から知らせを足せる", problem(H, "POST", "/api/hub/notice", container=True), None)
check("コンテナからは、知らせの口を読む（GET）ことはできない", problem(H, "GET", "/api/hub/notice", container=True) is not None, True)
check("鍵が無い・違うコンテナからは、知らせも断る", [problem(h, "POST", "/api/hub/notice", container=True) is not None for h in
                                          ({"host": "127.0.0.1:8790"}, {"host": "127.0.0.1:8790", "x-vl-key": "k2"})], [True, True])
check("よそのページ（Origin）からは、知らせを断る", problem({"host": "127.0.0.1:8790", "origin": "https://evil.example"}, "POST", "/api/hub/notice") is not None, True)

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
    check("…通知には題だけが載る（一言は載らない）", (pushes[-1][1], "下書き" in json.dumps(pushes[-1], ensure_ascii=False)), ("見積もりの返事", False))
    r = hub("notice", "")
    check("hubctl notice: 題が空なら断り、理由を言う", (r.returncode != 0, "題が空です" in r.stderr), (True, True))
    urls.clear()
    r = hub("notice", "判断待ち 1 件: 見積もり", "案件｜見積もり", "--item", "41")
    check("hubctl notice --item: 判断待ちの番号を添え、押すとその件が開く", (r.returncode, urls), (0, ["/#i=41"]))
    r = hub("notice", "判断待ち", "--item", "0")
    check("hubctl notice --item: 番号が違えば断り、理由を言う", (r.returncode != 0, "--item の番号が正しくありません" in r.stderr), (True, True))
    check("hubctl の説明に notice がある", "hubctl notice <題>" in hub("--help").stdout, True)
    srv.should_exit = True
    th.join(5)

ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
