"""「止める」を押したら、利用者は返事を聞いていない、の試験（承認に答える・閉じるの守り）。
2026-11-24: 顔の表示で、考えている・話しているときに顔を押すと止めるようにした（R12）。止めても頭（Hermes）との往復は最後まで進み、
届いた返事を本体が「利用者が聞いた文」にしていた。返事が「〇〇を閉じていいですか？」なら、聞いていない問いへの「はい」で閉じられうる。
見ること:
  - 読んでいる途中で止めたら、直前に聞いた文と、台帳の最新の返事（閉じるの守りが問いとして読む欄）を空にする
  - 待つ間に止めたら、あとで届いた返事は、聞いた文にも台帳の返事の欄にも置かない（記録には「聞いていない」の印を付けて残す）
  - 止めたあとに始めた往復の返事は、ふつうに聞いた文になる
  - 起動し直しても、聞いていない返事は台帳の返事の欄に戻さない
  - 報告: 頼んだあとに止めたら、届いた報告は聞いた文にしない（止めなければ聞いた文）
  - 画面の「停止」（WebSocket の stop）で、話し相手に止めたことが伝わる
本物の Hermes・herdr・コンテナには触れない（頭の往復は差し替える。VL_HERMES_URL は何も待ち受けていないポート）。
使い方: uv run spike/stop_test.py"""
import asyncio
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HERMES_HOME=f"{TMP}/hermes", VL_HERMES_KEY="k1",
                  VL_HERMES_URL="http://127.0.0.1:9", VL_WHISPER="http://127.0.0.1:9/v1/audio/transcriptions", VL_VOICE_URL="http://127.0.0.1:9")
os.environ.pop("VL_HUB", None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import server as s  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

hm = s.hermes
results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


def log_rows():
    p = Path(hm.HERMES_LOG)
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


Q = "proj のセッションを閉じていいですか？"

# ---- 読んでいる途中で止める
hm.ask_hermes = lambda text: Q
asyncio.run(hm.hermes_exchange("proj を閉じて"))
t1 = s.hub_human_turn()
check("前提: 止めなければ、返事は聞いた文で、台帳の返事の欄にも載る", (hm.HERMES_HEARD["text"], s.hub_reply_to(t1)), (Q, Q))
s.HUB.stopped()
check("読んでいる途中で止めたら、聞いた文と台帳の最新の返事を空にする（閉じるの守りに聞き直してもらう）", (hm.HERMES_HEARD["text"], s.hub_reply_to(t1)), ("", ""))
check("…止めたことを記録に残す（画面の履歴には出さない）", ([r.get("turn") for r in log_rows() if r.get("role") == "stop"], any(m["role"] == "stop" for m in hm.hermes_history())), ([t1], False))

# ---- 待つ間に止める（返事は止めたあとに届く）
gate, entered = threading.Event(), threading.Event()


def slow(text):
    entered.set()
    gate.wait(5)
    return Q


hm.ask_hermes = slow


async def stop_while_waiting():
    task = asyncio.create_task(hm.hermes_exchange("proj を閉じて"))
    await asyncio.to_thread(entered.wait, 5)
    s.HUB.stopped()
    gate.set()
    return await task


reply, why, saved = asyncio.run(stop_while_waiting())
t2 = s.hub_human_turn()
check("待つ間に止めたら、あとで届いた返事は、聞いた文にも台帳の返事の欄にも置かない", (reply, why, hm.HERMES_HEARD["text"], s.hub_reply_to(t2)), (Q, "", "", ""))
last_ai = [r for r in log_rows() if r.get("role") == "ai" and r.get("turn") == t2]
check("…記録には残し、「聞いていない」の印を付ける（画面の履歴では読める）", [(r["text"], r.get("unheard")) for r in last_ai], [(Q, True)])

# ---- 止めたあとに始めた往復
hm.ask_hermes = lambda text: Q
asyncio.run(hm.hermes_exchange("もう一度、proj を閉じて"))
t3 = s.hub_human_turn()
check("止めたあとに始めた往復の返事は、ふつうに聞いた文になる", (hm.HERMES_HEARD["text"], s.hub_reply_to(t3)), (Q, Q))

# ---- 起動し直し
hm.HERMES_TURNS.clear()
hm._load_hermes_turns()
check("起動し直しても、止めた・聞いていない返事は台帳の返事の欄に戻さない（聞いた返事は戻す）", (s.hub_reply_to(t1), s.hub_reply_to(t2), s.hub_reply_to(t3)), ("", "", Q))

# ---- 報告
s.HUB.report_begin("proj｜t が完了しました", "proj｜t")
s.HUB.stopped()
asyncio.run(s.HUB.report_end("done", "proj の報告です。閉じていいですか？", "proj｜t", True))
check("報告を頼んだあとに止めたら、届いた報告は聞いた文にしない", hm.HERMES_HEARD["text"], "")
s.HUB.report_begin("proj｜t が完了しました", "proj｜t")
asyncio.run(s.HUB.report_end("done", "proj の報告です。", "proj｜t", True))
check("止めていない報告は、聞いた文になる", hm.HERMES_HEARD["text"], "proj の報告です。")


# ---- 画面の「停止」（WebSocket の stop）で、話し相手に伝わる
async def nothing(*a, **k):
    return None


s.boot_ensure_hub = s.keep_gateway_token = s.watch_blocked = s.watch_delegation = s.hermes.watch_hermes_skills = nothing
s.list_agents = lambda: []
hm.HERMES_HEARD["text"] = Q
before = hm.HERMES_STOP["at"]
with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8790"}) as w:
        w.send_text(json.dumps({"type": "stop"}))
        got = None
        for _ in range(20):
            m = json.loads(w.receive_text())
            if m.get("type") == "stopped":
                got = m
                break
check("画面の「停止」で、止めたことが話し相手に伝わる（聞いた文を空にし、止めた時刻を進める）",
      (got is not None, hm.HERMES_HEARD["text"], hm.HERMES_STOP["at"] > before), (True, "", True))

bad = [r for r in results if not r[1]]
for name, ok, got, want in results:
    print(("ok  " if ok else "NG  ") + name + ("" if ok else f"  got={got!r} want={want!r}"))
print(f"\n{len(results) - len(bad)}/{len(results)} 件が期待どおり")
sys.exit(1 if bad else 0)
