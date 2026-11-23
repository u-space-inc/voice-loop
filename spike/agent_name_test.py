"""話しかける相手の呼び名（config.env の VL_AGENT_NAME）の試験。本体の写しを 127.0.0.1:8791 に立て、本物のブラウザ（Chromium）で開く
（常駐の 8790・herdr・docker・Hermes には触れない。話し相手の一覧は、この試験の中で作る）。
見ること:
  - 書かなければ、これまでの表示のまま（役の名前は「ハブ」、話す相手として部品の名前を出していたところは「Hermes」）
  - 書けば、画面・通知・Slack の文・状態の文に、その名前を出す（「Hermes の」は「ヴィカの」。英字の名前なら「Vika の」）
  - 使えない名前（記号・改行・17 字以上）は使わず、これまでの表示のまま。診断で知らせる
  - 一覧の Hermes の項目に、呼び名（agent_name）を載せる。画面はそれを受けて、HTML に書いてある文（ボタン・見出し）も替える
  - 名前は、文字として扱う（$& などの置き換えの記号を含む名前は、そもそも受け付けない）
使い方: uv run spike/agent_name_test.py"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp()).resolve()
ROOT = Path(__file__).resolve().parent.parent
results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


# ---------- 本体の側（名前は読み込みのときに決まるので、名前ごとに別の Python で確かめる） ----------
CHILD = r"""
import json, os, sys
sys.path.insert(0, os.environ["ROOT"]); sys.path.insert(0, os.environ["ROOT"] + "/scripts")
import server as s, hub_brain, slack_report, hermes_inbox
items = []
s.hermes.TALKER.doctor(lambda name, ok, detail="", optional=False: items.append((name, ok, detail)))
print(json.dumps({"agent": hub_brain.AGENT, "agent_h": hub_brain.AGENT_H, "bad": hub_brain.AGENT_NAME_BAD,
                  "entry": s.hermes.hermes_agent().get("agent_name"), "label": s.hermes.TALKER.label, "brain_label": hub_brain.Brain.label,
                  "sending": s.hermes.TALKER.sending_text(), "no_report": slack_report.NO_REPORT, "server_agent": s.AGENT,
                  "doctor": [i for i in items if i[0] == "呼び名"]}, ensure_ascii=False))
"""


def child(name):
    env = {k: v for k, v in os.environ.items() if not k.startswith("VL_")}
    env.update(ROOT=str(ROOT), VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HERMES_KEY="k1", VL_HERMES_URL="http://127.0.0.1:9",
               VL_HERMES_HOME=f"{TMP}/no-hermes")
    if name is not None:
        env["VL_AGENT_NAME"] = name
    out = subprocess.run([sys.executable, "-c", CHILD], env=env, capture_output=True, text=True, timeout=120)
    lines = [l for l in out.stdout.splitlines() if l.startswith("{")]
    if not lines:
        raise RuntimeError(out.stderr[-2000:])
    return json.loads(lines[-1])


d = child(None)
check("書かない: 役の名前はハブ・部品の名前は Hermes（これまでのまま）", (d["agent"], d["agent_h"], d["label"], d["brain_label"], d["server_agent"]),
      ("ハブ", "Hermes", "Hermes", "ハブ", "ハブ"))
check("書かない: 状態の文・Slack の文はこれまでのまま", (d["sending"], d["no_report"]),
      ("ハブに送りました。考え中…", "（ハブの報告を受け取れませんでした。voice-loop の画面で確かめてください）"))
check("書かない: 一覧の呼び名は空・診断で知らせない", (d["entry"], d["doctor"]), ("", []))

d = child("ヴィカ")
check("ヴィカ: 役の名前も部品の名前を出していたところもヴィカ", (d["agent"], d["agent_h"], d["label"], d["brain_label"], d["server_agent"]),
      ("ヴィカ", "ヴィカ", "ヴィカ", "ヴィカ", "ヴィカ"))
check("ヴィカ: 状態の文・Slack の文", (d["sending"], d["no_report"]),
      ("ヴィカに送りました。考え中…", "（ヴィカの報告を受け取れませんでした。voice-loop の画面で確かめてください）"))
check("ヴィカ: 一覧に呼び名を載せる・診断で知らせない", (d["entry"], d["doctor"]), ("ヴィカ", []))
check("前後の空白は除く", child("  ヴィカ ")["agent"], "ヴィカ")
check("英字の名前も使える", child("Vika")["agent_h"], "Vika")
check("「・ー」は使える", child("ヴィー・カ")["agent"], "ヴィー・カ")
check("全角の英数字は使える", child("ｖｉｋａ２")["agent"], "ｖｉｋａ２")
# 見えない文字（ハングルの埋め字・幅の無い空白）や、ほかの文字体系の似た形の字は受け付けない（a2a c9d927a6 の指摘）
for bad in ("ヴィ<カ>", "a'b", 'a"b', "x" * 17, "${x}", "$&", "ヴィ カ", "a\\b", "ヴィカ`", "\u115f", "\u1160", "\u3164", "\uffa0",
            "ヴィカ\u200b", "\u200bヴィカ", "Вика", "ヴィカ\u202e", "ヴィカ\u3000カ", "ヴィカ\ufe0f"):
    d = child(bad)
    check(f"使えない名前 {bad!r}: これまでの表示のまま", (d["agent"], d["agent_h"], d["entry"]), ("ハブ", "Hermes", ""))
    check(f"使えない名前 {bad!r}: 診断で知らせる", (len(d["doctor"]), d["doctor"][0][1] if d["doctor"] else None), (1, False))

# ---------- 画面 ----------
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg2", VL_STATE_DIR=f"{TMP}/state2", VL_VOICE_URL="http://127.0.0.1:8796")
for k in ("VL_HUB", "VL_HERMES_KEY", "VL_AGENT_NAME"):
    os.environ.pop(k, None)
sys.path.insert(0, str(ROOT))
import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

import server as s  # noqa: E402

PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"


async def nothing(*a, **k):
    return None


s.boot_ensure_hub = s.keep_gateway_token = s.watch_blocked = s.watch_delegation = s.hermes.watch_hermes_skills = nothing
s.list_agents = lambda: []
s.jsonl_path = lambda a: None
srv = uvicorn.Server(uvicorn.Config(s.app, host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=srv.run, daemon=True).start()
for _ in range(200):
    if srv.started:
        break
    time.sleep(0.05)

HUB = {"pane": "hermes", "key": "hub", "title": "Hermes", "cwd": "", "kind": "hermes", "session": "voice-loop-20261123-abcdef", "reset_hour": 4,
       "status": "idle", "name": "hermes", "hub": True, "hermes": True}
WORKER = {"pane": "w1:p1", "key": "s1", "title": "ハブの記事", "cwd": "/x/blog", "kind": "claude", "session": "s1", "status": "idle", "name": "", "hub": False}


def screen(pg, name):
    entry = dict(HUB, agent_name=name) if name is not None else dict(HUB)
    pg.unroute("**/api/agents")
    pg.route("**/api/agents", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps([entry, WORKER])))
    pg.goto(BASE + "/")
    pg.wait_for_function("[...document.querySelectorAll('#agent option')].filter(o => o.value).length >= 2", timeout=10000)
    return pg.evaluate("""() => ({
      option: [...document.querySelectorAll('#agent option')].filter(o => o.value).map(o => o.textContent),  // 先頭の「選んでください」を除く
      start: document.getElementById('hubStart').textContent,
      tasks: document.querySelector('#tasks .top span').textContent,
      sit: document.querySelector('#situation .top span').textContent,
      memo: H('Hermes の記憶と手順書は残ります'), quote: A('ハブから'),
    })""")


with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 1280, "height": 800})
    pg.route("**/api/prompts", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
    pg.route("**/api/history*", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({"source": "hermes", "messages": []})))

    for name in (None, ""):
        v = screen(pg, name)
        label = "名前なし" if name is None else "空の名前"
        check(f"画面（{label}）: 一覧はこれまでのまま", v["option"][0], "ハブ（Hermes）｜全セッションを統括 [idle]")
        check(f"画面（{label}）: ボタン・見出しはこれまでのまま", (v["start"], v["tasks"], v["sit"]),
              ("ハブを起動", "頼んだ件（ハブ経由で送った指示と、その見張り）", "判断待ち（Hermes から）"))
        check(f"画面（{label}）: 文はこれまでのまま", (v["memo"], v["quote"]), ("Hermes の記憶と手順書は残ります", "ハブから"))

    v = screen(pg, "ヴィカ")
    check("画面（ヴィカ）: 一覧は「ヴィカ｜全セッションを統括」（部品の名前は付けない）", v["option"][0], "ヴィカ｜全セッションを統括 [idle]")
    check("画面（ヴィカ）: セッションの題の「ハブ」は書き換えない", any("ハブの記事" in o for o in v["option"]), True)
    check("画面（ヴィカ）: ボタン・見出し", (v["start"], v["tasks"], v["sit"]),
          ("ヴィカを起動", "頼んだ件（ヴィカ経由で送った指示と、その見張り）", "判断待ち（ヴィカから）"))
    check("画面（ヴィカ）: 「Hermes の」は「ヴィカの」", (v["memo"], v["quote"]), ("ヴィカの記憶と手順書は残ります", "ヴィカから"))
    v = screen(pg, "Vika")
    check("画面（Vika）: 英字の名前は「Vika の」と空けたまま", (v["memo"], v["sit"]), ("Vika の記憶と手順書は残ります", "判断待ち（Vika から）"))

    # 開いたままの画面で、入れ替えで名前が変わる・消える（a2a c9d927a6 の指摘）。名前を受け取る前に描いた問いの見出しも描き直す
    ASK = {"pane": "w1:p1", "name": "ハブの記事", "kind": "question", "text": "どれにしますか？", "fp": "f1", "status": "blocked"}
    pg.unroute("**/api/prompts")
    pg.route("**/api/prompts", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps([ASK])))
    screen(pg, None)
    pg.wait_for_selector(".ask .head", timeout=10000)
    head = lambda: pg.text_content(".ask .head")
    check("問いの見出し（名前なし）", head(), "ハブの記事｜質問で止まっています。端末かハブで答えてください")

    def relist(name, want):
        """名前を返す一覧に替えて読み直させ、先頭の相手の表示を返す。遅い実行環境でも、want になるまで最大 10 秒待つ
        （決め打ちの 0.3 秒では、CI で描き直しの前に読んで落ちた）。ならなければ、そのときの表示を返す（確かめは呼ぶ側）"""
        entry = dict(HUB, agent_name=name) if name is not None else dict(HUB)
        pg.unroute("**/api/agents")
        pg.route("**/api/agents", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps([entry, WORKER])))
        pg.evaluate("loadAgents()")
        first = lambda: pg.evaluate("[...document.querySelectorAll('#agent option')].filter(o => o.value).map(o => o.textContent)[0]")  # noqa: E731
        end = time.time() + 10
        while first() != want and time.time() < end:
            pg.wait_for_timeout(100)
        return first()

    check("開いたまま、名前を受け取る: 一覧", relist("ヴィカ", "ヴィカ｜全セッションを統括 [idle]"), "ヴィカ｜全セッションを統括 [idle]")
    want_head = "ハブの記事｜質問で止まっています。端末かヴィカで答えてください"
    end = time.time() + 10
    while head() != want_head and time.time() < end:  # 描き直しを待つ（遅い実行環境）
        pg.wait_for_timeout(100)
    check("開いたまま、名前を受け取る: 先に描いた問いの見出しも替わる（セッションの名前の「ハブ」は替えない）", head(), want_head)
    check("開いたまま、名前が変わる", (relist("ミカ", "ミカ｜全セッションを統括 [idle]"), pg.text_content("#hubStart")), ("ミカ｜全セッションを統括 [idle]", "ミカを起動"))
    check("開いたまま、名前を消す: これまでの表示に戻る", (relist("", "ハブ（Hermes）｜全セッションを統括 [idle]"), pg.text_content("#hubStart"), head()),
          ("ハブ（Hermes）｜全セッションを統括 [idle]", "ハブを起動", "ハブの記事｜質問で止まっています。端末かハブで答えてください"))
    b.close()

srv.should_exit = True
bad = [r for r in results if not r[1]]
for name, ok, got, want in results:
    print(("ok  " if ok else "NG  ") + name + ("" if ok else f"  got={got!r} want={want!r}"))
print(f"\n{len(results) - len(bad)}/{len(results)} 通過")
sys.exit(1 if bad else 0)
