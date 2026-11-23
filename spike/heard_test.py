"""聞き取りの表記の直し（設定フォルダの heard.txt）の試験。whisper と Hermes は動かさず（Hermes は応答しない場所を指す）、聞き取りのあとの文字と、
ハブの守りの台帳・記録だけを見る。常駐の 8790・本物の whisper・Hermes・herdr には触れない。
確かめること:
  - 辞書の「聞き取りの表記=直す表記」で、声で入った文を直す。長い語から先に当てる。英字は大文字・小文字を区別せず、英単語の一部では直さない。
    大文字・小文字の扱いは正規表現に任せる（ß・合字も、その表記で直す）
  - 行頭に ^ を付けた語は、文の頭の呼びかけ（前に「ねえ」などが来てもよい。後ろに読点・空白・文の終わり）だけで直す
  - 直した語は、もう一度は直さない。行の中の記号は、ただの文字として扱う（正規表現として読まない）
  - ファイルが無い・形の崩れた行・長すぎる行・読めないファイル・201 行目から後は無視する。書き足しは次の発言から効く（再起動なし）
  - 相手の位置: 辞書の行が「はい」を作り出しても（雑音の「肌」が「はい」になる）、承認に答える・閉じるの守りが読む台帳は、直す前の言葉。
    記録には直した文と直す前の言葉を残し、起動のときも直す前の言葉で台帳を戻す。断った発言も同じ
  - WebSocket: 声の turn は直した文を画面とハブに渡す。打った文（type=text）は直さない
使い方: uv run spike/heard_test.py"""
import asyncio
import json
import os
import sys
import tempfile
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
    """声の turn と同じ順: 聞き取り（transcribe。幻の定番文を捨てる）→ 辞書で直す（fix_heard）。"""
    heard["text"] = text
    return s.fix_heard(s.transcribe(b"AUDIO", "a.webm"))


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

    # ---------- 大文字・小文字の扱いは正規表現に任せる（引き当てで別の折り畳みをしない） ----------
    dictionary("Straße=道路\nﬀ=エフ\n")
    check("ß を含む語も、その表記で直す", hear("Straße を渡る"), "道路 を渡る")
    check("合字（ﬀ）も、その表記で直す", hear("ﬀ の字"), "エフ の字")
    dictionary("Wica=ヴィカ\nwica=ビカ\n^Pika=A\n^PIKA=B\n")
    check("大文字・小文字だけ違う行は、後の行が勝つ", hear("WICA と Wica"), "ビカ と ビカ")
    check("大文字・小文字だけ違う行は、後の行が勝つ（文の頭だけの語）", hear("pika、次は"), "B、次は")
    dictionary("Wica=ヴィカ\n")
    check("聞き取り（transcribe）は直さない（直すのは、そのあとの fix_heard）", (heard.update(text="Wica、次は"), s.transcribe(b"AUDIO", "a.webm"))[1], "Wica、次は")

    # ---------- 幻の定番文は直す前に捨てる ----------
    dictionary("ご視聴=ヴィカ\n")
    check("無音の定番文は、辞書より先に空にする", hear("ご視聴ありがとうございました。"), "")

    # ---------- 相手の位置: 守りの台帳には直す前の言葉 ----------
    # 辞書の行が「はい」を作り出しても（雑音の「肌」が「はい」になる、など）、承認に答える・閉じるの守りが読む台帳は、聞き取ったままの言葉。
    # 本物のハブの受け口（hub_accepted → Hermes との往復）を通す。Hermes は動いていない（往復は失敗する）が、台帳と記録には載る
    dictionary("Wica=ヴィカ\n肌=はい\n")
    hub = s.hermes.HERMES_PANE

    def voice(c, text, pane=hub):
        heard["text"] = text
        shown = None
        with c.websocket_connect("ws://127.0.0.1:8790/ws") as ws:
            ws.send_text(json.dumps({"type": "turn", "pane": pane, "speaker": "8", "filename": "a.webm"}))
            ws.send_bytes(b"AUDIO")
            for _ in range(40):
                m = ws.receive_json()
                if m.get("type") == "transcript":
                    shown = m.get("text")
                if m.get("type") in ("error", "reply"):
                    break
        return shown

    def typed(c, text, pane=hub):
        with c.websocket_connect("ws://127.0.0.1:8790/ws") as ws:
            ws.send_text(json.dumps({"type": "text", "text": text, "pane": pane, "speaker": "8"}))
            for _ in range(40):
                m = ws.receive_json()
                if m.get("type") in ("error", "reply"):
                    break

    def last_me():
        rows = [json.loads(line) for line in s.hermes.HERMES_LOG.read_text().splitlines() if line.strip()]
        return [d for d in rows if d.get("role") == "me"][-1]

    with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
        shown = voice(c, "肌")
        check("声: 画面には直した文を出す", shown, "はい")
        check("声: 記録の発言（画面の履歴・Hermes に送る文）は直した文", last_me().get("text"), "はい")
        check("声: 記録には直す前の言葉も残す", last_me().get("said"), "肌")
        check("声: 守りの台帳は直す前の言葉", s.hub_turns(1)[0][1], "肌")
        check("声: 台帳の言葉は、承認に答えるの文法では許すにならない", s.consent.affirms(s.hub_turns(1)[0][1], "answer"), False)
        check("声: 台帳の言葉は、閉じるの文法でも許すにならない", s.consent.affirms(s.hub_turns(1)[0][1], "close"), False)
        s.hermes.HERMES_TURNS.clear()
        s.hermes._load_hermes_turns()
        check("声: 起動のときも、記録の直す前の言葉で台帳を戻す", s.hub_turns(1)[0][1], "肌")

        shown = voice(c, "ねえ、Wica、次は?")
        check("声: 名前を直した文を画面に出す", shown, "ねえ、ヴィカ、次は?")
        check("声: 台帳は直す前の言葉", s.hub_turns(1)[0][1], "ねえ、Wica、次は?")

        shown = voice(c, "次は?")
        check("声: 直さなかった発言は、記録に直す前の言葉を足さない", (shown, "said" in last_me(), s.hub_turns(1)[0][1]), ("次は?", False, "次は?"))

        typed(c, "Wica、肌")
        check("打った文は直さない（記録も台帳も打ったまま）", (last_me().get("text"), "said" in last_me(), s.hub_turns(1)[0][1]),
              ("Wica、肌", False, "Wica、肌"))

    # 断った発言（添付の形が違う）も、記録には直す前の言葉を残す（台帳には空で載る）
    asyncio.run(s.hub_accepted(s.TALKERS[hub], "はい", hub, None, ["../x"], None, "肌"))
    check("断った発言: 台帳は空（承認にも閉じる許可にもならない）", s.hub_turns(1)[0][1], "")
    check("断った発言: 記録には直す前の言葉", (last_me().get("text"), last_me().get("said")), ("はい", "肌"))
    s.hermes.HERMES_TURNS.clear()
    s.hermes._load_hermes_turns()
    check("断った発言: 起動のときは、直す前の言葉で戻す（「はい」で戻さない）", s.hub_turns(1)[0][1], "肌")
finally:
    s._whisper = real_whisper

bad = [r for r in results if not r[1]]
for name, ok, got, want in results:
    print(("ok  " if ok else "NG  ") + name + ("" if ok else f"  got={got!r} want={want!r}"))
print(f"\n{len(results) - len(bad)}/{len(results)} 通過")
sys.exit(1 if bad else 0)
