"""Hermes の返事が、モデルの安全の仕組み（content filter）に止められたときの決まり文句だったときの試験。
2026-11-23 22:11、報告の回に「I'm sorry, but I cannot assist with that request.」が返り、本体はふつうの報告として読み上げた
（Hermes は finish_reason=stop で返す。Hermes の記録では finish_reason=content-filter）。
返事の全体がこの決まり文句なら、本体は失敗として扱う（報告なら「報告を受け取れませんでした」、発言なら画面に ⚠）。
偽の Hermes（127.0.0.1:8792）を立て、本物の受け取りの処理（ask_hermes）を通す。本物の Hermes・herdr・コンテナには触れない。
使い方: uv run spike/hermes_refusal_test.py"""
import asyncio
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HERMES_HOME=f"{TMP}/hermes", VL_HERMES_KEY="k1",
                  VL_HERMES_URL="http://127.0.0.1:8792")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server as s  # noqa: E402

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


REFUSAL = "I'm sorry, but I cannot assist with that request."
said = {"content": REFUSAL, "asked": 0}


class FakeHermes(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        said["asked"] += 1
        body = json.dumps({"choices": [{"message": {"role": "assistant", "content": said["content"]}, "finish_reason": "stop"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


threading.Thread(target=HTTPServer(("127.0.0.1", 8792), FakeHermes).serve_forever, daemon=True).start()


def log_rows():
    p = Path(TMP) / "state" / "hermes-log.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# ---- 決まり文句の見分け（返事の全体がその形のときだけ）
for text in (REFUSAL, "I’m sorry, but I can’t assist with that request.", "I'm sorry, but I can't help with that.", "  i'm sorry but I cannot assist with this request  "):
    check(f"決まり文句と見分ける: {text.strip()[:40]}", s.hermes.refusal(text), True)
for text in ("わかりました。brexa の件を確かめます", REFUSAL + " However, here is the summary.", "Error: I'm sorry, but I cannot assist with that request.", ""):
    check(f"決まり文句でない: {text[:40] or '（空）'}", s.hermes.refusal(text), False)

# ---- 受け取りの処理（本物の ask_hermes。偽の Hermes は finish_reason=stop で返す）
try:
    got = ("返った", s.hermes.ask_hermes("こんにちは"))
except RuntimeError as e:
    got = ("失敗", "安全の仕組み" in str(e) and "I'm sorry" not in str(e))
check("決まり文句は、返事として返さず失敗にする（理由は日本語）", got, ("失敗", True))
said["content"] = "わかりました。確かめます"
check("ふつうの返事は、そのまま返す", s.hermes.ask_hermes("こんにちは"), "わかりました。確かめます")

# ---- ふつうの発言: 画面には ⚠ の理由が出て、読み上げる返事は無い。台帳の聞いた文も空にする
said["content"] = REFUSAL
s.hermes.HERMES_HEARD["text"] = "前に聞いた問い（閉じていいですか？）"
reply, why, saved = asyncio.run(s.hermes.hermes_exchange("brexa の結果を教えて"))
check("発言への返事が決まり文句なら、返事は空で、理由を出す", (reply, "安全の仕組み" in why), ("", True))
check("…利用者が聞くのは失敗の知らせなので、前に聞いた問いを空にする", s.hermes.HERMES_HEARD["text"], "")
rows = log_rows()
check("…記録には失敗として残し、英語の断り文を返事として残さない",
      (rows[-1]["role"], "安全の仕組み" in rows[-1]["text"], any(r["role"] == "ai" and "I'm sorry" in r.get("text", "") for r in rows)), ("error", True, False))

# ---- 報告の回: 本体の ask は失敗（error）を返し、報告の記録は「報告を受け取れませんでした」になる
state, msg = asyncio.run(s.ask({"pane": s.hermes.HERMES_PANE}, "[voice-loop 通知] blog｜記事 が完了しました ‖ ユーザーに声で報告してください。"))
check("報告の回で決まり文句が返ったら、失敗（error）として返す", (state, "安全の仕組み" in msg), ("error", True))
asyncio.run(s.HUB.report_end(state, msg, "blog｜記事", True))
rows = log_rows()
check("…報告の記録は「報告を受け取れませんでした」と理由になる（読み上げる報告は残さない）",
      (rows[-1]["role"], "報告を受け取れませんでした" in rows[-1]["text"], "安全の仕組み" in rows[-1]["text"],
       any(r.get("report") for r in rows)), ("error", True, True, False))
check("偽の Hermes に届いた回数（毎回 1 度だけ聞き、聞き直さない）", said["asked"], 4)

bad = [r for r in results if not r[1]]
for name, ok, got, want in results:
    print(("ok  " if ok else "NG  ") + name + ("" if ok else f"  got={got!r} want={want!r}"))
print(f"\n{len(results) - len(bad)}/{len(results)} 件が期待どおり")
sys.exit(1 if bad else 0)
