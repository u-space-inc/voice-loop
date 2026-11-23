"""ハブの頭としての Hermes（herdr の外、コンテナの中の API サーバー）。

本体との約束は scripts/hub_brain.py。Hermes に固有のこと（会話の名前・記録・発言の台帳、手順の書き足しと記憶を画面で扱う口）はここに置き、
本体（server.py）は約束の形でだけ呼ぶ。本体の道具は core から呼ぶ（server.py が読み込みのあとで core に自分を置く）。
守りの決まりは README「守り」のとおりで、この分け方で変わらない。"""
import asyncio
import json
import os
import re
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections import OrderedDict, deque
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import attach  # 画面から添付したファイルを Hermes のコンテナの中に置く
import hermes_memory  # Hermes の記憶を画面（PC の「記憶」）で足す・直す・消す
import hermes_check  # 判断待ちを 1 件ずつ新しい会話で確かめる（層 3。VL_HERMES_CHECK=on のときだけ）
import hermes_inbox  # 判断待ちの受け箱（画面の 💡）。Hermes の見張りの記録を読み、済んだ・あとでを受ける
import hermes_learn  # Hermes の学びを自動で当て、跡を残し、画面の「戻す」で戻す
import hermes_skills  # Hermes の手順の書き足しを画面で採る・捨てる、Hermes の記憶を読む
import topics  # 件（頼んだ件・報告・Slack のスレッド・問いを結ぶ単位）
from hub_brain import Brain, Talker
from places import CONFIG_DIR, STATE_DIR

core = None  # 本体（server.py のモジュール）。使ってよい名前は hub_brain.CORE_TOOLS
router = APIRouter()

# Hermes を話し相手に足すとき（README「Hermes を話し相手にする」）。鍵を書くと一覧に出る。コンテナの中の API サーバーに送り、
# 会話は HERMES_SESSION を元にした名前で続く。名前は毎朝 HERMES_RESET_HOUR 時に変わり（前の日の会話は持ち越さない）、画面の「新しくする」でも変わる
# （hermes_session）。記憶と手順書は Hermes の側に残る。Hermes から本体へは host.docker.internal 経由で、鍵つきの hubctl の口だけ（origin_guard）
# Hermes がハブ（読む・送る・答える・立てる・閉じる。守りは本体側）。0.22.0 で Claude Code のハブを外し、ハブの頭は Hermes だけになった（VL_HUB は使わない）
HERMES_URL = os.environ.get("VL_HERMES_URL", "http://127.0.0.1:8642").rstrip("/")
HERMES_KEY = os.environ.get("VL_HERMES_KEY", "")
HERMES_SESSION = os.environ.get("VL_HERMES_SESSION", "voice-loop")
# 会話を新しくする時刻（0〜23）。空か off なら日ごとには変えない（画面の「新しくする」だけ）。
# Hermes は会話の始まりに記憶を読み込み、会話が長いほど1回ごとに読み直す量が増えるので、日ごとに新しくする（README「Hermes を話し相手にする」）
_reset = os.environ.get("VL_HERMES_RESET_HOUR", "4").strip().lower()
HERMES_RESET_HOUR = int(_reset) if _reset.isdigit() and 0 <= int(_reset) <= 23 else None
HERMES_SESSION_FILE = STATE_DIR / "hermes-session.json"  # いまの会話の印 {"day": "20261026", "token": "a1b2c3"}
HERMES_PANE = "hermes"  # 画面が話し相手を指す鍵。herdr の pane ではない
HERMES_DIR = CONFIG_DIR / "hermes"  # Hermes への指示文（hub.md）・道具（hubctl）・鍵。コンテナに /opt/voice-loop として読み取り専用で見せる
# Hermes の置き場（ホストの側。コンテナの /opt/data）とコンテナの名前。手順の書き足しを画面で採る・捨てるのに使う
HERMES_HOME = Path(os.environ.get("VL_HERMES_HOME") or Path.home() / ".hermes").expanduser()
HERMES_CONTAINER = os.environ.get("VL_HERMES_CONTAINER", "hermes")
hermes_inbox.HOME, hermes_inbox.CONTAINER = HERMES_HOME, HERMES_CONTAINER


def hermes_problem() -> str:
    """Hermes がハブのとき、使える状態でなければその理由。空なら使える。"""
    if not HERMES_KEY:
        return "config.env に VL_HERMES_KEY が無いので、Hermes をハブにできません（README「Hermes をハブにする」）"
    try:
        urllib.request.urlopen(f"{HERMES_URL}/health", timeout=5).read()
    except Exception:
        return f"Hermes の API サーバーが {HERMES_URL} で応答しません。コンテナを起動してください（README「Hermes をハブにする」）"
    missing = [n for n in ("hub.md", "hubctl", "hub_key") if not (HERMES_DIR / n).is_file()]
    return f"Hermes への指示文・道具・鍵が揃っていません（{', '.join(missing)}）。プラグインの start を実行してください" if missing else ""


def hermes_day(now: datetime | None = None) -> str:
    """会話の日付。HERMES_RESET_HOUR 時より前は前の日に数える。日ごとに変えないなら空。"""
    if HERMES_RESET_HOUR is None:
        return ""
    return ((now or datetime.now()) - timedelta(hours=HERMES_RESET_HOUR)).strftime("%Y%m%d")


_session_mem: dict = {}  # この本体が使っている会話の印 {"day", "token"}。状態を書けないときも、動いている間は同じ名前で続ける
_session_lock = threading.Lock()  # 印を読む・決める・書く・覚えるを一度にひとつ（一覧の読み出しと「新しくする」が重なっても、古い印で上書きしない）
_TOKEN = re.compile(r"[0-9a-f]{6}")


def _session_token(day: str) -> str:
    """その日の会話の印。会話ごとに新しい乱数の印にして、前に使った名前へは戻らない（Hermes は同じ名前の会話を続けるので、
    状態が壊れた・消えたときに回数のような数え直せる印や、印なしの名前に戻すと、捨てた会話に戻ってしまう）。
    状態が無い・壊れている・別の日なら新しい印を作る（日ごとに変えない設定でも同じ）。"""
    with _session_lock:
        if _session_mem.get("day") == day:
            return _session_mem["token"]
        try:
            d = json.loads(HERMES_SESSION_FILE.read_text())
        except (OSError, ValueError, RecursionError):  # 無い・読めない・壊れている
            d = None
        tok = d.get("token") if isinstance(d, dict) and d.get("day") == day else None
        if not (isinstance(tok, str) and _TOKEN.fullmatch(tok)):
            tok = os.urandom(3).hex()
            try:
                core.write_json(HERMES_SESSION_FILE, {"day": day, "token": tok})
            except OSError:
                # 書けなくても、動いている間はこの印で続ける（ハブを止めない）。この間に利用者が会話を捨てることはない（「新しくする」は
                # 書けなければ断る）。直ってから起動し直すと、保存されている会話（利用者が最後に選んだ会話）に戻る
                traceback.print_exc()
        _session_mem.update(day=day, token=tok)
        return tok


def _session_name(day: str, tok: str) -> str:
    return "-".join([HERMES_SESSION] + ([day] if day else []) + [tok])


def hermes_session(now: datetime | None = None) -> str:
    """いまの Hermes の会話の名前（X-Hermes-Session-Id）。元の名前＋日付（日ごとに変えるとき）＋会話の印。"""
    day = hermes_day(now)
    return _session_name(day, _session_token(day))


def hermes_session_new(now: datetime | None = None) -> str:
    """画面の「新しくする」。新しい印にして、新しい名前を返す。書けなければ OSError（名前は変わらない）。"""
    day, tok = hermes_day(now), os.urandom(3).hex()
    with _session_lock:
        core.write_json(HERMES_SESSION_FILE, {"day": day, "token": tok})
        _session_mem.update(day=day, token=tok)
    return _session_name(day, tok)


def hermes_agent() -> dict:
    """話し相手の一覧に載せる Hermes の項目（ハブ）。herdr のセッションではないので、pane も key も固定の名前。"""
    return {"pane": HERMES_PANE, "key": "hub", "title": "Hermes", "cwd": "", "kind": "hermes",
            "session": hermes_session(), "reset_hour": HERMES_RESET_HOUR, "status": "idle", "name": "hermes", "hub": True, "hermes": True}


# Hermes との会話の記録。Claude Code のセッションが自分で書く記録（JSONL）にあたるものを、本体が状態フォルダに書く。
# 画面の履歴はここから描く（画面が閉じていた間の返答や報告も残る）。ハブかどうかに関係なく書く（Hermes の会話の名前が変わっても1本に続けて書く）。
# 1行に1件: me（ユーザーの発言）・ai（Hermes の返答。報告なら report と about）・note（自動通知の見出し）・error（届かなかった・返事が無かった）
HERMES_LOG = STATE_DIR / "hermes-log.jsonl"
HERMES_LOG_MAX = 8_000_000  # 超えたら hermes-log.1.jsonl に回す（一つ前の分だけ残す）


def hermes_log(role: str, text: str, **extra) -> bool:
    """1件を書き足す。書けなかったら False（会話は止めない。呼び出し側が画面に知らせる）。"""
    line = (json.dumps({"at": time.time(), "role": role, "text": text, **extra}, ensure_ascii=False) + "\n").encode()
    try:
        HERMES_LOG.parent.mkdir(parents=True, exist_ok=True)
        if HERMES_LOG.exists() and HERMES_LOG.stat().st_size > HERMES_LOG_MAX:
            HERMES_LOG.replace(HERMES_LOG.with_name("hermes-log.1.jsonl"))
        with HERMES_LOG.open("ab+") as f:
            if f.seek(0, os.SEEK_END):  # 前の書き込みが途中で切れて末尾に改行が無ければ、つなげずに次の行から書く
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    line = b"\n" + line
            f.write(line)
        return True
    except OSError:
        traceback.print_exc()
        return False


def hermes_records(tail_bytes: int = 2_000_000) -> list[dict] | None:
    """記録の行（壊れた行は飛ばす）。記録そのものを読めなければ None。"""
    try:
        lines = core.read_lines(HERMES_LOG, tail_bytes=tail_bytes) if HERMES_LOG.exists() else []
    except OSError:
        traceback.print_exc()
        return None
    out = []
    for line in lines:
        try:
            d = json.loads(line)
        except (ValueError, RecursionError):  # 壊れた行・入れ子が深すぎる行
            continue
        if isinstance(d, dict) and isinstance(d.get("role"), str) and isinstance(d.get("text"), str):
            out.append(d)
    return out


def hermes_history(limit: int = 40) -> list[dict]:
    """画面に出す Hermes との会話。形は history() と同じ（role と text）。"""
    records = hermes_records()
    if records is None:
        return [{"role": "error", "text": f"Hermes との会話の記録を読めませんでした（{HERMES_LOG.name}。本体のログを見てください）"}]
    out = []
    for d in records:
        item = d.get("reply_item") if d["role"] == "me" and isinstance(d.get("reply_item"), int) and not isinstance(d.get("reply_item"), bool) else None
        if d["role"] not in ("me", "ai", "note", "error") or not (d["text"] or item):
            continue
        m = {"role": d["role"], "text": d["text"] or "（ことばなし）"}
        if d["role"] == "ai" and d.get("report"):  # 頼んだ件の報告: 画面が「どの件の報告か」を出し、返信で記録の時刻を指す
            m.update(report=True, about=str(d.get("about") or ""), at=d.get("at"))
            if isinstance(d.get("topic"), str) and topics.ID_RE.fullmatch(d["topic"]):  # どの件の報告か（画面が件の URL で開くときに探す）
                m["topic"] = d["topic"]
        if d["role"] == "me" and d.get("reply_to"):  # 報告への返信として送った発言
            m["reply_to"] = str(d["reply_to"])
        if item:  # 受け箱の判断待ちへの返信として送った発言
            m["reply_item"] = item
        out.append(m)
    return out[-limit:]


# 返信先の報告の名前（件の題。セッションの題などから本体が作ったもの）に入れない文字: 制御文字・向きを変える文字・前置きの括弧
_REF_DROP = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\ud800-\udfff〔〕]")


def report_ref(at) -> tuple[str, str, str]:
    """画面の「返信」が指した報告の (名前, 報告の文, どのセッションの報告か)。記録にある報告（ハブからの自発的な報告）を、記録の時刻で探す（名前は画面から受け取らない）。
    名前は 1 行・80 字にし、制御文字・向きを変える文字・〔〕は空白にする（発言の前置きの形を崩せないように）。見つからなければ ("", "", "")。
    セッションは、0.25.0 より前の報告には無い（空。返信の守りは、空なら答える・閉じるを断る）。"""
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return "", "", ""
    for d in reversed(hermes_records() or []):
        if d.get("role") == "ai" and d.get("report") and d.get("at") == at:
            return (core._clip(" ".join(_REF_DROP.sub(" ", str(d.get("about") or "")).split()), 80), str(d.get("text") or ""),
                    str(d.get("pane") or ""))
    return "", "", ""


_FP = re.compile(r"[0-9a-f]{40}")  # 画面の指紋（server.prompt_fp の sha1）


def report_ask(at) -> dict:
    """画面の「返信」が指した報告が見ていた問い {"pane", "fp", "session"}（止まった報告で、ハブがその間に画面を見たときだけ記録にある）。
    無い・形が違う欄は空（本体は、どれかが空なら返信を答えに使わない）。"""
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return {"pane": "", "fp": "", "session": ""}
    for d in reversed(hermes_records() or []):
        if d.get("role") == "ai" and d.get("report") and d.get("at") == at:
            ask = d.get("ask") if isinstance(d.get("ask"), dict) else {}
            fp, session = ask.get("fp"), ask.get("session")
            return {"pane": str(d.get("pane") or ""), "fp": fp if isinstance(fp, str) and _FP.fullmatch(fp) else "",
                    "session": session if isinstance(session, str) and 0 < len(session) <= 200 else ""}
    return {"pane": "", "fp": "", "session": ""}


def report_topic(at) -> str:
    """画面の「返信」が指した報告の件（記録にあれば）。無い（0.29.0 より前の報告など）ときは空。"""
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return ""
    for d in reversed(hermes_records() or []):
        if d.get("role") == "ai" and d.get("report") and d.get("at") == at:
            t = d.get("topic")
            return t if isinstance(t, str) and topics.ID_RE.fullmatch(t) else ""
    return ""


def topic_report(tid) -> dict | None:
    """件の一番新しい報告（記録の時刻と名前）。画面の履歴（直近の 40 件）に出ていない古い報告も、記録の後ろ（返信の守りの report_ref と同じ範囲）から探す。
    名前は report_ref と同じ形（1 行・80 字・制御文字などを空白に）。無ければ None。"""
    if not isinstance(tid, str) or not topics.ID_RE.fullmatch(tid):
        return None
    for d in reversed(hermes_records() or []):
        at = d.get("at")
        if d.get("role") == "ai" and d.get("report") and d.get("topic") == tid and isinstance(at, (int, float)) and not isinstance(at, bool):
            return {"at": at, "about": core._clip(" ".join(_REF_DROP.sub(" ", str(d.get("about") or "")).split()), 80)}
    return None


# 画面の「↩ 返信」で報告に返した発言を Hermes が扱っている間の、その報告の件。この間にハブが送った依頼は、同じ件の続きにする
# （Hermes とのやり取りは pane_lock で一度にひとつ。Slack から頼まれた依頼は、Slack のスレッドで件を決めるので、これを使わない）
REPLY_TOPIC = {"topic": ""}


def reply_topic() -> str:
    return REPLY_TOPIC["topic"]


# ユーザーの発言と Hermes（ハブ）の返答の台帳。Hermes は herdr のセッションではないので、本体が直接持つ。
# 承認に答える・閉じるの守り（「見たあとにユーザーが発言したか」「聞いたときの返答に問いがあるか」）はこの台帳で確かめる。自動通知は載せない。
# 中身は上の記録にも書き、起動のときに記録から戻す（再起動で消えない。発言の ID は再起動をまたいでも重ならない）。
# 記録を読めなければ空から始める（守りは「まだ発言が無い」として、新しい発言を待つ側に倒れる）
HERMES_TURNS: deque = deque(maxlen=50)  # (発言の ID, ユーザーの文, Hermes の返答, 発言の直前に聞いた Hermes の返答, 返信先の報告の名前（返信でなければ空）, 返信先の報告のセッション, 返信先の報告が見ていた問い（report_ask）)
# ハブ（Hermes）の最後の出力（発言への返答か、自動通知への報告）。利用者の発言が始まるときに台帳の行へ写す（承認に答えるの守りの heard）。
# 記録（hermes-log.jsonl）からは取らない（書けなかった報告で、古い問いを拾わないため）。Hermes とのやり取りの鍵（pane_lock）の中で置き換え・写す
HERMES_HEARD = {"text": ""}
_unsaved: set[str] = set()  # 記録に書けなかった発言の ID（画面に「記録に残っていない」と知らせるため）


def hermes_turn_begin(text: str, reply_to: str = "", heard: str | None = None, reply_pane: str = "", extra: str = "", refused: bool = False,
                      reply_ask: dict | None = None, item: int | None = None) -> str:
    """発言を台帳と記録に載せる。返信のときは heard に返信先の報告の文を渡す: 承認に答える・閉じるの守りが「発言の直前に聞いた文」として見るのは、
    最後に届いた返事ではなく、利用者が返信を押した報告になる（古い報告への「はい」が、そのあとに届いた別件の問いへの答えにならない）。
    extra（本体が足す添付の行）は記録にだけ付ける（画面の履歴に残す）。守りの台帳は利用者の言葉のまま。
    refused は、本体が送らずに断った発言（添付・返信先が無い）。台帳には空の発言として載せる: 「すぐ次の発言」を進めて前の問い・確認を失効させ、
    断った発言そのものは承認にも閉じる許可にもならない（閉じるの守りは、聞いたときの返答と「次の発言」の文を見るので、聞いた文を空にするだけでは足りない）。"""
    turn = f"h{os.urandom(6).hex()}"
    HERMES_TURNS.append([turn, "" if refused else text, "", HERMES_HEARD["text"] if heard is None else heard, reply_to, reply_pane,
                         dict(reply_ask or {}) if reply_to else {}])  # 守りの台帳は、利用者の言葉のまま（返信の前置き・添付の行は入れない）
    if not hermes_log("me", text + extra, turn=turn, **({"reply_to": reply_to, "reply_pane": reply_pane} if reply_to else {}),
                      **({"reply_item": item} if item else {})):
        _unsaved.add(turn)
    return turn


def hermes_turn_end(turn: str, reply: str):
    HERMES_HEARD["text"] = reply
    for row in HERMES_TURNS:
        if row[0] == turn:
            row[2] = reply
    if reply and not hermes_log("ai", reply, turn=turn):
        _unsaved.add(turn)


def _load_hermes_turns():
    records = hermes_records()
    if records is None:
        print(f"[hermes] 記録を読めないので、守りの台帳は空から始めます: {HERMES_LOG}", flush=True)
        return
    rows: dict[str, list] = {}
    for d in records:
        turn = d.get("turn")
        if not isinstance(turn, str):
            continue
        if d["role"] == "me":
            rows[turn] = [turn, d["text"], "", None, str(d.get("reply_to") or ""), str(d.get("reply_pane") or ""), {}]  # 直前に聞いた返答の写しと、返信先が見ていた問いは記録から作らない（承認に答えるときは聞き直してもらう）
        elif d["role"] == "ai" and turn in rows:
            rows[turn][2] = d["text"]
    HERMES_TURNS.extend(rows.values())


# 判断待ちに、ことばを添えずに返したとき（「このまま送る」）に Hermes へ渡す文。利用者の言葉ではないので、台帳と記録には入れない
ITEM_EMPTY = "（ことばなし。この件の中身と、次にすることの案を短く答えてください。送る・頼む・立てるはしないでください）"


async def hermes_exchange(text: str, reply_at: float | None = None, extra: str = "", item: int | None = None) -> tuple[str, str, bool]:
    """Hermes に1往復し、発言と返事を記録する。(返事, 失敗の理由, 記録に書けたか) を返す。
    Hermes とのやり取りは同時に1件だけ（自動通知もこの順番を待つ）。
    reply_at は、画面の「返信」が指した報告の記録の時刻。記録にあれば、Hermes に送る文だけに「〔返信: <件> の報告〕」の 1 行を前に付ける
    （守りの台帳と記録の発言は利用者の言葉のまま。件の名前は記録の別の欄に残す）。
    extra は本体が足す添付の行。Hermes に送る文と記録の発言の末尾に付け、守りの台帳には入れない（「はい」に添付を付けても、守りは「はい」を見る）。
    返信なのに報告が見つからない（記録が回った・読めない・報告でない）ときは、送らずに止める（ふつうの発言として送ると、守りが最後に聞いた
    別件の問いを見てしまう）。返信先は、やり取りの鍵の中で読む（あとから届いた発言と順が入れ替わらないように）。
    item は、受け箱（💡）で押した判断待ちの記録の番号。記録にあれば「〔返信: 判断待ち [番号]「題」〕」の 1 行を前に付けて送る（text が空なら、
    中身と次にすることの案を聞く文にする）。台帳には返信として載せ、返信先の問いは空にする（承認に答える・閉じるには使えない）。"""
    async with core.pane_lock(HERMES_PANE):
        it = await asyncio.to_thread(hermes_inbox.item, item) if item is not None and reply_at is None else None
        ref, ref_text, ref_pane = await asyncio.to_thread(report_ref, reply_at) if reply_at is not None and item is None else ("", "", "")
        if (reply_at is not None or item is not None) and not (ref or it):
            # 送らずに断る。断った発言も台帳と記録に載せる（承認に答える・閉じるの「すぐ次の発言」を進め、前の確認を失効させる）。
            # 利用者が最後に聞くのは本体の断り（前に聞いた問いへの答えに、次の発言を使わせない。失敗のときと同じ）
            why = ("返信先の判断待ちが見つかりませんでした（記録が読めないか、番号が違います）。送っていません。💡 から選び直すか、返信をやめて送ってください"
                   if item is not None else
                   "返信先の報告が見つかりませんでした（記録が回ったか、読めません）。送っていません。返信を選び直すか、返信をやめて送ってください")
            turn = hermes_turn_begin(text, heard="", extra=extra, refused=True)
            HERMES_HEARD["text"] = ""
            if not hermes_log("error", why, turn=turn):
                _unsaved.add(turn)
            saved = turn not in _unsaved
            _unsaved.discard(turn)
            return "", why, saved
        if it:  # 判断待ちへの返信。返信先の問いは空（承認に答える・閉じるには使えない）。聞いた文も空（ハブの問いに答えた発言ではない）
            turn = hermes_turn_begin(text, f"判断待ち [{it['n']}]", "", "", extra, reply_ask={}, item=it["n"])
            REPLY_TOPIC["topic"] = ""
            plain = lambda v: " ".join(_REF_DROP.sub(" ", str(v or "")).split())  # 記録の文から前置きの括弧（〔〕）と制御文字を外す（報告への返信と同じ）
            head = f"〔返信: 判断待ち [{it['n']}]「{plain(it['title'])}」（{plain(it['project'])}）〕"
            sent = f"{head}\n{text}" if text.strip() else f"{head}\n{ITEM_EMPTY}"
        else:
            ask = await asyncio.to_thread(report_ask, reply_at) if ref else {}  # 返信先の報告が見ていた問い（承認に答えるの守りが、いまの問いと照らす）
            turn = hermes_turn_begin(text, ref, ref_text if ref else None, ref_pane, extra, reply_ask=ask)  # 発言の台帳。承認に答える・閉じるの守りが「見たあとの発言か」を見る
            REPLY_TOPIC["topic"] = await asyncio.to_thread(report_topic, reply_at) if ref else ""
            sent = f"〔返信: {ref} の報告〕\n{text}" if ref else text
        try:
            reply = await asyncio.to_thread(ask_hermes, sent + extra)
        except Exception as e:
            why = str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}"
            if not isinstance(e, RuntimeError):
                traceback.print_exc()
            reply = ""
            HERMES_HEARD["text"] = ""  # 利用者が聞くのは本体の失敗の知らせ（ハブの問いではない）
        else:
            hermes_turn_end(turn, reply)
            why = "" if reply else "Hermes の返事が空でした"
        finally:
            REPLY_TOPIC["topic"] = ""
        if why and not hermes_log("error", why[:300], turn=turn):
            _unsaved.add(turn)
        saved = turn not in _unsaved
        _unsaved.discard(turn)
        return reply, why, saved


def hermes_messages(text: str) -> list[dict]:
    """Hermes へ送る messages。指示文（hub.md）は発言のたびに読んで system として付ける。
    会話の始まりに一度だけ読まれる AGENTS.md と違い、書き換えが次の発言から効く（同じ会話を続けたまま）。
    system には変わらない文だけを入れる（指示文だけ）。Hermes は system を会話の前に置くので、
    中身が1字でも変わると、それまでの会話をすべてキャッシュに書き直す（件数や「何分前」を入れていたとき、発言のたびに書き直していた）。"""
    guide = (HERMES_DIR / "hub.md").read_text().strip() if (HERMES_DIR / "hub.md").is_file() else ""
    return ([{"role": "system", "content": guide}] if guide else []) + [{"role": "user", "content": text}]


def ask_hermes(text: str) -> str:
    """Hermes の API サーバー（OpenAI 互換）に送り、返事を待つ。X-Hermes-Session-Id で、その日の会話を続ける。"""
    req = urllib.request.Request(
        f"{HERMES_URL}/v1/chat/completions",
        data=json.dumps({"model": "hermes-agent", "messages": hermes_messages(text), "stream": False}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {HERMES_KEY}", "X-Hermes-Session-Id": hermes_session()},
        method="POST")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Hermes が {e.code} を返しました: {e.read().decode('utf-8', 'replace')[:200]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Hermes に接続できません（{e.reason}）。コンテナが動いているか確認してください") from None
    choice = (body.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    if isinstance(content, list):  # 形が配列のときは文字の部分だけ拾う
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    print(f"[hermes] {time.monotonic() - t0:.1f}秒（{len(text)}字 → {len(content)}字、finish={choice.get('finish_reason')}）", flush=True)
    return content.strip()


@router.post("/api/hermes/session/new")
async def api_hermes_session_new():
    """画面の「ハブの会話を新しくする」。前の会話を持ち越さずに始め直す（Hermes の記憶と手順書は残り、直した記憶はここから効く）。
    ハブ（コンテナ）からは使えない口（origin_guard）。Hermes とのやり取りの途中では切り替えず、終わるのを待つ。"""
    if not HERMES_KEY:
        return JSONResponse({"ok": False, "reason": "off"})
    async with core.pane_lock(HERMES_PANE):
        try:
            sid = await asyncio.to_thread(hermes_session_new)
        except OSError:
            traceback.print_exc()
            return JSONResponse({"ok": False, "reason": "state"})
        hermes_log("note", "ハブの会話を新しくしました（Hermes の記憶と手順書は残ります）")
    core.hlog(f"Hermes の会話を新しくしました: {sid}", flush=True)
    return JSONResponse({"ok": True, "session": sid})


# ---------- Hermes の手順の書き足し（画面の 💡 で採る・捨てる） ----------
# Hermes は手順（スキル）の書き足しを承認待ちに積む（skills.write_approval）。Hermes の承認の口は対話画面の /skills だけなので、
# 同じ処理を画面から呼べるようにする。声では採らない（書き足しを出した Hermes 自身が、中身を説明して「はい」を伝えることになるため）。
# ホストは Hermes の置き場で何も動かさない・書かない。当てる・捨てる・差分は、コンテナの中の固定の台本で行う（scripts/hermes_skills.py）。
# 状態はここ（Hermes が書けない側）に置く: id ごとの状態・知らせた id・前の処理の終わりを確かめる要否・当てた記録の控え・操作の記録
HSK_DIR = STATE_DIR / "hermes-skills"
HSK_FILE = HSK_DIR / "state.json"
HSK_LOG = STATE_DIR / "hermes-skill-log.jsonl"
HSK_KEEP = 30 * 86400
HSK_BLOCK = ("applying", "applied", "unknown", "failed")  # この id はもう採らない（同じ書き足しを二度当てない。捨てても、この印は消さない。
# failed は、自動で当てて当てられず、本体が承認待ちから外した id。同じ記録を置き直されても、試し直さない）
HSK_SHOWN: dict[tuple[str, str], bytes] = {}  # 画面に出した記録の中身（id, 指紋）。採る・捨てるは、画面の指紋がここにあるときだけ
HSK_DIFFS: dict[str, str] = {}  # 指紋 → 差分（見たときの、対象の中身に対するもの）
HSK_LOCK = asyncio.Lock()  # 採る・捨てるは一度にひとつ
_hsk_lock = threading.Lock()  # 状態のファイルを書き換えるのは一度にひとつ
HSK_NOTE = {
    "applying": "当てている途中です（台本の終わりを確かめるまで、採る・捨てるは断ります）",
    "applied": "当てました。承認待ちの記録を消せなかったので残っています（捨てるで消せます）",
    "unknown": "当たったか分かりません。手順のファイルか Hermes の対話画面で確かめてください。この書き足しはもう採れません（捨てるはできます）",
    "failed": "自動で当てようとして、当てられませんでした。この書き足しはもう採れません（捨てるはできます）",
}


def hsk_on() -> bool:
    """Hermes の置き場があるときだけ扱う（無ければ欄を出さない）。"""
    return HERMES_HOME.is_dir()


def hsk_state() -> dict | None:
    """状態のファイル。無ければ空。あるのに読めない・形が違うときは None（当てた印を失って二度当てないよう、採る・捨てるを断る）。"""
    try:
        d = json.loads(HSK_FILE.read_text())
    except FileNotFoundError:
        d = {}
    except (OSError, ValueError, RecursionError):
        traceback.print_exc()
        return None
    items, told = (d.get("items", {}), d.get("told", {})) if isinstance(d, dict) else (None, None)
    if not isinstance(items, dict) or not isinstance(told, dict):
        return None
    ok = lambda v: isinstance(v, dict) and isinstance(v.get("state"), str) and isinstance(v.get("at"), (int, float))
    return {"items": {k: v for k, v in items.items() if ok(v)}, "told": {k: v for k, v in told.items() if isinstance(v, (int, float))},
            "verify": d.get("verify") is True}


def hsk_log(**rec):
    """操作の記録（時刻・id・要約・結果・理由。記録の中身は書かない）。人が手入れする台帳ではない。書けなくても止めない。"""
    try:
        HSK_LOG.parent.mkdir(parents=True, exist_ok=True)
        with HSK_LOG.open("a") as f:
            f.write(json.dumps({"at": time.time(), **rec}, ensure_ascii=False) + "\n")
    except OSError:
        traceback.print_exc()


def _hsk_archive(pid: str, data: dict | None):
    """当てた（当てようとした）記録の控え（hermes-skills/approved/<id>.json、30 日）。None なら消す。書けなければ OSError。"""
    path = HSK_DIR / "approved" / f"{pid}.json"
    if data is None:
        path.unlink(missing_ok=True)
    else:
        core.write_json(path, data)


def _recover_hermes_skills():
    """起動のとき、当てている途中で本体が止まった id は「分からない」にし、前の処理の終わりを確かめるまで採る・捨てるを断る。
    学んだことの跡のほうも、当てている途中のものを「分からない」にする（_recover_hlearn）。"""
    _recover_hlearn()
    with _hsk_lock:
        d = hsk_state()
        stuck = [k for k, v in d["items"].items() if v["state"] == "applying"] if d else []
        if not stuck:
            return
        for k in stuck:
            d["items"][k].update(state="unknown", at=time.time(), note="当てている途中で本体が止まりました")
        d["verify"] = True
        try:
            core.write_json(HSK_FILE, d)
        except OSError:
            traceback.print_exc()
    for k in stuck:
        hsk_log(id=k, act="approve", result="unknown", why="当てている途中で本体が止まりました")


def _hsk_problem() -> str:
    """コンテナの台本が使えるか（コンテナが動いているか・使う Hermes の関数が揃っているか）。結果は少しのあいだ覚えておく。"""
    def check():
        out = hermes_skills.run_script(HERMES_CONTAINER, "check", "", b"", wait=30)
        return "" if out.get("result") == "ok" else f"Hermes のコンテナ（{HERMES_CONTAINER}）で台本を動かせません: {out.get('why') or out.get('result')}"
    return core._probe("hermes-skills", check, keep_ok=300, keep_ng=30)


def _hsk_view() -> dict:
    """画面に出す承認待ち。出した中身は HSK_SHOWN に控える（採るのときに、画面の指紋と照らす）。"""
    if not hsk_on():
        return {"on": False}
    try:
        got = hermes_skills.list_pending(HERMES_HOME)
    except OSError as e:
        return {"on": True, "problem": f"承認待ちの置き場（{HERMES_HOME}/pending/skills）を開けません（{type(e).__name__}。symlink はたどりません）",
                "items": [], "unreadable": 0, "verify": False}
    for r in got["records"]:
        HSK_SHOWN.pop((r["id"], r["v"]), None)
        HSK_SHOWN[(r["id"], r["v"])] = r["raw"]
    while len(HSK_SHOWN) > 300:
        HSK_SHOWN.pop(next(iter(HSK_SHOWN)))
    with _hsk_lock:
        d = hsk_state()
    problem = _hsk_problem() if got["records"] else ""
    if d is None:
        problem = problem or f"状態のファイル（{HSK_FILE}）を読めません。採る・捨てるは断ります"
    items = []
    for r in got["records"]:
        st = d["items"].get(r["id"], {}) if d else {}
        state = st.get("state") if st.get("state") in HSK_BLOCK else ""
        note = HSK_NOTE.get(state, "") + (f"（{st['note']}）" if state and st.get("note") else "")
        items.append({**{k: r[k] for k in ("id", "v", "at", "origin", "summary")}, "unsupported": r["why"], "state": state, "note": note,
                      "can_apply": not (r["why"] or state or problem)})
    # verify: 前の処理の終わりを確かめるまで断っている（採る・捨てるの最中は、印があっても処理中なので出さない）
    return {"on": True, "problem": problem, "items": items, "unreadable": got["unreadable"], "verify": bool(d and d["verify"]) and not HSK_LOCK.locked()}


def _hsk_args(body: dict) -> tuple[str, str] | None:
    pid, v = body.get("id"), body.get("v")
    if not isinstance(pid, str) or not isinstance(v, str) or not hermes_skills.ID_RE.fullmatch(pid):
        return None
    return pid, v


def _hsk_settled() -> str:
    """前の処理が終わったと確かめられているか。確かめられたら ""。採る・捨てるの鍵の中で呼ぶ（自分の処理は動いていない）。
    「終わり未確認」の印（台本を動かす前に書き、戻ってきたら外す）か、「当てている途中」の id が残っていれば、
    コンテナの中に台本がまだ動いていないかを確かめる。時間だけでは解かない。いないと確かめたら、当てている途中の id は分からないにする。"""
    with _hsk_lock:
        d = hsk_state()
    if d is None:
        return f"状態のファイル（{HSK_FILE}）を読めません"
    if not d["verify"] and not any(v["state"] == "applying" for v in d["items"].values()):
        return ""
    running = hermes_skills.still_running(HERMES_CONTAINER)
    if running is not False:
        return "前の処理がコンテナの中でまだ動いています" if running else "前の処理が終わったか確かめられません（Hermes のコンテナが答えません）"
    with _hsk_lock:
        d = hsk_state()
        if d is None:
            return f"状態のファイル（{HSK_FILE}）を読めません"
        stuck = [k for k, v in d["items"].items() if v["state"] == "applying"]
        for k in stuck:
            d["items"][k].update(state="unknown", at=time.time(), note="当てている途中で止まり、結果を記録できませんでした")
        d["verify"] = False
        try:
            core.write_json(HSK_FILE, d)
        except OSError:
            traceback.print_exc()
            return "状態を書けません"
    for k in stuck:
        hsk_log(id=k, act="approve", result="unknown", why="当てている途中で止まり、結果を記録できませんでした")
    core.hlog("Hermes の書き足し: 前の処理が終わったのを確かめました", flush=True)
    return ""


def _hsk_mark_running() -> bool:
    """台本を動かす前に「終わり未確認」の印を書く（戻ってきたら外す。外せなければ、次の要求で終わりを確かめる）。書けたら True。"""
    with _hsk_lock:
        d = hsk_state()
        if d is None:
            return False
        d["verify"] = True
        try:
            core.write_json(HSK_FILE, d)
        except OSError:
            traceback.print_exc()
            return False
    return True


def _hsk_begin(pid: str, v: str, rec: dict, raw: bytes) -> str:
    """当て始める: 二度当てない印を確かめ、「当てている途中」と控えを書く。書けなければ当てない。断る理由を返す（"" なら始めてよい）。"""
    with _hsk_lock:
        d = hsk_state()
        if d is None:
            return "state"
        st = d["items"].get(pid)
        if st and st["state"] in HSK_BLOCK:
            return st["state"]
        try:
            _hsk_archive(pid, {"id": pid, "v": v, "summary": rec["summary"], "at": time.time(), "result": "applying",
                               "record": raw.decode("utf-8", "replace")})
            d["items"][pid] = {"state": "applying", "at": time.time(), "v": v, "summary": rec["summary"][:120], "note": ""}
            d["verify"] = True  # 台本が戻ってきたら外す（_hsk_finish）
            core.write_json(HSK_FILE, d)
        except OSError:
            traceback.print_exc()
            with suppress(OSError):
                _hsk_archive(pid, None)
            return "state"
    return ""


def _hsk_finish(pid: str, v: str, rec: dict, raw: bytes, out: dict, dropped: bool = False) -> dict:
    """台本の結果で状態を決める。changed・unsupported は当てていない（承認待ちに戻す）。applied は当てた。
    failed・例外・形の違い・時間切れは、当たったか分からない（その id はもう採らない）。
    dropped は、自動で当てて failed になり、手順が当てる前のままと本体が確かめて、承認待ちの記録を捨て終えたとき（_learn_skills だけが渡す。
    台本の答えからは決めない）。当てていないと分かっているが、同じ記録を置き直されても試し直さないよう、二度当てない印（failed）を残す。当てようとした記録は控えに残す。"""
    res, why = out.get("result"), str(out.get("why") or "")
    dropped = dropped and res == "failed"
    state = "failed" if dropped else None if res in ("changed", "unsupported") else "applied" if res == "applied" else "unknown"
    with _hsk_lock:
        d = hsk_state()
        if d is not None:
            if state is None:
                d["items"].pop(pid, None)
            else:
                d["items"][pid] = {"state": state, "at": time.time(), "v": v, "summary": rec["summary"][:120],
                                   "note": "" if state == "applied" else why[:200], "discarded": dropped or out.get("discarded") is True}
                if dropped:
                    d["items"][pid]["dropped"] = time.time()
            d["verify"] = res == "timeout"  # 時間切れなら、台本がまだ動いているかもしれない
            try:
                core.write_json(HSK_FILE, d)
            except OSError:  # 「当てている途中」と印が残る。次の要求で終わりを確かめ、分からないにする
                traceback.print_exc()
    try:
        _hsk_archive(pid, None if state is None else {"id": pid, "v": v, "summary": rec["summary"], "at": time.time(), "result": res,
                                                     "why": why, "record": raw.decode("utf-8", "replace")})
    except OSError:
        traceback.print_exc()
    if res in ("changed", "unsupported") or dropped:
        HSK_SHOWN.pop((pid, v), None)
    hsk_log(id=pid, act="approve", summary=rec["summary"][:120], result=res, why=why[:200])
    core.hlog(f"Hermes の書き足し {pid} を採る: {res}" + (f"（{why[:80]}）" if why else ""), flush=True)
    if res == "applied":
        return {"ok": True, "discarded": out.get("discarded") is True}
    if dropped:
        hsk_log(id=pid, act="reject", summary=rec["summary"][:120], result="rejected", why="当てられず、手順は変わっていないので、承認待ちから外しました")
        core.hlog(f"Hermes の書き足し {pid} は当てられず、手順は変わっていないので、承認待ちから外しました", flush=True)
        return {"ok": False, "reason": "failed", "why": why[:200]}
    return {"ok": False, "reason": res if res in ("changed", "unsupported") else "unknown", "why": why[:200]}


@router.get("/api/hermes/skills")
async def api_hermes_skills():
    """画面の 💡 の「ハブが手順に書き足したいこと」。ハブ（コンテナ）からは使えない口。"""
    return JSONResponse(await asyncio.to_thread(_hsk_view))


@router.post("/api/hermes/skills/diff")
async def api_hermes_skill_diff(body: dict):
    """差分を見る。Hermes 自身の差分の作り方を、画面に出した記録の中身でコンテナの中で呼ぶ（見たときの、対象の中身に対するもの）。"""
    args = _hsk_args(body)
    if not args:
        return JSONResponse({"ok": False, "reason": "bad_request"}, status_code=400)
    raw = HSK_SHOWN.get(args)
    if not hsk_on() or raw is None:
        return JSONResponse({"ok": False, "reason": "stale" if hsk_on() else "off"})
    if args[1] not in HSK_DIFFS:
        out = await asyncio.to_thread(hermes_skills.run_script, HERMES_CONTAINER, "diff", args[0], raw, 40)
        if out.get("result") != "diff" or not isinstance(out.get("diff"), str):
            return JSONResponse({"ok": False, "reason": "broken", "why": str(out.get("why") or out.get("result"))[:200]})
        HSK_DIFFS[args[1]] = out["diff"]
        while len(HSK_DIFFS) > 100:
            HSK_DIFFS.pop(next(iter(HSK_DIFFS)))
    return JSONResponse({"ok": True, "diff": HSK_DIFFS[args[1]]})


@router.post("/api/hermes/skills/approve")
async def api_hermes_skill_approve(body: dict):
    """画面の「採る」（2 段の押しの 2 段目）。画面が見せた版（id と指紋 v）の中身を本体が控えていて、扱える形で、
    まだ当てていない（当てている途中・結果が分からない、でもない）ときだけ、コンテナの中で当てる。ハブ（コンテナ）からは使えない口。"""
    args = _hsk_args(body)
    if not args:
        return JSONResponse({"ok": False, "reason": "bad_request"}, status_code=400)
    if not hsk_on():
        return JSONResponse({"ok": False, "reason": "off"})
    pid, v = args
    async with HSK_LOCK:
        raw = HSK_SHOWN.get(args)
        if raw is None:
            return JSONResponse({"ok": False, "reason": "stale"})
        why = await asyncio.to_thread(_hsk_settled)
        if why:
            return JSONResponse({"ok": False, "reason": "busy", "why": why})
        rec = hermes_skills.parse_record(raw, pid)
        if rec["why"]:
            return JSONResponse({"ok": False, "reason": "unsupported", "why": rec["why"]})
        core._probes.pop("hermes-skills", None)  # 当てる直前に、コンテナと関数が揃っているかを確かめ直す（動かせないものを「分からない」にしない）
        problem = await asyncio.to_thread(_hsk_problem)
        if problem:
            return JSONResponse({"ok": False, "reason": "broken", "why": problem})
        began = await asyncio.to_thread(_hsk_begin, pid, v, rec, raw)
        if began:
            return JSONResponse({"ok": False, "reason": began})
        try:
            out = await asyncio.to_thread(hermes_skills.run_script, HERMES_CONTAINER, "approve", pid, raw)
        except Exception as e:
            traceback.print_exc()
            out = {"result": "error", "why": f"本体の問題（{type(e).__name__}）"}
        return JSONResponse(await asyncio.to_thread(_hsk_finish, pid, v, rec, raw, out))


@router.post("/api/hermes/skills/reject")
async def api_hermes_skill_reject(body: dict):
    """画面の「捨てる」。承認待ちの記録を消すだけで、二度当てない印（当てた・分からない）は消さない。ハブ（コンテナ）からは使えない口。"""
    args = _hsk_args(body)
    if not args:
        return JSONResponse({"ok": False, "reason": "bad_request"}, status_code=400)
    if not hsk_on():
        return JSONResponse({"ok": False, "reason": "off"})
    pid, v = args
    async with HSK_LOCK:
        raw = HSK_SHOWN.get(args)
        if raw is None:
            return JSONResponse({"ok": False, "reason": "stale"})
        why = await asyncio.to_thread(_hsk_settled)
        if why:
            return JSONResponse({"ok": False, "reason": "busy", "why": why})
        if not await asyncio.to_thread(_hsk_mark_running):
            return JSONResponse({"ok": False, "reason": "state"})
        out = await asyncio.to_thread(hermes_skills.run_script, HERMES_CONTAINER, "reject", pid, raw)
        res, why = out.get("result"), str(out.get("why") or "")
        summary = hermes_skills.parse_record(raw, pid)["summary"][:120]
        with _hsk_lock:
            d = hsk_state()
            if d is not None:
                st = d["items"].get(pid)
                d["verify"] = res == "timeout"  # 時間切れなら、台本がまだ動いているかもしれない
                if res == "rejected" and st and st["state"] in HSK_BLOCK:
                    st["dropped"] = time.time()
                elif res == "rejected":
                    d["items"][pid] = {"state": "rejected", "at": time.time(), "v": v, "summary": summary, "note": ""}
                try:
                    core.write_json(HSK_FILE, d)
                except OSError:
                    traceback.print_exc()
        if res in ("rejected", "changed"):
            HSK_SHOWN.pop(args, None)
        hsk_log(id=pid, act="reject", summary=summary, result=res, why=why[:200])
        if res == "rejected":
            return JSONResponse({"ok": True, "discarded": out.get("discarded") is True})
        return JSONResponse({"ok": False, "reason": res if res in ("changed", "timeout") else "broken", "why": why[:200]})


# ---------- Hermes の記憶（PC の画面の「記憶」で足す・直す・消す） ----------
# 読むのはホストの側の安全な読み（hermes_memory.read_targets）。書くのはコンテナの中の固定の台本で、Hermes 自身の apply_memory_pending。
# 画面だけの口（コンテナに開く口の一覧 DOCKER_ALLOWED に無い）。直す・消すは、画面に出した項目（版 → target と本文）を引き直して当てる。
# 当たったか分からない（error・failed・timeout）ときは「終わり未確認」の印を残し、前の台本がもう動いていないと確かめるまで次を断る（PR #19 と同じ）
HMEM_FILE = STATE_DIR / "hermes-memory" / "state.json"
HMEM_LOCK = asyncio.Lock()  # 足す・直す・消すは一度にひとつ
HMEM_SHOWN: OrderedDict[str, tuple[str, str]] = OrderedDict()  # 版 → (target, 本文)。画面に出した項目
HMEM_SHOWN_MAX = 500
HMEM_LIMITS = {"at": 0.0, "value": None, "why": ""}  # 上限と有効（画面の表示だけに使う。書く判定は Hermes の Store がその場で行う）
HMEM_DONE = ("applied", "changed", "refused", "invalid")  # 決着した結果（書いていない・書いた、が分かる）


def hmem_state() -> dict | None:
    """状態のファイル。無ければ空。あるのに読めない・形が違うときは None（書くのを断る）。"""
    try:
        d = json.loads(HMEM_FILE.read_text())
    except FileNotFoundError:
        return {"verify": False}
    except (OSError, ValueError, RecursionError):
        traceback.print_exc()
        return None
    return {"verify": d.get("verify") is True} if isinstance(d, dict) else None


def _hmem_limits() -> dict | None:
    """上限と有効（5 分覚える）。読めなければ None（理由は HMEM_LIMITS["why"]）。"""
    if time.time() - HMEM_LIMITS["at"] < (300 if HMEM_LIMITS["value"] else 30):
        return HMEM_LIMITS["value"]
    out = hermes_memory.run_script(HERMES_CONTAINER, "limits", wait=60)
    ok = out.get("result") == "limits"
    HMEM_LIMITS.update(at=time.time(), value={k: v for k, v in out.items() if k != "result"} if ok else None, why="" if ok else out.get("why", out.get("result", "")))
    return HMEM_LIMITS["value"]


def _hmem_view() -> dict:
    targets = hermes_memory.read_targets(HERMES_HOME)
    limits = _hmem_limits()
    st = hmem_state()
    out = {}
    for t, d in targets.items():
        rows = []
        for e in d["entries"]:
            v = hermes_memory.version(t, e)
            HMEM_SHOWN[v] = (t, e)
            HMEM_SHOWN.move_to_end(v)
            rows.append({"v": v, "text": e})
        lim = limits and limits.get("user_char_limit" if t == "user" else "memory_char_limit")
        on = limits and limits.get("user_profile_enabled" if t == "user" else "memory_enabled")
        out[t] = {"status": d["status"], "entries": rows, "chars": len(hermes_memory.DELIM.join(d["entries"])), "limit": lim, "enabled": on}
    while len(HMEM_SHOWN) > HMEM_SHOWN_MAX:
        HMEM_SHOWN.popitem(last=False)
    return {"on": True, "targets": out, "verify": bool(st and st["verify"]), "state_ok": st is not None,
            "problem": "" if limits else (HMEM_LIMITS["why"] or "上限を読めません")}


@router.get("/api/hermes/memory")
async def api_hermes_memory():
    """PC の画面の「記憶」。Hermes の記憶の項目（ユーザーについて・メモ）と、字数・上限・有効。"""
    if not hsk_on():
        return JSONResponse({"on": False})
    return JSONResponse(await asyncio.to_thread(_hmem_view))


def _hmem_mark(verify: bool) -> bool:
    try:
        core.write_json(HMEM_FILE, {"verify": verify, "at": time.time()})
        return True
    except OSError:
        traceback.print_exc()
        return False


async def _hmem_run(op: str, req: dict) -> dict:
    """記憶の台本を 1 回動かす（一度にひとつ。前の処理の終わりを確かめてから）。動かせなければ result が state・busy・unverifiable。"""
    async with HMEM_LOCK:
        st = await asyncio.to_thread(hmem_state)
        if st is None:
            return {"result": "state"}
        if st["verify"]:
            running = await asyncio.to_thread(hermes_memory.still_running, HERMES_CONTAINER)
            if running is True:
                return {"result": "busy"}
            if running is None:
                return {"result": "unverifiable"}
            if not await asyncio.to_thread(_hmem_mark, False):
                return {"result": "state"}
        if not await asyncio.to_thread(_hmem_mark, True):  # 台本の前に「終わり未確認」を書く（書けなければ動かさない）
            return {"result": "state"}
        out = await asyncio.to_thread(hermes_memory.run_script, HERMES_CONTAINER, op, req)
        if out.get("result") in HMEM_DONE:
            await asyncio.to_thread(_hmem_mark, False)
    core.hlog(f"Hermes の記憶: {op} {req.get('target')} → {out.get('result')}" + (f"（{core._clip(str(out.get('why', '')), 80)}）" if out.get("why") else ""), flush=True)
    return out


async def _hmem_write(op: str, body: dict) -> JSONResponse:
    if not hsk_on():
        return JSONResponse({"ok": False, "reason": "off"})
    target = body.get("target")
    if target not in hermes_memory.TARGETS:
        return JSONResponse({"ok": False, "reason": "bad_request"}, status_code=400)
    req = {"target": target}
    if op in ("add", "edit"):
        why = hermes_memory.text_problem(body.get("content"))
        if why:
            return JSONResponse({"ok": False, "reason": "invalid", "why": why})
        req["content"] = body["content"]
    if op in ("edit", "remove"):
        shown = HMEM_SHOWN.get(str(body.get("v", "")))
        if not shown or shown[0] != target:
            return JSONResponse({"ok": False, "reason": "stale"})
        req["entry"] = shown[1]
    # 書いてから比べるもとを進めるまでを、見張りの読み取り（_learn_memory）と同じ鍵の中で行う
    # （書いたあとの記憶を見張りが先に読むと、画面で書いたものが Hermes の学びとして残り、「戻す」で本人の書き込みを消せてしまう）
    async with HMEM_LEARN_LOCK:
        # 書く前に、見張りと同じ読み取りで、ここまでの Hermes の変化を跡に残す（Hermes が足した項目を、見張りが読む前に画面で中身を変えずに保存したなど、
        # 画面で書く項目と同じ中身の Hermes の変化を、外す項目に紛れさせて跡から落とさない）。読めない・跡を書けないときは書かない
        why = await _learn_memory(target)
        if why:
            return JSONResponse({"ok": False, "reason": why})
        # 画面で書いて当たった項目だけを、Hermes の学びとして跡に残さない（当たらなかった・分からないものは外さない。ほかの変化は跡に残す）。
        # 読む側（記憶のファイルの読み取り）と同じく、改行を LF にそろえて前後の空白を外したもので照らす
        mine = {"added": [_hmem_norm(req["content"])] if op in ("add", "edit") else [], "removed": [req["entry"]] if op in ("edit", "remove") else []}
        out = await _hmem_run_mine(op, req, target, mine)
        res = out.get("result")
        if res in ("state", "busy", "unverifiable"):
            return JSONResponse({"ok": False, "reason": res})
        await _learn_memory()
    if res == "applied":
        return JSONResponse({"ok": True, "shape": out.get("shape", "")})
    if res in HMEM_DONE:
        return JSONResponse({"ok": False, "reason": res, "why": out.get("why", "")})
    return JSONResponse({"ok": False, "reason": "unknown", "why": out.get("why", "")})


@router.post("/api/hermes/memory/add")
async def api_hermes_memory_add(body: dict):
    """画面の「足す」。target（user・memory）に content を 1 項目として足す。"""
    return await _hmem_write("add", body)


@router.post("/api/hermes/memory/edit")
async def api_hermes_memory_edit(body: dict):
    """画面の「直す」。画面に出した版 v の項目を、content に置き換える（項目全体）。"""
    return await _hmem_write("edit", body)


@router.post("/api/hermes/memory/remove")
async def api_hermes_memory_remove(body: dict):
    """画面の「消す」（2 段）。画面に出した版 v の項目を消す。"""
    return await _hmem_write("remove", body)


def _hsk_tidy(ids: set[str]) -> list[str]:
    """まだ知らせていない承認待ちの id を、知らせた印にして返す。古い状態と印（30 日。承認待ちに残っている id のものは残す）と控えを片づける。"""
    now = time.time()
    with _hsk_lock:
        d = hsk_state()
        if d is None:
            return []
        new = sorted(ids - set(d["told"]))
        told = {k: t for k, t in d["told"].items() if now - t < HSK_KEEP or k in ids} | {k: now for k in new}
        items = {k: v for k, v in d["items"].items() if now - v["at"] < HSK_KEEP or k in ids or v["state"] == "applying"}
        if told != d["told"] or items != d["items"]:
            core.write_json(HSK_FILE, {**d, "told": told, "items": items})
    for p in (HSK_DIR / "approved").glob("*.json"):
        with suppress(OSError):
            if now - p.stat().st_mtime > HSK_KEEP:
                p.unlink()
    return new


# ---------- 学んだこと（Hermes の手順の書き足しを自動で当て、記憶の変化とあわせて跡を残し、画面の「戻す」で戻す） ----------
# 2026-11-16 本人が推奨を選んだ（学びは承認待ちを作らずに自動で当てる・見たいときに見る画面と「戻す」だけ）。部品は scripts/hermes_learn.py。
# 跡と控え（当てる前の手順の中身・当てた直後の指紋）は、Hermes が書けない側（状態フォルダの hermes-learned/）に置く
HLEARN_DIR = STATE_DIR / "hermes-learned"
HLEARN_FILE = HLEARN_DIR / "state.json"
HLEARN_UNDO_KEEP = 90 * 86400  # 戻すための控え（当てる前の手順の中身）を持つ期間。跡そのもの（いつ・何を・差分）は消さない（変わった跡は全部残す）
HLEARN_LOCK = asyncio.Lock()  # 当てる・戻すは一度にひとつ
HMEM_LEARN_LOCK = asyncio.Lock()  # 記憶を本体が書いてから比べるもとを進めるまでと、見張りの読み取りは一度にひとつ（HLEARN_LOCK の中で取ることはあっても、逆はしない）
_hlearn_lock = threading.Lock()  # 状態のファイルを書き換えるのは一度にひとつ
_HLEARN_SKIP: set[tuple[str, str]] = set()  # 中身を読めなかった書き足し（id, 指紋）。この版は、本体を起動し直すまで自動では試し直さない


def hlearn_state() -> dict | None:
    """跡の状態。無ければ空。あるのに読めない・形が違うときは None（当てない・戻さない）。"""
    try:
        d = json.loads(HLEARN_FILE.read_text())
    except FileNotFoundError:
        d = {"entries": [], "memory": None}
    except (OSError, ValueError, RecursionError):
        traceback.print_exc()
        return None
    if not isinstance(d, dict) or not isinstance(d.get("entries"), list) or not (d.get("memory") is None or isinstance(d.get("memory"), dict)):
        return None
    d.setdefault("days", {})
    if not isinstance(d["days"], dict) or not all(isinstance(v, int) for v in d["days"].values()):
        return None
    return d


def _hlearn_save(d: dict):
    """跡を書く。跡は消さない。90 日を過ぎた手順の跡は、戻すための控え（当てる前の中身・指紋）だけを控えのファイルから外し、戻せない印にする
    （差分は残す）。日ごとの数（days）は 31 日分。"""
    now = time.time()
    d["days"] = {k: v for k, v in d.get("days", {}).items() if k >= _hlearn_day(now - 31 * 86400)}
    for e in d["entries"]:
        # 戻せるかに関わらず（時間切れ・当てたあとを読めなかったものも、当てる前の中身は控えてある）、90 日を過ぎたら控えを外す
        if e.get("kind") == "skill" and not e.get("pruned") and now - e.get("at", 0) >= HLEARN_UNDO_KEEP:
            path = HLEARN_DIR / f"{e['id']}.json"
            try:
                det = json.loads(path.read_text())
                if isinstance(det, dict) and ("before" in det or "expect" in det):
                    det.pop("before", None)
                    det.pop("expect", None)
                    core.write_json(path, det)
            except FileNotFoundError:
                pass
            except (OSError, ValueError, RecursionError):
                continue  # 外せなかったら、次に書くときにもう一度
            if e.get("can_undo"):
                e.update(can_undo=False, note="90 日を過ぎたので、戻すための控えを片づけました（跡と差分は残ります）")
            e["pruned"] = True
    core.write_json(HLEARN_FILE, d)


def _hlearn_day(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(t + 9 * 3600))  # 日本時間の日付


def _hlearn_new(entry: dict, detail: dict) -> dict:
    """跡の 1 つを作り、控え（別のファイル、0600）を書く。状態にはまだ足さない。書けなければ OSError。"""
    entry = {"id": f"l{os.urandom(6).hex()}", "at": time.time(), "state": "applied", "note": "", **entry}
    core.write_json(HLEARN_DIR / f"{entry['id']}.json", detail)
    return entry


def _hlearn_add(entry: dict, detail: dict) -> dict:
    """跡を 1 つ足す（控えは別のファイル、0600）。書けなければ OSError。"""
    entry = _hlearn_new(entry, detail)
    with _hlearn_lock:
        d = hlearn_state()
        if d is None:
            raise OSError("跡の状態を読めません")
        d["entries"].append(entry)
        _hlearn_save(d)
    return entry


def _hlearn_set(eid: str, **kw):
    with _hlearn_lock:
        d = hlearn_state()
        if d is None:
            return
        for e in d["entries"]:
            if e["id"] == eid:
                e.update(kw)
        try:
            _hlearn_save(d)
        except OSError:
            traceback.print_exc()


def _hlearn_today() -> int:
    """今日（日本時間）自動で当てようとした手順の書き足しの数（当てた・失敗・分からない・戻した、を全部数える。跡とは別の欄 days）。"""
    with _hlearn_lock:
        d = hlearn_state()
    if d is None:
        return hermes_learn.LIMIT  # 読めなければ当てない
    return d["days"].get(_hlearn_day(time.time()), 0)


def _recover_hlearn():
    """起動のとき、学んだことの跡で「当てている途中」のまま残ったもの（当てる前に跡を書いたあと、結果を書く前に本体が止まった）を、
    「分からない」・戻せないにする（いつまでも「当てている途中」と出さない）。"""
    with _hlearn_lock:
        d = hlearn_state()
        stuck = [e for e in (d or {}).get("entries", []) if e.get("state") == "applying"]
        if not stuck:
            return
        for e in stuck:
            e.update(state="unknown", can_undo=False, note="当てている途中で本体が止まりました。当たったか分かりません（手順のファイルか Hermes の対話画面で確かめてください）")
        try:
            _hlearn_save(d)
        except OSError:
            traceback.print_exc()


def _hlearn_reserve(entry: dict, detail: dict) -> dict | None:
    """当てる前に、今日の枠を 1 つ使い、跡（当てている途中）と控え（前の中身・差分）を書く。上限に届いている・書けないときは None（当てない）。"""
    eid = f"l{os.urandom(6).hex()}"
    with _hlearn_lock:
        d = hlearn_state()
        if d is None:
            return None
        day = _hlearn_day(time.time())
        if d["days"].get(day, 0) >= hermes_learn.LIMIT:
            return None
        entry = {"id": eid, "at": time.time(), "state": "applying", "note": "", **entry}
        try:
            core.write_json(HLEARN_DIR / f"{eid}.json", detail)
            d["days"][day] = d["days"].get(day, 0) + 1
            d["entries"].append(entry)
            _hlearn_save(d)
        except OSError:
            traceback.print_exc()
            with suppress(OSError):
                (HLEARN_DIR / f"{eid}.json").unlink()
            return None
    return entry


def _hlearn_finish(eid: str, expect: dict | None, **kw):
    """当てた結果を跡に書く。expect（当てた直後の指紋）があれば控えに足す（戻せる）。書けなければ戻せない印のまま。"""
    if expect is not None:
        try:
            det = json.loads((HLEARN_DIR / f"{eid}.json").read_text())
            det["expect"] = expect
            core.write_json(HLEARN_DIR / f"{eid}.json", det)
        except (OSError, ValueError):
            traceback.print_exc()
            kw.update(can_undo=False, note="控えに当てた直後の中身を書けなかったので、戻せません")
    _hlearn_set(eid, **kw)


async def _learn_skills() -> int:
    """承認待ちの手順の書き足しを、今日の上限まで自動で当てる。当てた数を返す。扱えない形・当てた／分からない id・読めない手順は当てない（承認待ちに残す）。"""
    if not hsk_on():
        return 0
    applied = 0
    async with HSK_LOCK:  # 画面の「採る」「捨てる」と同時に動かさない
        if await asyncio.to_thread(_hsk_settled):
            return 0
        try:
            got = await asyncio.to_thread(hermes_skills.list_pending, HERMES_HOME)
        except OSError:
            return 0
        with _hsk_lock:
            st = hsk_state()
        if st is None:
            return 0
        todo = [r for r in got["records"] if not r["why"] and st["items"].get(r["id"], {}).get("state") not in HSK_BLOCK
                and (r["id"], r["v"]) not in _HLEARN_SKIP]
        if not todo:
            return 0
        left = hermes_learn.LIMIT - await asyncio.to_thread(_hlearn_today)
        if left <= 0:
            return 0
        core._probes.pop("hermes-skills", None)
        if await asyncio.to_thread(_hsk_problem):
            return 0
        for r in todo[:left]:
            if await asyncio.to_thread(_hsk_settled):  # 前の書き足しの台本の終わりを確かめられなければ、この回はやめる
                break
            pid, v, raw = r["id"], r["v"], r["raw"]
            rec = hermes_skills.parse_record(raw, pid)
            payload = json.loads(raw.decode("utf-8-sig"))["payload"]
            before = await asyncio.to_thread(hermes_learn.run_script, HERMES_CONTAINER, "snap", {"payload": payload})
            if before.get("result") != "snap" or not isinstance(before.get("skills"), dict):
                hsk_log(id=pid, act="auto", summary=rec["summary"][:120], result="skipped", why=str(before.get("why") or before.get("result"))[:200])
                if before.get("result") == "unsupported":  # 扱えない形（読めない手順・別名・鍵の無い版など）: この版の書き足しは、起動し直すまで試し直さない
                    _HLEARN_SKIP.add((pid, v))
                    continue
                break  # 時間切れ・コンテナの問題など一時的なもの: この回はやめて、次の回に試し直す
            diff = await asyncio.to_thread(hermes_skills.run_script, HERMES_CONTAINER, "diff", pid, raw, 40)
            entry = await asyncio.to_thread(_hlearn_reserve, {
                "kind": "skill", "auto": True, "pid": pid, "summary": rec["summary"][:300], "origin": r["origin"],
                "places": sorted(before["skills"]), "can_undo": False},
                {"diff": str(diff.get("diff") or "")[:hermes_skills.DIFF_MAX] if diff.get("result") == "diff" else "", "before": before["skills"], "expect": {}})
            if entry is None:  # 上限に届いた・控えを書けない: 当てない
                break
            if await asyncio.to_thread(_hsk_begin, pid, v, rec, raw):
                await asyncio.to_thread(_hlearn_set, entry["id"], state="skipped", note="当てる前に止めました（ほかの処理と重なりました）")
                continue
            # 当てるのは、控えを取ったときの指紋と同じときだけ。Hermes の手順の鍵を持って確かめ・当て・当てたあとの指紋まで一続きに行う
            # （控えのあとに Hermes が書いた中身を、当てた直後の中身に混ぜない。混ざると「戻す」がその書き込みまで消す）
            expect_before = json.dumps({k: hermes_learn.digest(x) for k, x in before["skills"].items()})
            try:
                out = await asyncio.to_thread(hermes_skills.run_script, HERMES_CONTAINER, "approve", pid, raw,
                                              env={"VL_LEARN_EXPECT": expect_before}, script=hermes_learn.APPLY_SCRIPT)
            except Exception as e:
                traceback.print_exc()
                out = {"result": "error", "why": f"本体の問題（{type(e).__name__}）"}
            dropped = False
            if out.get("result") == "failed" and await asyncio.to_thread(_hlearn_unchanged, payload, before["skills"]):
                # Hermes がはっきり失敗と返し、手順も当てる前のまま（直す行がもう無い、など）: 当たっていないと分かるので、承認待ちから外す。
                # 外せなければ、これまでどおり「分からない」に残す（時間切れなら、終わりを確かめるまで次を当てない）
                drop = await asyncio.to_thread(hermes_skills.run_script, HERMES_CONTAINER, "reject", pid, raw)
                dropped = drop.get("result") == "rejected" and drop.get("discarded") is True  # 消したと答えたときだけ
                if drop.get("result") == "timeout":
                    out = {"result": "timeout", "why": f"当てられなかった書き足しを、承認待ちから外せませんでした（{core._clip(str(out.get('why') or ''), 120)}）"}
            res = await asyncio.to_thread(_hsk_finish, pid, v, rec, raw, out, dropped)
            if not res.get("ok"):
                why = core._clip(str(out.get("why") or out.get("result")), 120)
                if dropped:
                    await asyncio.to_thread(_hlearn_set, entry["id"], state="failed", note=f"当てられませんでした（{why}）。手順は変わっていないので、承認待ちから外しました")
                    continue
                if out.get("result") in ("changed", "unsupported"):
                    await asyncio.to_thread(_hlearn_set, entry["id"], state="failed", note=f"当てられませんでした（{why}）")
                else:
                    await asyncio.to_thread(_hlearn_set, entry["id"], state="unknown",
                                            note=f"当たったか分かりません（{why}）。手順のファイルか Hermes の対話画面で確かめてください")
                if out.get("result") not in ("changed", "unsupported"):  # 時間切れ・分からない: 終わりを確かめるまで、この回はもう当てない
                    break
                continue
            after = out.get("after")  # 鍵を持ったまま読んだ、当てた直後の指紋（場所ごと）
            ok_after = (isinstance(after, dict) and set(after) == set(before["skills"])
                        and all(isinstance(x, str) and len(x) == 64 for x in after.values()))
            await asyncio.to_thread(_hlearn_finish, entry["id"], after if ok_after else None,
                                    state="applied", can_undo=ok_after, note="" if ok_after else "当てたあとの中身を読めなかったので、戻せません")
            applied += 1
            core.hlog(f"Hermes の書き足し {pid} を自動で当てました（今日 {await asyncio.to_thread(_hlearn_today)}/{hermes_learn.LIMIT}）", flush=True)
    return applied


def _hlearn_unchanged(payload: dict, before: dict) -> bool:
    """手順のいまの中身が、当てる前に取った控えと同じか（同じ場所の組で、どの場所も指紋が同じ）。読めない・形が違うときは False。"""
    try:
        now = hermes_learn.run_script(HERMES_CONTAINER, "snap", {"payload": payload})
        got = now.get("skills") if now.get("result") == "snap" else None
        return (isinstance(got, dict) and bool(before) and set(got) == set(before)
                and all(isinstance(got[k], dict) and hermes_learn.digest(got[k]) == hermes_learn.digest(before[k]) for k in before))
    except Exception:
        traceback.print_exc()
        return False


HMEM_MINE_FILE = HLEARN_DIR / "memory-mine.json"  # _HMEM_MINE の控え（取り込む前に本体を起動し直しても失わない）
HMEM_MINE_KEEP = 600  # 書けたか分からない「外す項目」を持ち続ける秒数（時間切れの台本が遅れて書き終える分を待つ）
HMEM_NOT_WRITTEN = ("changed", "refused", "invalid", "state", "busy", "unverifiable")  # 記憶を確かに書いていない結果
_MINE_KEYS = ("sure_added", "sure_removed", "maybe_added", "maybe_removed")  # sure_* は中身の文字列、maybe_* は [中身, 書こうとした時刻]（期限は項目ごと）


def _mine_item(k: str, x):
    if k.startswith("sure_"):
        return x if isinstance(x, str) else None
    ok = (isinstance(x, list) and len(x) == 2 and isinstance(x[0], str) and isinstance(x[1], (int, float)) and not isinstance(x[1], bool))
    return [x[0], float(x[1])] if ok else None


def _load_hmem_mine() -> dict[str, dict]:
    """起動のとき、外す項目の控えを読む。読めない・形が違えば空。"""
    try:
        d = json.loads(HMEM_MINE_FILE.read_text())
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(d, dict) or not all(isinstance(m, dict) and all(isinstance(m.get(k, []), list) for k in _MINE_KEYS) for m in d.values()):
        return {}
    out = {t: {k: [_mine_item(k, x) for x in m.get(k, [])] for k in _MINE_KEYS} for t, m in d.items()}
    if any(x is None for m in out.values() for k in _MINE_KEYS for x in m[k]):
        return {}
    return out


def _save_hmem_mine() -> bool:
    """外す項目の控えを書く（空なら消す）。書けたか。_hlearn_lock の中で呼ぶ。"""
    try:
        if _HMEM_MINE:
            core.write_json(HMEM_MINE_FILE, _HMEM_MINE)
        else:
            HMEM_MINE_FILE.unlink(missing_ok=True)
        return True
    except OSError:
        traceback.print_exc()
        return False


def _hmem_norm(t: str) -> str:
    """読む側（記憶のファイルの読み取り）と同じ形: 改行を LF にそろえ、前後の空白を外す。"""
    return t.replace("\r\n", "\n").replace("\r", "\n").strip()


def _mine_slot(target: str) -> dict:
    return _HMEM_MINE.setdefault(target, {k: [] for k in _MINE_KEYS})


def _maybe(slot: dict, side: str) -> list[str]:
    """書けたか分からない外す項目の中身（[中身, 時刻] から）。"""
    return [x for x, _ in slot["maybe_" + side]]


def _mine_tidy(target: str):
    slot = _HMEM_MINE.get(target)
    if slot is not None and not any(slot[k] for k in _MINE_KEYS):
        _HMEM_MINE.pop(target, None)


def _hmem_mine_change(target: str, mine: dict, phase: str) -> bool:
    """本体が書く記憶の項目（mine: {"added": [...], "removed": [...]}）を「外す項目」として扱う。phase:
      plan  … 書く前。「書けたか分からない」（maybe。書こうとした時刻つき）に足して控えのファイルに書く。書けなければ戻して False（書かない）
      done  … 書けた。maybe から「確かに書けた」（sure）へ移す（sure は期限で消さず、比べるもとに取り込めたら手放す）
      drop  … 確かに書いていない。maybe から外す
    書けたか分からない結果のときは、maybe のまま（照らし合うか、HMEM_MINE_KEEP を過ぎるまで）。"""
    with _hlearn_lock:
        slot = _mine_slot(target)
        now, start = time.time(), {side: len(slot["maybe_" + side]) for side in ("added", "removed")}
        for side in ("added", "removed"):
            ms = slot["maybe_" + side]
            for x in mine.get(side, []):
                if phase == "plan":
                    ms.append([x, now])
                    continue
                # この書き込みで足した分（最後に足したもの）を外す。本体の書き込みは HMEM_LEARN_LOCK で一度にひとつなので、plan から続いている
                i = next((i for i in range(len(ms) - 1, -1, -1) if ms[i][0] == x), None)
                if i is not None:
                    ms.pop(i)
                    if phase == "done":
                        slot["sure_" + side].append(x)
        if phase == "plan":
            if not _save_hmem_mine():
                for side in ("added", "removed"):
                    del slot["maybe_" + side][start[side]:]
                _mine_tidy(target)
                return False
            return True
        _mine_tidy(target)
        _save_hmem_mine()
        return True


async def _hmem_run_mine(op: str, req: dict, target: str, mine: dict) -> dict:
    """本体が記憶を書く（画面の足す・直す・消す、戻す）。書く前に、書く項目を「外す項目」として控えのファイルにも書き
    （書けなければ書かない: 本体を起動し直すと、本人の書き込みが学びになるため）、結果で sure・外す・maybe のままに分ける。
    HMEM_LEARN_LOCK の中で呼ぶ。"""
    if not await asyncio.to_thread(_hmem_mine_change, target, mine, "plan"):
        return {"result": "state", "why": "学んだことの控え（状態フォルダの hermes-learned/）に書けないので、書いていません"}
    out = await _hmem_run(op, req)
    res = out.get("result")
    if res == "applied":
        await asyncio.to_thread(_hmem_mine_change, target, mine, "done")
    elif res in HMEM_NOT_WRITTEN:  # 確かに書いていないときだけ外す（時間切れなど分からないときは、あとで書き終わっても学びにしない）
        await asyncio.to_thread(_hmem_mine_change, target, mine, "drop")
    return out


# 本体が書いた（書こうとした）記憶の項目で、まだ比べるもとに取り込めていないもの（記憶のファイルごと。_hlearn_lock の中で触る）。控えのファイルにも書く
_HMEM_MINE: dict[str, dict] = _load_hmem_mine()


async def _learn_memory(need: str | None = None) -> str:
    """記憶を読み、前に読んだとき（比べるもと）から足された・消えた項目を跡に残し、比べるもとを進める。
    本体が書いた項目（_HMEM_MINE。画面で足す・直す・消したとき、学んだことを戻したとき）は跡から外し、ほかの変化（同じ間に Hermes が書いたもの）は
    跡に残す。跡と比べるもとは、同じ 1 回の書き込みで進める（跡を書けなければ、比べるもとも進めない。次に読んだときに残し直す）。
    外す項目の手放し方: 確かに書けたもの（sure）は、比べるもとに取り込めたら手放す（書いたあとに読んでいるので、もう比べるもとに入っている。
    中身を変えない保存も、ここで消える）。書けたか分からないもの（maybe）は、照らし合って外したものだけを手放し、まだ現れないものは
    HMEM_MINE_KEEP まで持ち続ける（期限を過ぎたものは、差分を見る前に手放す）。読めない記憶のファイルは比べない。
    戻り値は、跡と比べるもとを進めて need の記憶のファイルも読めたら ""。進められなければ "off"・"state"、need を読めなければ "unreadable"
    （本体が記憶を書く前に呼び、"" でなければ書かない）。"""
    if not hsk_on():
        return "off"
    got = await asyncio.to_thread(hermes_memory.read_targets, HERMES_HOME)
    cur = {t: v["entries"] for t, v in got.items() if v["status"] in ("ok", "missing")}
    with _hlearn_lock:
        expired, now_t = False, time.time()
        for t, slot in list(_HMEM_MINE.items()):  # 期限切れの maybe は、差分を見る前に手放す（期限のあとに Hermes が同じ中身を書いた変化を、跡から落とさない）
            for side in ("added", "removed"):  # 期限は項目ごと（あとの書き込みで、前の項目の期限を延ばさない）
                keep = [m for m in slot["maybe_" + side] if now_t - m[1] <= HMEM_MINE_KEEP]
                if len(keep) != len(slot["maybe_" + side]):
                    slot["maybe_" + side] = keep
                    expired = True
            _mine_tidy(t)
        if expired:
            _save_hmem_mine()
        d = hlearn_state()
        if d is None:
            return "state"
        prev = d.get("memory")
        new, used = [], {}
        if prev is not None:
            for t, now in cur.items():
                if t not in prev:
                    continue
                slot = _HMEM_MINE.get(t) or {k: [] for k in _MINE_KEYS}
                skip_a, skip_r = slot["sure_added"] + _maybe(slot, "added"), slot["sure_removed"] + _maybe(slot, "removed")
                added = [e for e in now if e not in prev[t] and e not in skip_a]
                removed = [e for e in prev[t] if e not in now and e not in skip_r]
                used[t] = ([e for e in _maybe(slot, "added") if e in now and e not in prev[t]],  # 照らし合った maybe
                           [e for e in _maybe(slot, "removed") if e in prev[t] and e not in now])
                if added or removed:
                    new.append(({"kind": "memory", "target": t, "added": len(added), "removed": len(removed),
                                 "summary": core._clip(added[0] if added else removed[0], 120), "can_undo": True},
                                {"added": added, "removed": removed}))
        try:
            for entry, detail in new:
                d["entries"].append(_hlearn_new(entry, detail))  # 控えを先に書く（書けなければ OSError で、跡も比べるもとも進めない）
            d["memory"] = {**(prev or {}), **cur}
            _hlearn_save(d)
        except OSError:
            traceback.print_exc()
            return "state"
        changed = False
        for t in cur:  # 比べるもとに取り込めた記憶のファイルの分だけ手放す
            slot = _HMEM_MINE.get(t)
            if slot is None:
                continue
            slot["sure_added"], slot["sure_removed"] = [], []
            if prev is None or t not in prev:  # 初めて読んだ（比べるもとが無い）: もう比べるもとに入った maybe だけを手放す
                ua = [x for x in _maybe(slot, "added") if x in cur[t]]
                ur = [x for x in _maybe(slot, "removed") if x not in cur[t]]
            else:
                ua, ur = used.get(t, ([], []))
            slot["maybe_added"] = [m for m in slot["maybe_added"] if m[0] not in ua]
            slot["maybe_removed"] = [m for m in slot["maybe_removed"] if m[0] not in ur]
            _mine_tidy(t)
            changed = True
        if changed:
            _save_hmem_mine()
    return "" if need is None or need in cur else "unreadable"


def _hlearn_detail(eid: str) -> dict | None:
    try:
        d = json.loads((HLEARN_DIR / f"{eid}.json").read_text())
    except (OSError, ValueError, RecursionError):
        return None
    return d if isinstance(d, dict) else None


def _hlearn_read() -> dict | None:
    with _hlearn_lock:
        return hlearn_state()


def _hlearn_entry(eid: str) -> dict | None:
    d = _hlearn_read()
    return next((e for e in (d or {}).get("entries", []) if e.get("id") == eid), None)


@router.get("/api/hermes/learned")
async def api_hermes_learned():
    """画面の「学んだこと」。跡（新しい順）と、今日自動で当てた数。ハブ（コンテナ）からは使えない口。"""
    if not hsk_on():
        return JSONResponse({"on": False})
    d = await asyncio.to_thread(_hlearn_read)
    if d is None:
        return JSONResponse({"on": True, "problem": f"学んだことの状態（{HLEARN_FILE}）を読めません", "entries": []})
    return JSONResponse({"on": True, "today": await asyncio.to_thread(_hlearn_today), "limit": hermes_learn.LIMIT,
                         "entries": sorted(d["entries"], key=lambda e: -e.get("at", 0))})


@router.post("/api/hermes/learned/detail")
async def api_hermes_learned_detail(body: dict):
    """跡の中身（手順なら差分、記憶なら足した・消した項目）。"""
    eid = str(body.get("id", ""))
    e = await asyncio.to_thread(_hlearn_entry, eid)
    det = await asyncio.to_thread(_hlearn_detail, eid) if e else None
    if not e or det is None:
        return JSONResponse({"ok": False, "reason": "gone"})
    if e.get("kind") == "skill":
        return JSONResponse({"ok": True, "diff": det.get("diff", "")})
    return JSONResponse({"ok": True, "added": det.get("added", []), "removed": det.get("removed", [])})


@router.post("/api/hermes/learned/undo")
async def api_hermes_learned_undo(body: dict):
    """画面の「戻す」（2 段の押しの 2 段目）。手順は、当てた直後から変わっていないときだけ前の中身に戻す。記憶は、足された項目を消し、
    消えた項目を足し直す（もう無い・もうある項目は飛ばす）。ハブ（コンテナ）からは使えない口。"""
    eid = str(body.get("id", ""))
    if not hsk_on():
        return JSONResponse({"ok": False, "reason": "off"})
    async with HLEARN_LOCK:
        e = await asyncio.to_thread(_hlearn_entry, eid)
        det = await asyncio.to_thread(_hlearn_detail, eid) if e else None
        if not e or det is None:
            return JSONResponse({"ok": False, "reason": "gone"})
        if e.get("state") != "applied" or not e.get("can_undo"):
            return JSONResponse({"ok": False, "reason": "done" if e.get("state") == "undone" else "cannot"})
        if e["kind"] == "skill":
            async with HSK_LOCK:
                why = await asyncio.to_thread(_hsk_settled)
                if why:
                    return JSONResponse({"ok": False, "reason": "busy", "why": why})
                if not await asyncio.to_thread(_hsk_mark_running):
                    return JSONResponse({"ok": False, "reason": "state"})
                skills = {k: {"before": det["before"][k], "expect": det["expect"][k]} for k in det.get("expect", {}) if k in det.get("before", {})}
                out = await asyncio.to_thread(hermes_learn.run_script, HERMES_CONTAINER, "restore", {"skills": skills}) if skills else {"result": "failed", "why": "控えがありません"}
                if out.get("result") != "timeout":
                    with _hsk_lock:
                        d = hsk_state()
                        if d is not None:
                            d["verify"] = False
                            with suppress(OSError):
                                core.write_json(HSK_FILE, d)
            res = out.get("result")
            hsk_log(id=e.get("pid", ""), act="undo", summary=e.get("summary", "")[:120], result=res, why=str(out.get("why") or "")[:200])
            if res == "restored":
                await asyncio.to_thread(_hlearn_set, eid, state="undone", undone_at=time.time(), note="")
                return JSONResponse({"ok": True})
            if res == "changed":
                await asyncio.to_thread(_hlearn_set, eid, note="当てたあとに手順が変わっているので、戻せませんでした")
                return JSONResponse({"ok": False, "reason": "changed"})
            await asyncio.to_thread(_hlearn_set, eid, note=f"戻せたか分かりません（{core._clip(str(out.get('why') or res), 120)}）", can_undo=False)
            return JSONResponse({"ok": False, "reason": "unknown", "why": str(out.get("why") or res)[:200]})
        t = e.get("target")
        if t not in hermes_memory.TARGETS:
            return JSONResponse({"ok": False, "reason": "unreadable"})
        skipped, failed = 0, []
        async with HMEM_LEARN_LOCK:  # 書いてから比べるもとを進めるまで、見張りに読ませない
            why = await _learn_memory(t)  # 戻す前に、ここまでの Hermes の変化を跡に残す（戻す項目と同じ中身の Hermes の変化を、外す項目に紛れさせない）
            if why:
                return JSONResponse({"ok": False, "reason": why})
            got = await asyncio.to_thread(hermes_memory.read_targets, HERMES_HOME)
            if got.get(t, {}).get("status") not in ("ok", "missing"):
                return JSONResponse({"ok": False, "reason": "unreadable"})
            now = got[t]["entries"]
            for x in det.get("added", []):
                if x not in now:
                    skipped += 1
                    continue
                out = await _hmem_run_mine("remove", {"target": t, "entry": x}, t, {"added": [], "removed": [x]})
                if out.get("result") != "applied":
                    failed.append(str(out.get("result")))
            for x in det.get("removed", []):
                if x in now:
                    skipped += 1
                    continue
                if hermes_memory.text_problem(x):  # 長すぎるなど、画面の口では書けない項目は、戻せなかったとして扱う（飛ばしたことにしない）
                    failed.append("書けない項目")
                    continue
                out = await _hmem_run_mine("add", {"target": t, "content": x}, t, {"added": [_hmem_norm(x)], "removed": []})
                if out.get("result") != "applied":
                    failed.append(str(out.get("result")))
            await _learn_memory()
        if failed:
            await asyncio.to_thread(_hlearn_set, eid, note=f"一部を戻せませんでした（{', '.join(failed)[:80]}）")
            return JSONResponse({"ok": False, "reason": "partial", "why": ", ".join(failed)[:200]})
        await asyncio.to_thread(_hlearn_set, eid, state="undone", undone_at=time.time(), note=f"もう変わっていた {skipped} 項目は飛ばしました" if skipped else "")
        return JSONResponse({"ok": True, "skipped": skipped})


async def _watch_memory():
    """見張りの回の記憶の読み取り。本体が記憶を書いて比べるもとを進めている間は待つ（HMEM_LEARN_LOCK）。"""
    async with HMEM_LEARN_LOCK:
        await _learn_memory()


async def watch_hermes_skills():
    """1 分おきに: 承認待ちの手順の書き足しを、今日の上限まで自動で当てる（学んだこと）。記憶の変化を跡に残す。
    当てたら、どの画面も見えていなければ 1 通知らせる（中身は載せない）。当てずに残った新しい承認待ちも 1 通知らせる。"""
    while True:
        await asyncio.sleep(60)
        await _watch_once()


async def _watch_once():
    """見張りの 1 回。手順（承認待ちを読む・当てる・知らせる）と記憶は別々に動かす（手順の承認待ちを読めなくても、記憶の変化は跡に残す）。"""
    try:
        ids = await asyncio.to_thread(hermes_skills.pending_ids, HERMES_HOME) if HERMES_HOME.is_dir() else None
        if ids is not None:
            new = await asyncio.to_thread(_hsk_tidy, set(ids))
            applied = await _learn_skills() if ids else 0
            if applied:
                await core.push_all("voice-loop｜Hermes", f"ハブが手順を {applied} 件書き足しました（💡 の「学んだこと」で見て、戻せます）", "hermes-skills")
            elif new and await asyncio.to_thread(hermes_skills.pending_ids, HERMES_HOME):
                await core.push_all("voice-loop｜Hermes", f"ハブが手順への書き足しを {len(new)} 件出しました（今日の自動の上限か、画面で扱えない形です。💡 で見られます）", "hermes-skills")
    except asyncio.CancelledError:
        raise
    except Exception:
        traceback.print_exc()
    try:
        await _watch_memory()
    except asyncio.CancelledError:
        raise
    except Exception:
        traceback.print_exc()


class Hermes(Brain, Talker):
    """Hermes（ハブ）。鍵（VL_HERMES_KEY）を書くと、一覧の先頭に出る。"""

    pane = HERMES_PANE
    name = "hermes"
    label = "Hermes"
    off_text = "Hermes は設定されていません（config.env の VL_HERMES_KEY）。選び直してください。"

    def on(self) -> bool:
        return bool(HERMES_KEY)

    def entry(self) -> dict:
        return hermes_agent()

    def history(self) -> list[dict]:
        return hermes_history()

    def sending_text(self) -> str:
        return "ハブに送りました。考え中…"

    async def exchange(self, text: str, reply_at: float | None = None, extra: str = "", item: int | None = None) -> tuple[str, str, bool]:
        return await hermes_exchange(text, reply_at, extra, item)

    def ask(self, text: str) -> str:
        return ask_hermes(text)

    async def refused(self, text: str, why: str):
        # 往復と同じ鍵の中で載せる: 前の往復（画面が止めても裏で続く）がまだ返事を待っていれば、それが終わって最後に聞いた文を書いたあとで
        # 空にする（鍵の外で空にすると、あとから終わった往復が古い返事で書き戻し、利用者に届いていない問いが次の発言の「聞いた文」になる）
        async with core.pane_lock(HERMES_PANE):
            turn = hermes_turn_begin(text, heard="", refused=True)  # 断った発言も台帳に載せる（「すぐ次の発言」を進め、保留中の確認を失効させる。承認・閉じる許可にはならない）
            HERMES_HEARD["text"] = ""  # 利用者が聞くのは本体の断り（前に聞いた問いへの答えに、次の発言を使わせない。失敗のときと同じ）
            await asyncio.to_thread(hermes_log, "error", why[:300], turn=turn)
            _unsaved.discard(turn)  # 画面には断りをそのまま出すので、記録に書けたかは返さない

    def put_attachment(self, name: str, data: bytes) -> str:
        """Hermes はコンテナの中なので、コンテナの中の置き場（/opt/data/voice-loop/attachments/）に、固定の台本で置く（ホストからは書かない）。"""
        return attach.put_container(hermes_skills.docker_bin(), HERMES_CONTAINER, hermes_skills.USER, hermes_skills.PY, name, data)

    def ensure(self) -> dict:
        """Hermes がハブのときは立てるものが無いので、使える状態かだけ確かめる。"""
        why = hermes_problem()
        return {"ok": False, "reason": "hermes", "detail": why} if why else {"ok": True, "pane": HERMES_PANE, "started": False}

    async def boot(self):
        core.hlog(f"ハブは Hermes: {hermes_problem() or '使えます'}", flush=True)

    def watchers(self) -> list:
        return [watch_hermes_skills, hermes_check.watch]

    def doctor(self, add):
        problem = hermes_problem()
        add("ハブ（Hermes）", not problem, problem)
        if not HERMES_HOME.is_dir():
            add("Hermes の書き足し（💡 で採る）", False, f"Hermes の置き場 {HERMES_HOME} がありません。別の場所なら config.env の VL_HERMES_HOME に書いてください", optional=True)
        else:
            problem = _hsk_problem()
            add("Hermes の書き足し（💡 で採る）", not problem, problem, optional=True)

    def turns(self, n: int = 1) -> list[tuple[str, str]]:
        return [(row[0], row[1]) for row in list(HERMES_TURNS)[::-1][:n]]

    def heard(self, turn: str) -> str:
        return next((row[3] or "" for row in HERMES_TURNS if row[0] == turn), "")

    def reply_to(self, turn: str) -> str:
        return next((row[2] for row in HERMES_TURNS if row[0] == turn), "")

    def replying(self, turn: str) -> str | None:
        return next((row[5] if len(row) > 5 else "" for row in HERMES_TURNS if row[0] == turn and len(row) > 4 and row[4]), None)

    def reply_ask(self, turn: str) -> dict | None:
        for row in HERMES_TURNS:
            if row[0] == turn and len(row) > 4 and row[4]:
                ask = row[6] if len(row) > 6 and isinstance(row[6], dict) else {}
                return {"pane": str(ask.get("pane") or ""), "fp": str(ask.get("fp") or ""), "session": str(ask.get("session") or "")}
        return None

    async def report_target(self) -> dict | None:
        return hermes_agent() if HERMES_KEY else None

    def reply_topic(self) -> str:
        return reply_topic()

    def topic_report(self, tid: str) -> dict | None:
        return topic_report(tid)

    def report_begin(self, headline: str, about: str) -> bool:
        """Hermes の会話は本体が記録する（画面が無くても、あとで履歴に出る）。"""
        return hermes_log("note", headline, about=about)

    async def report_end(self, state: str, reply: str, about: str, saved: bool, pane: str = "", topic: str = "", ask: dict | None = None) -> bool:
        # 利用者が次に聞くのはこの報告（受け取れなければ本体の失敗の知らせ）。記録に書けたかに関係なく、ここで置き換える
        HERMES_HEARD["text"] = reply if state == "done" and reply.strip() else ""
        if state == "done" and reply.strip():
            # pane: どのセッションの報告か（返信の守りが照らす）。topic: どの件の報告か（件の URL で開くとき・返信の続きを同じ件にするとき）。
            # ask: 止まった報告でハブが見た問い（返信をその問いへの答えに使えるかを、本体が照らす）
            link = ({"ask": {"fp": ask["fp"], "session": ask["session"]}}
                    if isinstance(ask, dict) and isinstance(ask.get("fp"), str) and _FP.fullmatch(ask["fp"]) and isinstance(ask.get("session"), str) and ask["session"] else {})
            return hermes_log("ai", reply, report=True, about=about, pane=pane, **({"topic": topic} if topics.ID_RE.fullmatch(topic or "") else {}), **link) and saved
        failed = f"ハブから報告を受け取れませんでした: {core._clip(reply, 200) or '返事が空でした'}"
        saved = hermes_log("error", failed, about=about) and saved
        for c in list(core.CLIENTS):  # 開いている画面には、いま知らせる（記録に書けなければ、ここでしか伝わらない）
            try:
                await c.send(type="notice", text=f"{about}: {failed}", saved=saved)
            except Exception:
                pass
        return saved



TALKER = Hermes()


def mount(app):
    """画面の口を本体に足す。include_router だと app.routes に 1 つの塊で入り、口を全部たどる試験（container_guard_test）から見えなくなるので、1 本ずつ足す。"""
    for r in [*router.routes, *hermes_inbox.router.routes]:
        app.add_api_route(r.path, r.endpoint, methods=list(r.methods))


def setup(server_module):
    """本体が読み込みの最後に呼ぶ。本体を core に置き、前の起動から続く状態を戻す。"""
    global core
    core = server_module
    hermes_check.sent_reader = lambda since, project="", limit=200: core.read_sent(since, project, limit)  # 判断待ちの確かめが、出ていく側の記録を読む口
    _load_hermes_turns()
    _recover_hermes_skills()
