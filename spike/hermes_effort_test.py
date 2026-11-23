"""Hermes に頼むときの考える強さ（config.env の VL_HERMES_EFFORT）の試験。Hermes の API は差し替え、送る要求の中身だけを見る。
常駐の 8790・本物の Hermes・コンテナには触れない。
確かめること:
  - 書かなければ、要求に model_options を付けない（Hermes の設定のまま。これまでと同じ要求）
  - none・minimal・low・medium・high・xhigh・max なら、model_options.reasoning.effort に入れて渡す（大文字・前後の空白は整える）
  - 知らない値は渡さず、診断で知らせる（書かないときと、使える値のときは知らせない）
  - 本体の記録の行に、渡した強さを残す（入れる前と後の速さを比べるため）
使い方: uv run spike/hermes_effort_test.py"""
import contextlib
import importlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HERMES_KEY="k1", VL_HERMES_URL="http://127.0.0.1:9", VL_HERMES_HOME=f"{TMP}/no-hermes")  # 置き場が無い（コンテナの台本を動かさない）
os.environ.pop("VL_HERMES_EFFORT", None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import server as s  # noqa: E402

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


class Reply:
    def __init__(self, body): self.body = body
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self.body


def ask_with(value):
    """VL_HERMES_EFFORT を value にして読み直し、ask_hermes が送る要求の中身と、記録の行と、診断の項目を返す。"""
    if value is None:
        os.environ.pop("VL_HERMES_EFFORT", None)
    else:
        os.environ["VL_HERMES_EFFORT"] = value
    h = importlib.reload(s.hermes)
    h.core = s  # 読み直すと本体への結び（setup）が消えるので、結びだけ戻す（起動の続きの処理は動かさない）
    sent = []

    def fake(req, timeout=None):
        sent.append(json.loads(req.data))
        return Reply(json.dumps({"choices": [{"message": {"content": "はい。"}, "finish_reason": "stop"}]}).encode())

    real = h.urllib.request.urlopen
    h.urllib.request.urlopen = fake
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            reply = h.ask_hermes("こんにちは")
    finally:
        h.urllib.request.urlopen = real
    items = []
    h.TALKER.doctor(lambda name, ok, detail="", optional=False: items.append((name, ok, detail)))
    return reply, sent[0] if sent else None, out.getvalue(), [i for i in items if i[0] == "Hermes の考える強さ"]


try:
    reply, body, log, doc = ask_with(None)
    check("書かなければ: 返事はそのまま", reply, "はい。")
    check("書かなければ: model_options を付けない（これまでと同じ要求）", "model_options" in body, False)
    check("書かなければ: 要求の形はこれまでどおり", (body["model"], body["stream"], [m["role"] for m in body["messages"]][-1]), ("hermes-agent", False, "user"))
    check("書かなければ: 記録の行に強さを書かない", "考える強さ" in log, False)
    check("書かなければ: 診断で知らせない", doc, [])

    for value, want in [("low", "low"), ("none", "none"), (" LOW ", "low"), ("medium", "medium"), ("max", "max"), ("minimal", "minimal")]:
        reply, body, log, doc = ask_with(value)
        check(f"{value!r}: model_options.reasoning.effort に入れて渡す", body.get("model_options"), {"reasoning": {"effort": want}})
        check(f"{value!r}: 記録の行に強さを残す", f"考える強さ {want}" in log, True)
        check(f"{value!r}: 診断で知らせない", doc, [])

    for value in ("fast", "ultra", "low; rm -rf", "最低", ""):
        reply, body, log, doc = ask_with(value)
        check(f"{value!r}: 知らない値は渡さない", "model_options" in body, False)
        check(f"{value!r}: 診断で知らせる（空は知らせない）", (len(doc), doc[0][1] if doc else None), ((0, None) if value == "" else (1, False)))
finally:
    os.environ.pop("VL_HERMES_EFFORT", None)
    importlib.reload(s.hermes).core = s

bad = [r for r in results if not r[1]]
for name, ok, got, want in results:
    print(("ok  " if ok else "NG  ") + name + ("" if ok else f"  got={got!r} want={want!r}"))
print(f"\n{len(results) - len(bad)}/{len(results)} 通過")
sys.exit(1 if bad else 0)
