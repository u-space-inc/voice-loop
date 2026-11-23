"""聞き取りの表記の直し（設定フォルダの heard.txt）の試験。whisper は差し替え、聞き取りのあとの文字だけを見る。常駐の 8790・本物の whisper には触れない。
確かめること:
  - 辞書の「聞き取りの表記=直す表記」で、声で入った文を直す。長い語から先に当てる。英字は大文字・小文字を区別せず、英単語の一部では直さない
  - 行頭に ^ を付けた語は、文の頭の呼びかけ（前に「ねえ」などが来てもよい。後ろに読点・空白・文の終わり）だけで直す
  - 直した語は、もう一度は直さない。行の中の記号は、ただの文字として扱う（正規表現として読まない）
  - ファイルが無い・形の崩れた行・長すぎる行は無視する（いまと同じ動き）。書き足しは次の発言から効く（再起動なし）
  - 相手の位置: 辞書の行が「はい」などの答えの言葉や番号を作り出す・断る言葉を消す・プロジェクト名の前置きを作る場合は、直さない
    （雑音や関係のない発言が、辞書を通って承認・閉じるの守りを通らないように）
  - WebSocket: 声の turn は直した文を画面と相手に渡す。打った文（type=text）は直さない
使い方: uv run spike/heard_test.py"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HERMES_KEY="k1", VL_HERMES_URL="http://127.0.0.1:9")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import server as s  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


def dictionary(text):
    s.HEARD_FILE.parent.mkdir(parents=True, exist_ok=True)
    s.HEARD_FILE.write_text(text)


heard = {"text": ""}
real_whisper = s._whisper
s._whisper = lambda audio, filename, timeout=120, vad=None: {"text": heard["text"]}


def hear(text):
    heard["text"] = text
    return s.transcribe(b"AUDIO", "a.webm")


try:
    # ---------- 辞書が無い ----------
    if s.HEARD_FILE.exists():
        s.HEARD_FILE.unlink()
    check("辞書が無ければ、そのまま", hear("ねえ、Wica、今どうなってる?"), "ねえ、Wica、今どうなってる?")

    dictionary("""# 聞き取りの表記=直す表記
ウィーカー=ヴィカ
ウィカー=ヴィカ
ウィカ=ヴィカ
Wica=ヴィカ
Weekar=ヴィカ
^ピッカ=ヴィカ   # 文の頭の呼びかけだけ
""")
    # ---------- どこでも直す語 ----------
    for said, want in [
        ("ねえ、Wica、今どうなってる?", "ねえ、ヴィカ、今どうなってる?"),
        ("ウィカー止めて", "ヴィカ止めて"),  # 長い語から先（「ヴィカー」にしない）
        ("ウィーカー止めて", "ヴィカ止めて"),
        ("その件はウィカに任せて", "その件はヴィカに任せて"),
        ("Weekar、今どうなってる?", "ヴィカ、今どうなってる?"),
        ("WICA、次は?", "ヴィカ、次は?"),  # 大文字・小文字を区別しない
        ("wica と Wica", "ヴィカ と ヴィカ"),
        ("Wicapedia を開いて", "Wicapedia を開いて"),  # 英単語の一部では直さない
        ("AWica の件", "AWica の件"),
        ("ウィカとウィカ", "ヴィカとヴィカ"),
    ]:
        check(f"どこでも: {said}", hear(said), want)

    # ---------- 文の頭の呼びかけだけ ----------
    for said, want in [
        ("ピッカ、次は", "ヴィカ、次は"),
        ("ねえピッカ、次は", "ねえヴィカ、次は"),
        ("ねえ、ピッカ、次は", "ねえ、ヴィカ、次は"),
        ("ピッカ", "ヴィカ"),
        ("ピッカ 次は", "ヴィカ 次は"),
        ("ピッカピカだね", "ピッカピカだね"),  # 後ろに読点・空白・文の終わりが無い
        ("それはピッカ、だね", "それはピッカ、だね"),  # 文の頭ではない
        ("ピッカ、ピッカ", "ヴィカ、ピッカ"),  # 直すのは文の頭の 1 つだけ
    ]:
        check(f"文の頭だけ: {said}", hear(said), want)

    # ---------- 直した語は重ねて直さない・行の記号はただの文字 ----------
    dictionary("^ピッカ=ウィカ\nウィカ=ヴィカ\n.*=X\na|b=Y\n")
    check("文の頭で直した語に、どこでも直す語を重ねない", hear("ピッカ、ウィカ"), "ウィカ、ヴィカ")
    check("行の記号は正規表現として読まない（.* は文字の .*）", hear("それは .* です"), "それは X です")
    check("行の記号は正規表現として読まない（a|b は文字の a|b）", hear("a b a|b"), "a b Y")

    # ---------- 形の崩れた行・長すぎる行は無視 ----------
    dictionary("\n".join(["=ヴィカ", "ウィカ=", "ウィカ", "^=ヴィカ", "同じ=同じ", "# Wica=ヴィカ", "あ" * 41 + "=い", "う=" + "え" * 41, "Weekar=ヴィカ"]))
    check("形の崩れた行は無視する", hear("ウィカ、Wica、同じ"), "ウィカ、Wica、同じ")
    check("長すぎる行は無視する", hear("あ" * 41 + "、う"), "あ" * 41 + "、う")
    check("崩れた行があっても、正しい行は効く", hear("Weekar、次は?"), "ヴィカ、次は?")
    s.HEARD_FILE.write_bytes(b"\xff\xfe\x00bad")
    check("読めない辞書は無視する", hear("Weekar、次は?"), "Weekar、次は?")
    dictionary("\n".join(["x=y"] * 300) + "\nWeekar=ヴィカ")
    check("読むのは先頭の 200 行まで", hear("Weekar"), "Weekar")

    # ---------- 書き足しは次の発言から ----------
    dictionary("Wica=ヴィカ\n")
    check("書き足す前", hear("ビカ、次は"), "ビカ、次は")
    dictionary("Wica=ヴィカ\n^ビカ=ヴィカ\n")
    check("書き足したら、再起動なしで次の発言から効く", hear("ビカ、次は"), "ヴィカ、次は")

    # ---------- 相手の位置: 辞書で答えの言葉を作らない ----------
    dictionary("""ウィカ=ヴィカ
肌=はい
^ピッカ=許可して
ダメ=ヴィカ
いち=1
ボイス=voice-loop
二=ニ
""")
    check("「はい」を作り出す行では直さない（雑音の「肌」が承認にならない）", hear("肌"), "肌")
    check("文の頭で「許可して」を作り出す行では直さない", hear("ピッカ"), "ピッカ")
    check("断る言葉を消す行では直さない（「ダメ」を消して許すにしない）", hear("ウィカ、ダメ"), "ウィカ、ダメ")
    check("番号を作り出す行では直さない", hear("いち番"), "いち番")
    check("番号を消す行では直さない", hear("二番"), "二番")
    check("直した語がプロジェクト名の前置きになって許す読みになる行では直さない", hear("ボイスはい"), "ボイスはい")
    check("答えの読みが変わらなければ直す（「はい」はそのまま、名前だけ直す）", hear("ウィカ、次は"), "ヴィカ、次は")
    check("答えの読みが変わらなければ直す（断る文の名前）", hear("ウィカ、だめだよ"), "ヴィカ、だめだよ")
    check("名前を直しても、許す読みにはならない（もとから許す読みではない）", hear("ウィカ、はい"), "ヴィカ、はい")
    check("辞書に当たらない発言は、そのまま", hear("はい"), "はい")

    # ---------- 幻の定番文は直す前に捨てる ----------
    dictionary("ご視聴=ヴィカ\n")
    check("無音の定番文は、辞書より先に空にする", hear("ご視聴ありがとうございました。"), "")

    # ---------- WebSocket: 声だけ直す ----------
    dictionary("Wica=ヴィカ\n")
    turns = []
    real_accepted = s.hub_accepted

    async def fake_accepted(talker, text, pane, *a, **k):  # ハブが受け取る文だけを控え、往復はしない
        turns.append(text)
        return "", "試験のため送っていません", True

    s.hub_accepted = fake_accepted
    try:
        heard["text"] = "ねえ、Wica、次は?"
        with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
            with c.websocket_connect("ws://127.0.0.1:8790/ws") as ws:
                ws.send_text(json.dumps({"type": "turn", "pane": s.hermes.HERMES_PANE, "speaker": "8", "filename": "a.webm"}))
                ws.send_bytes(b"AUDIO")
                shown = None
                for _ in range(20):
                    m = ws.receive_json()
                    if m.get("type") == "transcript":
                        shown = m.get("text")
                        break
                ws.send_text(json.dumps({"type": "text", "text": "Wica、次は?", "pane": s.hermes.HERMES_PANE, "speaker": "8"}))
                for _ in range(50):
                    if len(turns) >= 2:
                        break
                    time.sleep(0.02)
        check("声の turn: 画面には直した文を出す", shown, "ねえ、ヴィカ、次は?")
        check("声の turn: 相手には直した文を渡し、打った文は直さない", turns, ["ねえ、ヴィカ、次は?", "Wica、次は?"])
    finally:
        s.hub_accepted = real_accepted
finally:
    s._whisper = real_whisper

bad = [r for r in results if not r[1]]
for name, ok, got, want in results:
    print(("ok  " if ok else "NG  ") + name + ("" if ok else f"  got={got!r} want={want!r}"))
print(f"\n{len(results) - len(bad)}/{len(results)} 通過")
sys.exit(1 if bad else 0)
