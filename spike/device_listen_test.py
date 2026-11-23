"""機械の口の受け口（server.py の serve_all・device_servers）を、本物の uvicorn で立てて確かめる。画面の口は 127.0.0.1:8791、機械の口は 8792
（常駐の 8790・herdr・Hermes・docker には触れない。起動のときの裏の仕事は差し替える）。
確かめること:
  - 機械の口（VL_DEVICE_PORTS）を書くと、画面の口と一緒に、機械の口でも 127.0.0.1 だけで待つ。書かなければ機械の口は立てない
  - 起動の処理（lifespan）は 1 回だけ（機械の口の受け口では動かさない。見張りを二重にしない）
  - 機械の口では画面も API も 403、WebSocket は機械として受ける（置き場所は口で決まる）。画面の口は、これまでどおり
  - 画面の口の受け口を止めると、機械の口の受け口も止まる
使い方: uv run spike/device_listen_test.py"""
import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.pop("VL_PORT", None)
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HERMES_URL="http://127.0.0.1:9", VL_HERMES_HOME=f"{TMP}/no-hermes",
                  VL_DEVICE_PORTS="home=8792")
os.environ.pop("VL_HERMES_KEY", None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import uvicorn  # noqa: E402

import server as s  # noqa: E402

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


boots = []


async def counted(*a, **k):
    boots.append(1)


async def nothing(*a, **k):
    return None


s.boot_ensure_hub = counted  # 起動の処理が動いた回数（lifespan が動くたびに 1 回）
s.keep_gateway_token = s.watch_blocked = s.hermes.watch_hermes_skills = nothing
s.HUB.watchers = lambda: []
s.list_agents = lambda: []


def status(url, headers=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return "つながらない"


def listening(port):
    with socket.socket() as so:
        so.settimeout(0.5)
        return so.connect_ex(("127.0.0.1", port)) == 0


check("書かなければ、機械の口の受け口は作らない", (os.environ.update(VL_DEVICE_PORTS=""), len(s.device_servers(uvicorn)))[1], 0)
os.environ["VL_DEVICE_PORTS"] = "home=8792"
extras = s.device_servers(uvicorn)
check("書けば、機械の口ごとに 1 つ・127.0.0.1 で待つ・起動の処理は動かさない",
      [(x.config.host, x.config.port, x.config.lifespan) for x in extras], [("127.0.0.1", 8792, "off")])

main = uvicorn.Server(uvicorn.Config(s.app, host="127.0.0.1", port=8791, proxy_headers=False, log_level="warning"))
for x in extras:
    x.config.log_level = "warning"
loop = asyncio.new_event_loop()
th = threading.Thread(target=lambda: loop.run_until_complete(s.serve_all(main, extras)), daemon=True)
th.start()
for _ in range(200):
    if main.started and all(x.started for x in extras):
        break
    time.sleep(0.05)
check("画面の口と機械の口の両方で待つ", (listening(8791), listening(8792)), (True, True))
time.sleep(0.3)
check("起動の処理（lifespan）は 1 回だけ", len(boots), 1)
check("機械の口: 画面は 403", status("http://127.0.0.1:8792/"), 403)
check("機械の口: API は 403", status("http://127.0.0.1:8792/api/agents"), 403)
check("画面の口: これまでどおり", status("http://127.0.0.1:8791/api/agents"), 200)

try:
    from websockets.sync.client import connect  # noqa: E402
    before = set(s.CLIENTS)
    with connect("ws://127.0.0.1:8792/ws", open_timeout=5) as ws:
        cl = None
        for _ in range(100):
            new = [c for c in s.CLIENTS if c not in before]
            if new:
                cl = new[0]
                break
            time.sleep(0.02)
        check("機械の口の WebSocket は、機械として受け、置き場所は口で決まる", (cl.kind, cl.place) if cl else None, ("device", "home"))
except ImportError:
    print("（websockets が無いので、WebSocket の確かめは飛ばした）")

main.should_exit = True
th.join(10)
check("画面の口の受け口を止めると、機械の口の受け口も止まる", (th.is_alive(), listening(8791), listening(8792)), (False, False, False))

bad = [r for r in results if not r[1]]
for name, ok, got, want in results:
    print(("ok  " if ok else "NG  ") + name + ("" if ok else f"  got={got!r} want={want!r}"))
print(f"\n{len(results) - len(bad)}/{len(results)} 通過")
sys.exit(1 if bad else 0)
