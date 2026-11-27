# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "anthropic>=1.0", "openai>=1.40", "pywebpush>=2.0"]
# ///
"""voice-loop: トランシーバー式で herdr のエージェントと声で話す中継サーバー。

ブラウザ(タップ→話す→タップ) → whisper-server → herdr agent prompt → セッション記録(JSONL)から返信を読む
→ 語り手(Haiku, stream) → 文ごとに声のエンジン（AivisSpeech か VOICEVOX） → ブラウザで再生

話し相手は個別のセッションか「ハブ」。ハブは hub/ で動く Claude Code セッションで、hubctl 経由で
他の全セッションの状況を読み、指示を送る（/api/hub/*）。
"""
import asyncio
import concurrent.futures
import contextlib
import contextvars
import functools
import base64
import hashlib
import io
import json
import math
import os
import random
import re
import secrets
import shutil
import signal
import stat
import struct
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import wave
from contextlib import aclosing, asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import anthropic
import openai
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "scripts"))
from origin_guard import SameOriginOnly, device_place, device_ports  # noqa: E402  接続元の確かめ・機械の口で受けた接続か
import consent  # noqa: E402  利用者の発言が、許す・選ぶの答えになっているか（承認に答える・閉じるの守り）
import ask_form  # noqa: E402  選択肢の問い（AskUserQuestion）に、画面と Slack から答える
from places import CONFIG_DIR, STATE_DIR  # noqa: E402  端末ごとの設定と状態の置き場
import attach  # noqa: E402  画面から添付したファイル
import sent_log  # noqa: E402  出ていく側の記録（案件の Teams の控えの、当方の投稿）
import quick  # noqa: E402  本体がその場で答える決まった問い（次は・どうなってる・待ってるのある。LLM・頭を通さない）


def _load_config_env():
    """CONFIG_DIR/config.env の「名前=値」を、まだ無い環境変数として読み込む。"""
    try:
        lines = (CONFIG_DIR / "config.env").read_text().splitlines()
    except OSError:
        return
    for line in lines:
        name, sep, value = line.partition("=")
        if sep and name.strip() and not name.lstrip().startswith("#"):
            os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


_load_config_env()
import hub_hermes as hermes  # noqa: E402  頭の部品。読み込みのときに config.env の値を読むので、ここで読み込む
import slack_report  # noqa: E402  Slack から頼まれた件の報告を、同じスレッドにも返す（config.env の値を読むので、ここで読み込む）
import topics  # noqa: E402  件（頼んだ件・報告・Slack のスレッド・問いを結ぶ単位）。件の URL（/#t=<件>）で画面を開く
from hub_brain import AGENT  # noqa: E402  話しかける相手の呼び名（config.env の VL_AGENT_NAME。無ければ「ハブ」）
WHISPER = os.environ.get("VL_WHISPER", "http://127.0.0.1:8080/v1/audio/transcriptions")
# 聞き取りの言語。毎回 whisper に渡す（自動判定に任せると、短い発言や雑音で日本語以外と取り違える。2026-11 の記録で 283 件中 5 件）。
# 空（VL_WHISPER_LANG=）にすると渡さず、whisper の起動時の設定に任せる
WHISPER_LANG = os.environ.get("VL_WHISPER_LANG", "ja").strip()
# 声の区間だけを聞き取らせる（whisper の vad=true）。声の入っていない雑音が「はい」「コミット」と聞き取られ、承認やセッションへの
# 指示として届くのを防ぐ（2026-11-22 実測。雑音と無音の 8 本は空になり、本物の短い発話は同じ文字）。whisper の起動に VAD のモデル（-vm）が要る。
# off にすると渡さない
WHISPER_VAD = os.environ.get("VL_WHISPER_VAD", "on").strip().lower() != "off"
# 聞き取りに渡す音声の拡張子。画面の録音（webm・mp4）と、機械の録音（ogg・opus・wav）。ほかは webm として渡す
AUDIO_EXTS = ("webm", "mp4", "m4a", "ogg", "opus", "wav")
# 声のエンジン。どちらも同じ形の API（audio_query → synthesis）で、違うのは呼び先と既定の声だけ
VOICE_ENGINES = {
    "aivis": ("AivisSpeech", "http://127.0.0.1:10101", 888753760),  # まお / ノーマル
    "voicevox": ("VOICEVOX", "http://127.0.0.1:50021", 8),  # 春日部つむぎ / ノーマル。軽い（2026-10-21 実測: 1文 0.9秒・待機 78MB）
}
VOICE = os.environ.get("VL_VOICE", "aivis").strip().lower()
VOICE_NAME, _voice_url, _voice_speaker = VOICE_ENGINES.get(VOICE, VOICE_ENGINES["aivis"])  # 知らない名前なら AivisSpeech で動き、点検で知らせる
# VL_AIVIS は 0.2 までの名前。名前のとおり AivisSpeech のときだけ効かせ、VOICEVOX に替えた人の呼び先を古い値で上書きしない
TTS_URL = os.environ.get("VL_VOICE_URL") or (os.environ.get("VL_AIVIS") if VOICE_NAME == "AivisSpeech" else None) or _voice_url
# 語り手（返答を話し言葉に直す軽いモデル）。既定は NARRATOR_MODEL。narrator_model というファイルに1行書けば、再起動なしで切り替わる
#   名前に "/" が無い … Anthropic 直結。認証は `ant auth login` のプロフィール（~/.config/anthropic/）。API キー不要
#   名前に "/" がある … Vercel AI Gateway（OpenAI 互換）。認証は gateway/ に紐づけた Vercel プロジェクトの OIDC トークン
NARRATOR_MODEL = os.environ.get("VL_NARRATOR_MODEL", "claude-haiku-4-5")
NARRATOR_FILE = CONFIG_DIR / "narrator_model"
narrator_client = anthropic.AsyncAnthropic()
NARRATOR_PROMPT = (HERE / "narrator_prompt.md").read_text()
GATEWAY_URL = "https://ai-gateway.vercel.sh/v1"
GATEWAY_DIR = CONFIG_DIR / "gateway"  # `vercel link` 済みのフォルダ。.env.local の VERCEL_OIDC_TOKEN は12時間で切れる
GATEWAY_PULL = ["npx", "-y", "vercel@63.1.2", "env", "pull", ".env.local", "--yes"]  # チームは紐づけ情報（.vercel/）から決まる
# AivisSpeech が誤読する英字の語（2026-10-12 に読みを実測）。語り手が英字のまま残しても正しく読めるよう、合成の直前に置き換える。
# 利用者の語（プロジェクト名など）は、設定フォルダの readings.txt に「語=よみ」で足す
READINGS = {"herdr": "ハーダー", "launchd": "ローンチディー", "hubctl": "ハブコントロール", "ghostty": "ゴースティ"}


def _load_readings():
    try:
        lines = (CONFIG_DIR / "readings.txt").read_text().splitlines()
    except OSError:
        return
    for line in lines:
        word, sep, reading = line.partition("=")
        if sep and word.strip() and reading.strip() and not word.lstrip().startswith("#"):
            READINGS[word.strip().lower()] = reading.strip()


_load_readings()
READING_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(map(re.escape, sorted(READINGS, key=len, reverse=True))) + r")(?![A-Za-z])", re.I)
PROJECTS = Path.home() / ".claude" / "projects"
DEFAULT_SPEAKER = int(os.environ.get("VL_DEFAULT_SPEAKER") or _voice_speaker)  # 画面が声を知らせてこないときの声
NOTE_PREFIX = "[voice-loop 通知]"  # ハブへの自動通知の目印。ユーザーの発言と区別する
WATCH_TIMEOUT = 3 * 3600  # 頼んだ件を見張る上限
# herdr 経由の文は、改行があるか約800字を超えると Claude Code が「貼り付けられた内容」として包み、
# 受け手は指示として扱いにくくなる（2026-10-12 実測: 692字は通常、842字は貼り付け扱い）。指示と通知は一行・この字数以内にする
PROMPT_MAX = 700
BG_NOTICE_AFTER = float(os.environ.get("VL_BG_NOTICE_AFTER", "180"))  # 相手の裏の作業が長引いたとき、「まだ動いている」と一度知らせるまでの秒数
# ハブがセッションを立ててよいのは、この直下の既存プロジェクトだけ。未設定なら、セッションの起動は使えない
PROJECTS_DIR = Path(os.environ["VL_PROJECTS_DIR"]).expanduser() if os.environ.get("VL_PROJECTS_DIR") else None
SPAWN_MODELS = {"opus", "sonnet", "haiku", "fable"}
SPAWN_PREFIX = "hubjob-"  # ハブが立てたセッションの herdr 上の名前の頭（ハブが立てたものと見分けるため）
PASTED = re.compile(r'^\s*<pasted_content id="[^"]*">\n(.*)\n</pasted_content id="[^"]*">\s*$', re.S)
# 無音から whisper が生む定番文。これが来たら「聞こえなかった」扱い
HALLUCINATIONS = {"ご視聴ありがとうございました", "次の動画でお会いしましょう", "チャンネル登録お願いします", "おやすみなさい", "ありがとうございました"}
SENTENCE_END = re.compile(r"(?<=[。！？!?\n])")

@asynccontextmanager
async def lifespan(_app):
    try:
        notices_tidy()  # 前の版が残した知らせの題・一言を、記録から取り除く
    except Exception:
        traceback.print_exc()
    tasks = [
        asyncio.create_task(boot_ensure_hub()),  # サーバー起動時（= Mac の再起動後）にハブを立て直す
        asyncio.create_task(keep_gateway_token()),
        asyncio.create_task(watch_blocked()),  # 承認・入力待ちになったセッションを、プッシュ通知で知らせる
        *(asyncio.create_task(watch()) for watch in HUB.watchers()),  # 頭が持っている裏の仕事
    ]
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(lifespan=lifespan)
app.add_middleware(SameOriginOnly)  # ブラウザで開いた別のサイトからの接続を断る
hermes.mount(app)


def hlog(msg: str, flush: bool = True):
    """ハブまわりの出来事をログに残す。行頭は [hub]、行末に時刻。"""
    print(f"[hub] {msg}  @{time.strftime('%H:%M:%S')}", flush=flush)


# ---------- herdr ----------
def herdr(*args: str, timeout: float | None = 60) -> dict:
    try:
        p = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"raw": str(e), "code": -1}
    return _herdr_out(p.stdout, p.stderr, p.returncode)


def _herdr_out(stdout: str, stderr: str, code: int) -> dict:
    out = (stdout or "").strip() or (stderr or "").strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"raw": out, "code": code}


_HERDR = herdr  # 本物の herdr（_if_still が、確かめと送り始めを停止と直列にするのに使う。試験の差し替えでは使わない）
# 停止の確定（止めた番号を進める・送る前の指示を取り消す）・次の発言の受け付けと、操作の送り始め（承認のキー・閉じる・ハブ以外への指示）を直列にする鍵。
# 送り始めたら放す（返事を待つ間は持たない）。「止めました」と出たあとに、止めた発言での操作を始めない（codex-review 635eea0 の P1）
COMMIT_LOCK = threading.Lock()  # 鍵の中で受け付け・停止・送り始めを呼び合わない（入れ子にしない）


# 本体が読んだが、まだ確定していない停止・次の発言の受け付けの数（読んだその場で数え、鍵の中で確定したら戻す）。承認のキー・閉じるは、確定待ちの
# 停止・受け付けがあれば始めない。ハブ以外への指示を送るのは、確定待ちの停止があれば始めない（鍵を待つ間に、先に待っていた古い操作が始まらない。
# codex-review bee122f の P1）
PENDING = {"stop": 0, "admit": 0}
PENDING_WAIT = 30  # 指示の送りが、確定待ちの停止の確定を待つ上限（秒）
# 停止・受け付けの鍵待ちと、送り途中の操作の待ちに使う専用の実行枠（返事待ちの仕事が既定の実行枠を使い切っても、停止と受け付けを並ばせない。codex-review a1fa82c）
COMMIT_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="commit")


async def _acquire_commit():
    """COMMIT_LOCK の空きを、イベントループを止めずに待って取る。待つ間に取り消されたら、あとで取れた鍵はその場で放す（取り残さない。codex-review bee122f）"""
    if COMMIT_LOCK.acquire(blocking=False):  # 空いていればその場で取る
        return
    fut = asyncio.get_running_loop().run_in_executor(COMMIT_POOL, COMMIT_LOCK.acquire)
    try:
        await asyncio.shield(fut)
    except asyncio.CancelledError:
        fut.add_done_callback(lambda f: COMMIT_LOCK.release() if not f.cancelled() and f.exception() is None and f.result() else None)
        raise


# 確定待ちの数を数える・見るときの鍵（ごく短く持つ）。指示の送りの確定（確定待ちの停止が無いのを見て送ると決める）と、停止を読んだ印を、
# この鍵で順に並べる（見たあと送ると決める前に停止を読んで、止めたあとに送らない。codex-review 27fc1d4）
PENDING_LOCK = threading.Lock()


def _admit_order() -> asyncio.Lock:
    """受け付けを読んだ順に確定する鍵（このイベントループの。asyncio の鍵は待った順に渡す。codex-review 27fc1d4 の P1）"""
    loop = asyncio.get_running_loop()
    lk = getattr(loop, "_vl_admit_order", None)
    if lk is None:
        lk = asyncio.Lock()
        loop._vl_admit_order = lk
    return lk


@contextlib.asynccontextmanager
async def invalidating(kind: str, order: asyncio.Lock | None = None):
    """停止（kind="stop"）・次の発言の受け付け（"admit"）を確定する。読んだその場で確定待ちに数え、（order があれば読んだ順に）鍵を取ってから確定し、
    確定待ちから外して鍵を放す。"""
    with PENDING_LOCK:
        PENDING[kind] += 1
    counted = True
    try:
        if order is not None:
            await order.acquire()
        try:
            await _acquire_commit()
            try:
                yield
            finally:
                with PENDING_LOCK:
                    PENDING[kind] -= 1
                counted = False
                COMMIT_LOCK.release()
        finally:
            if order is not None:
                order.release()
    finally:
        if counted:
            with PENDING_LOCK:
                PENDING[kind] -= 1


HELD_TIMEOUT = 15  # 返事を待たない操作（承認のキー・閉じる・指示を送る）を herdr が返すのを待つ上限（秒）。ふつうはすぐ返る
HELD_MARGIN = 5  # 停止が送り途中の操作を待つのは、HELD_TIMEOUT よりこれだけ長く
UNSURE_HOLD = 60  # herdr の待ちを打ち切ってから、これだけの間の停止は、止めたか確かめられないと返す（打ち切っても herdr があとで操作することがある）
UNSURE_UNTIL = {"t": 0.0}
HELD_OPS: set[threading.Event] = set()  # 起動して、まだ返っていない承認のキー・閉じる（返ったら立つ印）


def _acquire_for_send(end: float) -> bool:
    """指示を送るための鍵を取る。確定待ちの停止がある間は取らずに待ち、期限（end）を過ぎても確定待ちの停止があれば False（鍵は持たない）。"""
    while True:
        if COMMIT_LOCK.acquire(timeout=0.1):
            if not PENDING["stop"]:
                return True
            COMMIT_LOCK.release()
            if time.monotonic() >= end:
                return False
            time.sleep(0.02)
        elif PENDING["stop"] and time.monotonic() >= end:
            return False


def _herdr_gated(still, args: tuple, timeout: float | None, hold: bool = False, wait_lock: bool = False) -> dict | None:
    """鍵の中で確かめて herdr を起動し、起動したら鍵を放す（鍵は短く持つ。停止・次の発言の受け付けを待たせない）。確かめが False なら起動せずに None。
    wait_lock でなければ（承認のキー・閉じる）、鍵が空いていなければ待たずに断る（待つ間に相手・問いが入れ替わらないように。codex-review e749b6a の P1）。
    wait_lock なら（ハブ以外への指示を送る）、鍵が空くのを待つ。hold なら、返るまで送り途中の印を持つ（停止は、送り途中の操作が返るのを待ってから
    「止めました」と返す。起動しただけでは herdr がまだ受け取っていないことがあり、「止めました」のあとに届かないように。codex-review b7bf623・57c6291 の P1）"""
    if wait_lock:
        # 確定待ちの停止があれば、鍵をいったん放して確定を待つ（停止の側が送る前の指示を取り消して「送っていません」と知らせる。ここで黙って
        # 送らずに終えると、知らせがどこからも出ない。codex-review a1fa82c）。上限（鍵を取る前から数える）を過ぎても確定しなければ送らず、
        # 送れなかったと返す（送る仕事の終わりで「送信できませんでした」と知らせる。codex-review 070146f・31c9e1c）
        end = time.monotonic() + PENDING_WAIT
        while True:
            if not _acquire_for_send(end):
                return {"error": {"code": "stop_pending", "message": "止める操作の確定を待ちきれなかったので、送っていません"}}
            with PENDING_LOCK:  # 確定待ちの停止が無いのを見て、送ると決めるまでを、停止を読んだ印と順に並べる（codex-review 27fc1d4）
                again = bool(PENDING["stop"])
                ok = False if again else still()
            if not again:
                break
            COMMIT_LOCK.release()
    elif not COMMIT_LOCK.acquire(blocking=False):
        return None
    else:
        with PENDING_LOCK:  # 本体が読んだが確定していない停止・次の発言の受け付けがあれば、承認のキー・閉じるは始めない
            ok = not (PENDING["stop"] or PENDING["admit"]) and still()
    ev = None
    try:
        if not ok:
            return None
        try:
            proc = subprocess.Popen(["herdr", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except OSError as e:
            return {"raw": str(e), "code": -1}
        if hold:
            ev = threading.Event()
            HELD_OPS.add(ev)
    finally:
        COMMIT_LOCK.release()
    r = None
    try:
        r = _herdr_wait(proc, min(timeout or HELD_TIMEOUT, HELD_TIMEOUT) if hold else timeout)
        return r
    finally:
        if ev is not None:
            # 待ちを打ち切った（herdr が返らなかった）・待つ途中で失敗したら、操作が届いたか分からない。停止は「止めました」と言わずに、
            # 止めたか確かめられないと返す（codex-review d9db7c8 の P1）
            ev.ok = _herdr_sure(r)
            if not ev.ok:  # 印が一覧から外れたあとに来た停止にも、しばらくは確かめられないと返す（codex-review 9d641dc の P1）
                UNSURE_UNTIL["t"] = max(UNSURE_UNTIL["t"], time.monotonic() + UNSURE_HOLD)
            ev.set()
            HELD_OPS.discard(ev)


SURE_REJECTS = {"agent_blocked"}  # 操作を実行していないと分かる herdr の拒否（agent_blocked は、入力を送る前に断る。herdr agent prompt --help）
SURE_ACCEPTED = {"agent_prompt_stalled"}  # 受け取ったと分かる herdr の返事（送ったが 5 秒で working を見なかった）


def _herdr_sure(r) -> bool:
    """herdr の返事から、操作が届いた・届いていないと確かめられたか。結果（result）か、実行していないと分かる拒否だけ。打ち切り・中身の分からない
    返事・ほかの error（応答待ちの打ち切りなど）は、届いたか分からない（codex-review e7ab276・711ce71 の P1）"""
    if not isinstance(r, dict) or r.get("timeout"):
        return False
    if "result" in r:
        return True
    err = r.get("error")
    return isinstance(err, dict) and (err.get("code") in SURE_REJECTS or err.get("code") in SURE_ACCEPTED)


def wait_inflight(evs: list) -> bool:
    """送り途中の操作（承認のキー・閉じる・指示を送る）が返るのを待つ（ワーカーのスレッドで呼ぶ）。herdr の待ちの上限（HELD_TIMEOUT 秒で打ち切って
    印を立てる）より少し長く待つ。全部ふつうに返ったら True。返らない・打ち切った・待つ途中で失敗したなら False（止めたか確かめられない）"""
    end = time.monotonic() + HELD_TIMEOUT + HELD_MARGIN
    return all(ev.wait(max(0.0, end - time.monotonic())) and getattr(ev, "ok", True) for ev in evs)


def _herdr_wait(proc, timeout: float | None) -> dict:
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as e:
        proc.kill()
        proc.communicate()
        return {"raw": str(e), "code": -1, "timeout": True}  # 打ち切った（herdr が操作を受け取ったかは分からない）
    return _herdr_out(out, err, proc.returncode)


def read_screen(pane: str, lines: int) -> str:
    """`herdr agent read` はプレーンテキストを返す。"""
    p = subprocess.run(["herdr", "agent", "read", pane, "--source", "recent-unwrapped", "--lines", str(lines)], capture_output=True, text=True, timeout=20)
    return "\n".join(l for l in p.stdout.splitlines() if l.strip())


STATUS_LINE = re.compile(r"^\s*(📂|⏵⏵|⏸|✻)")  # 状態表示の行（費用や経過時間で変わる）
OPTION = re.compile(r"^\s*(?:❯\s*)?(\d+)\.\s+(\S.*)$")
ODD_BREAKS = re.compile("[\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")  # 画面の 1 行の中にあっても、Python の splitlines が行の区切りとみなす文字


def read_prompt(pane: str) -> str:
    """承認・入力待ちで止まっている画面。対話の枠は visible でしか取れない（recent には出ない）。
    罫線と、内容に関係なく変わる状態表示の行は除く。"""
    p = subprocess.run(["herdr", "agent", "read", pane, "--source", "visible", "--lines", "45"], capture_output=True, text=True, timeout=20)
    # 行は画面の行（\n）だけで分ける。行の中の特殊な改行文字は � にし、あとの読み取りで偽の行を作らせない（選択肢を偽る形。2026-11-05 Codex）
    rows = [ODD_BREAKS.sub("\ufffd", l) for l in p.stdout.split("\n")]
    keep = [l.rstrip() for l in rows if l.strip() and not STATUS_LINE.match(l) and not set(l.strip()) <= set("─╌")]
    return "\n".join(keep[-30:])


def parse_prompt(text: str) -> dict:
    """止まっている画面から、問いの種類と選択肢を読む。末尾の操作案内（Esc to cancel）から上へたどり、1番まで集める
    （それより上にある会話中の番号つきリストを選択肢と取り違えないため）。"""
    lines = text.splitlines()
    foot = next((i for i in range(len(lines) - 1, -1, -1) if "Esc to cancel" in lines[i]), None)
    opts: dict[int, str] = {}
    if foot is not None:
        for l in reversed(lines[:foot]):
            m = OPTION.match(l)
            if m:
                opts[int(m.group(1))] = m.group(2).strip()
                if m.group(1) == "1":
                    break
    if opts.get(1, "").lower() == "yes" and any(v.lower().startswith("no") for v in opts.values()):
        return {"kind": "permission", "options": opts}
    choices = {n: t for n, t in opts.items() if t not in ("Type something.", "Chat about this")}
    return {"kind": "question" if 1 in choices else "unknown", "options": choices}


CONFIRM_OPT = re.compile(r"^ {1,3}(❯ |  )(Yes|No)$")  # 番号の無い確認の選択肢の行（「 ❯ No」「   Yes」）。行の全体がこの形のときだけ
CONFIRM_Q = re.compile(r"^ ?[^\s❯]")  # 確認の問いの文の行（字下げは 1 字まで、カーソルの印なし）。選択肢と同じ字下げの行（「   Maybe」）は問いとしない


def parse_confirm(text: str) -> dict | None:
    """番号の無い「Yes / No」の確認（Claude Code の道具が出す「Permanently delete "…"?  ❯ No / Yes」など。初めは No を指している）。
    末尾の操作案内（Esc to cancel）のすぐ上が Yes と No の 2 行だけで、カーソル（❯）がちょうど 1 つ、その上に問いの文が 1 行あるときだけ読む。
    返すのは {"question": 問いの文, "order": 画面の上からの並び, "cursor": ❯ の位置（0 か 1）}。この形でなければ None（答えない）。"""
    lines = text.splitlines()
    foot = next((i for i in range(len(lines) - 1, -1, -1) if "Esc to cancel" in lines[i]), None)
    if foot is None or foot < 3 or foot != len(lines) - 1:  # 案内が画面の最後の行でなければ（下に別の入力・選択肢が続く）、確認と読まない（a2a）
        return None
    ms = [CONFIRM_OPT.match(l) for l in lines[foot - 2:foot]]
    if not all(ms):
        return None
    order, marks = [m.group(2) for m in ms], [m.group(1) == "❯ " for m in ms]
    question = lines[foot - 3].strip()
    # 問いの文の行は、選択肢の行と字下げで分ける（Maybe / No / Yes の 3 択の Maybe を問いと読んで、No / Yes の 2 択と取り違えない。a2a）
    if (sorted(order) != ["No", "Yes"] or marks.count(True) != 1 or not question or not CONFIRM_Q.match(lines[foot - 3])
            or CONFIRM_OPT.match(lines[foot - 3]) or OPTION.match(lines[foot - 3])):
        return None
    return {"question": question, "order": order, "cursor": marks.index(True)}


def confirm_norm(text: str) -> str:
    """確認の画面の照らし合わせ用。行の頭の ⏺（実行を待つ道具の見出しの印。点滅して空白になる）だけを空白にそろえ、ほかの字・空白・
    行の区切りはそのまま残す（空白の連なりや行の区切りの違いも、別の画面とみなす。a2a）。"""
    return "\n".join(" " + l[1:] if l.startswith("⏺") else l for l in text.splitlines())


def confirm_text(text: str, at: int | None) -> str:
    """番号の無い確認の画面（parse_confirm が読めるもの）で、カーソル（❯）を選択肢の at 番目（0 か 1）に置いた画面。at が None なら、どちらにも置かない。"""
    lines = text.splitlines()
    foot = next(i for i in range(len(lines) - 1, -1, -1) if "Esc to cancel" in lines[i])
    for k, i in enumerate((foot - 2, foot - 1)):
        m = CONFIRM_OPT.match(lines[i])
        lines[i] = lines[i][:m.start(1)] + ("❯ " if k == at else "  ") + m.group(2)
    return "\n".join(lines)


QUOTED = re.compile(r'"([^"\n]{1,200})"|“([^”\n]{1,200})”|「([^」\n]{1,200})」|『([^』\n]{1,200})』')


def confirm_targets(text: str) -> list[str]:
    """番号の無い確認の問いの文（parse_confirm の question）の、引用符で囲まれた対象の名前（Permanently delete "受注の記録"? の「受注の記録」）。
    空白だけの名前は数えない。確認でない・名前が無ければ空。問いの文の引用符の数が、取り出した名前の数のちょうど 2 倍でなければ
    （名前の中に同じ引用符がある・閉じていない引用符がある）、名前を確かに読み取れないとして空（聞き返しに倒す。codex-review）。"""
    c = parse_confirm(text)
    if not c:
        return []
    spans = list(QUOTED.finditer(c["question"]))
    names = [next(g for g in m.groups() if g is not None) for m in spans]
    # 前後に空白のある名前は、名前の中の引用符で切れた一部とみなす（"My "draft" page" の "My " と " page"）
    if sum(c["question"].count(ch) for ch in '"“”「」『』') != 2 * len(spans) or any(n != n.strip() for n in names):
        return []
    return [n for n in names if n.strip()]


def read_ask(text: str) -> dict:
    """止まっている画面の問い（ハブの経路）。番号の無い「Yes / No」の確認を先に見る（その上に前の会話の番号つきの並びが残っていても、
    確認として読む）。確認なら {"kind": "confirm", "options": {}, "question": 問いの文}、ほかは parse_prompt と同じ。"""
    c = parse_confirm(text)
    return {"kind": "confirm", "options": {}, "question": c["question"]} if c else parse_prompt(text)


TRUST_PROMPT = re.compile(r"trust (this|the files in this) folder|Do you trust", re.I)


def visible_screen(pane: str) -> str | None:
    """その pane にいま映っている画面。読めなかったら None（「何も出ていない」と区別する）。"""
    try:
        r = subprocess.run(["herdr", "pane", "read", pane, "--source", "visible", "--lines", "45"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 and r.stdout.strip() else None


def pane_shows_trust(pane: str) -> bool | None:
    """その pane が、Claude Code の「このフォルダを信頼しますか」で止まっているか。新しいフォルダで初めて起動したときだけ出る。
    画面を読めなかったら None。"""
    screen = visible_screen(pane)
    return None if screen is None else bool(TRUST_PROMPT.search(screen))


def list_agents() -> list[dict]:
    """herdr のセッションの一覧。ハブ（Hermes）は herdr の外なので、ここには入らない（TALKERS）。"""
    return list_agents_checked()[0]


def list_agents_checked() -> tuple[list[dict], bool]:
    """セッションの一覧と、その呼び出しで一覧を取れたか（herdr に届かない・形が違うと空の一覧になる。決まった問いの答えが「無い」と言い切らないように。
    取れたかは呼び出しごとに返す。全体で 1 つ持つと、ほかの呼び出しの結果で上書きされる。a2a 8f05ef33）。"""
    r = herdr("agent", "list")
    ok = isinstance(r.get("result"), dict) and isinstance(r["result"].get("agents"), list)
    agents = r.get("result", {}).get("agents", []) if ok else []
    return [
        {
            "pane": a["pane_id"],
            "title": a.get("terminal_title_stripped") or a.get("terminal_title", ""),
            "cwd": a.get("cwd", ""),
            "kind": a.get("agent"),
            "session": (a.get("agent_session") or {}).get("value"),
            "status": a.get("agent_status"),
            "name": a.get("name") or "",
            "hub": False,  # 画面と本体の形をそろえるための印。ハブは Hermes の項目だけ（hermes_agent）
        }
        for a in agents
    ], ok


def agent_key(a: dict) -> str:
    """画面が「同じ相手か」を見分ける鍵（ハブの Hermes は、自分の項目で "hub"）。"""
    return a.get("session") or a["pane"]


TALKERS = {hermes.TALKER.pane: hermes.TALKER}  # herdr の外の話し相手
HUB = hermes.TALKER  # ハブの頭は Hermes（0.22.0 で Claude Code のハブを外した）。約束は scripts/hub_brain.py


def ensure_hub() -> dict:
    """ハブを使える状態にする。"""
    return HUB.ensure()


async def boot_ensure_hub():
    """サーバー起動時にハブを確かめる。"""
    await HUB.boot()


def jsonl_path(agent: dict) -> Path | None:
    """Claude Code のセッション記録の場所。記録は最初の発言で作られるので、まだ無いこともある（呼び出し側で exists を見る）。"""
    if agent.get("kind") != "claude" or not agent.get("session"):
        return None
    enc = re.sub(r"[^A-Za-z0-9]", "-", agent["cwd"])
    p = PROJECTS / enc / f"{agent['session']}.jsonl"
    if p.exists():
        return p
    hits = list(PROJECTS.glob(f"*/{agent['session']}.jsonl"))
    return hits[0] if hits else p


# ユーザー発話として記録されるが、本人の発言ではないもの（ローカルコマンドの出力など）
NOISE_PREFIXES = ("<command-name>", "<command-message>", "<local-command", "<system-reminder>", "<task-notification>", "<bash-input>", "<bash-stdout>", "[Request interrupted")


def iter_events(lines):
    """セッション記録(JSONL)の行から ("user" | "assistant" | "tool", text) を順に返す。"""
    for line in lines:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("isSidechain") or d.get("isMeta") or d.get("isCompactSummary"):
            continue
        content = (d.get("message") or {}).get("content")
        if d.get("type") == "user":
            if isinstance(content, str):
                m = PASTED.match(content)
                parts = [m.group(1) if m else content]
            else:
                blocks = content or []
                if any(b.get("type") == "tool_result" for b in blocks):
                    yield ("tool", "")
                    continue
                parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
                if any(b.get("type") == "image" for b in blocks):
                    parts.append("[画像]")
            text = "\n".join(p.strip() for p in parts if p.strip() and not p.strip().startswith(NOISE_PREFIXES))
            if text:
                yield ("user", text)
        elif d.get("type") == "assistant":
            for b in content or []:
                if b.get("type") == "text" and b.get("text", "").strip():
                    yield ("assistant", b["text"].strip())
                elif b.get("type") == "tool_use":
                    yield ("tool", "")


def read_lines(path: Path, offset: int = 0, tail_bytes: int | None = None) -> list[str]:
    with path.open("rb") as f:
        size = path.stat().st_size
        if tail_bytes is not None and size > tail_bytes:
            f.seek(size - tail_bytes)
            f.readline()  # 途中から始まる行を捨てる
        else:
            f.seek(offset)
        return f.read().decode("utf-8", errors="replace").splitlines()


def history(path: Path, limit: int = 40, tail_bytes: int = 4_000_000) -> list[dict]:
    """画面に出す履歴。1ターン内の assistant の text はひとつの吹き出しにまとめる。"""
    msgs: list[dict] = []
    for kind, text in iter_events(read_lines(path, tail_bytes=tail_bytes)):
        if kind == "tool":
            continue
        if kind == "assistant" and msgs and msgs[-1]["role"] == "ai":
            msgs[-1]["text"] += "\n\n" + text
        elif kind == "user" and text.startswith(NOTE_PREFIX):
            msgs.append({"role": "note", "text": text[len(NOTE_PREFIX):].split("‖", 1)[0].strip()})
        else:
            msgs.append({"role": "me" if kind == "user" else "ai", "text": text})
    return msgs[-limit:]


def last_exchange(lines) -> tuple[str, str]:
    """最後のターンの (ユーザーの発言, 返答)。返答は最後のツール呼び出しより後の text（=結論部）。
    結論部が無ければ（作業の途中など）、そのターンの text 全部。"""
    user = ""
    turn: list[str] = []
    final: list[str] = []
    for kind, text in iter_events(lines):
        if kind == "user":
            user, turn, final = text, [], []
        elif kind == "tool":
            final = []
        else:
            turn.append(text)
            final.append(text)
    return user, "\n\n".join(final or turn)


def last_turn_id(path: Path) -> str | None:
    """最後のユーザー発言の ID。「どのターンか」を見分ける目印（文面が同じでも、別のターンなら別の値になる）。"""
    lines = read_lines(path, tail_bytes=600_000)
    for i in range(len(lines) - 1, -1, -1):
        if any(kind == "user" for kind, _ in iter_events([lines[i]])):
            return json.loads(lines[i]).get("uuid") or f"line-{i}"
    return None


def reply_since(path: Path, offset: int) -> str:
    """読み上げる本文。offset 以降の最後のターンの返答。"""
    return last_exchange(read_lines(path, offset=offset))[1]


# ---------- 音声 ----------
def audio_name(filename) -> str:
    """whisper に渡すファイル名。送られてきた名前は使わず、拡張子だけを見て本体が付ける（名前に " や改行があると、multipart の見出しが崩れるため）。"""
    ext = str(filename or "").rsplit(".", 1)[-1].strip().lower() if "." in str(filename or "") else ""
    return f"audio.{ext if ext in AUDIO_EXTS else 'webm'}"


def _whisper(audio: bytes, filename: str, timeout: float = 120, vad: bool | None = None) -> dict:
    boundary = "----vl" + os.urandom(8).hex()
    lang = f"--{boundary}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\n{WHISPER_LANG}\r\n" if WHISPER_LANG else ""
    vad = WHISPER_VAD if vad is None else vad
    vad_part = f"--{boundary}\r\nContent-Disposition: form-data; name=\"vad\"\r\n\r\ntrue\r\n" if vad else ""
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper\r\n{lang}{vad_part}"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{audio_name(filename)}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + audio + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(WHISPER, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode(), strict=False)


def _text_of(reply) -> str:
    """whisper の返事の文。形が違えば（dict でない・text が無い・文が文字でない）ValueError にそろえる（受け口が「聞き取りに失敗しました」と返す。
    {"error": …} のような返事を、空の聞き取りとして通さない）。"""
    if not isinstance(reply, dict) or not isinstance(reply.get("text"), str):
        raise ValueError("whisper の返事の形が違います")
    return reply["text"]


def transcribe(audio: bytes, filename: str) -> str:
    text = _text_of(_whisper(audio, filename))
    text = re.sub(r"\s+", " ", text).strip()
    if text.replace("。", "").replace(" ", "") in HALLUCINATIONS:
        return ""
    return text


# ---------- 聞き取りの表記の直し ----------
# whisper に作った名前（呼びかけの名前など）を教えると、ほかの語が崩れる（2026-11-23 に合成音声で実測。語彙の並びが文に漏れる・名前がほかの語に入り込む）。
# 音はほぼ合っていて、ずれるのは表記（「ヴィカ」が「ウィカ」「Wica」）なので、聞き取ったあとの文字を、設定フォルダの辞書で直す。
# 直すのは、声で入った文の、画面とハブ・セッションに渡す文だけ。承認に答える・閉じるの守り（ハブの発言の台帳）には、直す前の聞き取ったままの言葉を
# 載せる（辞書の行が「はい」・番号・選択肢の文言・プロジェクト名を作り出しても、守りは通らない。hub_hermes.hermes_turn_begin の said）
HEARD_FILE = CONFIG_DIR / "heard.txt"
HEARD_MAX_LINES = 200  # 読む行の上限
HEARD_MAX_WORD = 40  # 1 つの語の字数の上限（これより長い行は読まない）
# 行頭に ^ を付けた語は、文の頭の呼びかけだけで直す（ふだんの言葉と重なる形。「ピッカ」）。前に来てよいのは、呼びかけの前置きだけ
HEARD_HEAD = r"^(?:(?:ねえ|ねぇ|ねー|ね|えっと|え|あの|おい|ほら)[、,\s]*)?"
HEARD_AFTER = r"(?=[、,。.!！?？\s]|$)"


def heard_rules() -> tuple[dict, dict]:
    """辞書（heard.txt の「聞き取りの表記=直す表記」）。どこでも直す語と、文の頭の呼びかけだけで直す語（行頭の ^）。同じ語は後の行が勝つ。
    発言のたびに読む（書き足しは次の発言から効く）。ファイルが無い・読めない・形の崩れた行は無視する（いまと同じ動き）。"""
    anywhere, head = {}, {}
    try:
        lines = HEARD_FILE.read_text(encoding="utf-8").splitlines()[:HEARD_MAX_LINES]
    except (OSError, UnicodeDecodeError):
        return anywhere, head
    for line in lines:
        line = re.split(r"\s#", line, maxsplit=1)[0].strip()  # 行の途中の「 #」からは書き込み（説明）
        if not line or line.startswith("#"):
            continue
        src, sep, dst = line.partition("=")
        src, dst = src.strip(), dst.strip()
        at_head = src.startswith("^")
        src = src[1:].strip() if at_head else src
        if not sep or not src or not dst or src == dst or len(src) > HEARD_MAX_WORD or len(dst) > HEARD_MAX_WORD:
            continue
        rules = head if at_head else anywhere
        for k in [k for k in rules if re.fullmatch(re.escape(k), src, re.I)]:  # 大文字・小文字だけ違う語（照合では同じ語）も、後の行が勝つ
            del rules[k]
        rules[src] = dst
    return anywhere, head


def _heard_rx(rules: dict) -> tuple[str, list[str]]:
    """辞書の語の並び（長い語から先に当てる）と、並びの番号ごとの直す表記。語ごとに名前付きの組（r0, r1, …）にして、当たった組から直す表記を引く
    （大文字・小文字の扱いを正規表現に任せ、引き当てで別の折り畳みをしない）。英字で始まる・終わる語は、英単語の一部では当てない。"""
    def one(w: str) -> str:
        return ("(?<![A-Za-z])" if w[0].isascii() and w[0].isalpha() else "") + re.escape(w) + ("(?![A-Za-z])" if w[-1].isascii() and w[-1].isalpha() else "")
    words = sorted(rules, key=len, reverse=True)
    return "|".join(f"(?P<r{i}>{one(w)})" for i, w in enumerate(words)), [rules[w] for w in words]


def fix_heard(text: str) -> str:
    """声で入った文の表記を、辞書で直す（画面とハブ・セッションに渡す文。守りの台帳には直す前の言葉を載せる）。"""
    anywhere, head = heard_rules()
    if not (anywhere or head):
        return text
    done = []

    def put(m: re.Match, dsts: list[str]) -> str:
        i = int(m.lastgroup[1:])
        done.append(f"{m.group(m.lastgroup)}→{dsts[i]}")
        return dsts[i]

    # 直した語は、もう一度は直さない（文の頭で直した語に、どこでも直す語を重ねて当てない）
    segs = [(text, True)]
    if head:
        rx, dsts = _heard_rx(head)
        m = re.match(f"{HEARD_HEAD}(?:{rx}){HEARD_AFTER}", text, flags=re.I)
        if m:
            g = m.lastgroup
            segs = [(text[:m.start(g)], True), (put(m, dsts), False), (text[m.end(g):], True)]
    if anywhere:
        rx, dsts = _heard_rx(anywhere)
        any_rx = re.compile(rx, re.I)
        segs = [(any_rx.sub(lambda m: put(m, dsts), s) if open_ else s, open_) for s, open_ in segs]
    fixed = "".join(s for s, _ in segs)
    if fixed != text:
        print(f"[heard] 直した: {'、'.join(done)}", flush=True)
    return fixed


def synth(text: str, speaker: int) -> bytes:
    text = READING_RE.sub(lambda m: READINGS[m.group(1).lower()], text)
    q = urllib.request.urlopen(
        urllib.request.Request(f"{TTS_URL}/audio_query?" + urllib.parse.urlencode({"text": text, "speaker": speaker}), method="POST"),
        timeout=60,
    ).read()
    return urllib.request.urlopen(
        urllib.request.Request(f"{TTS_URL}/synthesis?speaker={speaker}", data=q, headers={"Content-Type": "application/json"}, method="POST"),
        timeout=120,
    ).read()


# 機械（スタックチャン）に送る返答の声。hello で audio: "opus" を名乗った接続には、文ごとの WAV を Ogg の Opus に縮めて送る
# （SIM の細い回線のため。24 kbps で聞き取りは WAV と同じ、2026-11-21 の実測）。縮められないときは WAV のまま送る（機械は頭の 4 バイトで見分ける）
OPUS_KBPS = max(8, min(64, int(os.environ.get("VL_OPUS_KBPS", "24") or 24)))


def ffmpeg_path() -> str | None:
    """縮めるのに使う ffmpeg。VL_FFMPEG か、PATH か、Homebrew の場所。launchd の PATH には Homebrew が無いことがある。"""
    for p in (os.environ.get("VL_FFMPEG", ""), shutil.which("ffmpeg") or "", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def to_opus(wav: bytes) -> bytes | None:
    """WAV を Ogg の Opus（会話向け、60 ms ごとの区切り）にする。できなければ None。"""
    exe = ffmpeg_path()
    if not exe:
        return None
    try:
        p = subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-c:a", "libopus", "-b:a", f"{OPUS_KBPS}k",
                            "-application", "voip", "-frame_duration", "60", "-f", "ogg", "pipe:1"], input=wav, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[voice] Opus に縮められませんでした: {e}", flush=True)
        return None
    if p.returncode != 0 or not p.stdout.startswith(b"OggS"):
        print(f"[voice] Opus に縮められませんでした: {p.stderr.decode(errors='ignore')[:200]}", flush=True)
        return None
    return p.stdout


def narrator_model() -> str:
    """いま使う語り手。narrator_model ファイルがあればその1行、無ければ既定。"""
    try:
        name = NARRATOR_FILE.read_text().strip()
    except OSError:
        name = ""
    return name or NARRATOR_MODEL


def _saved_gateway_token() -> tuple[str, float]:
    """保存済みの OIDC トークンと、切れるまでの秒数。無ければ ("", 0)。"""
    try:
        token = re.search(r'VERCEL_OIDC_TOKEN="?([^"\n]+)', (GATEWAY_DIR / ".env.local").read_text()).group(1)
        payload = token.split(".")[1]
        exp = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]
        return token, exp - time.time()
    except Exception:
        return "", 0.0


_gateway_lock = asyncio.Lock()
_gateway_clients: dict[str, openai.AsyncOpenAI] = {}


async def gateway_token(min_left: float = 600) -> str:
    """AI Gateway に渡すトークン。残りが少なければ Vercel CLI で取り直す（数秒かかる）。"""
    token, left = _saved_gateway_token()
    if left > min_left:
        return token
    async with _gateway_lock:
        token, left = _saved_gateway_token()
        if left > min_left:
            return token
        r = await asyncio.to_thread(subprocess.run, GATEWAY_PULL, cwd=GATEWAY_DIR, capture_output=True, text=True, timeout=90)
        token, left = _saved_gateway_token()
        if left <= 0:
            raise RuntimeError(f"OIDC トークンを取り直せませんでした: {(r.stderr or r.stdout).strip()[-200:]}")
        print(f"[narrator] OIDC トークンを取り直しました（残り {left / 3600:.1f} 時間）", flush=True)
        return token


async def keep_gateway_token():
    """語り手が Gateway 経由のあいだ、トークンが切れる前に取り直しておく（返事の途中で数秒待たせないため）。"""
    while True:
        try:
            if "/" in narrator_model():
                await gateway_token(min_left=3600)
        except Exception as e:
            print(f"[narrator] {e}", flush=True)
        await asyncio.sleep(600)


async def _narrate_stream(model: str, text: str):
    """語り手の出力を、届いた順に少しずつ流す。"""
    user = f"<source>\n{text}\n</source>"  # 原稿であって話しかけではない、と区別できるように包む
    if "/" in model:
        token = await gateway_token()
        if token not in _gateway_clients:  # 接続を使い回す。トークンが替わったら作り直す
            _gateway_clients.clear()
            _gateway_clients[token] = openai.AsyncOpenAI(base_url=GATEWAY_URL, api_key=token)
        # OpenAI のモデルは既定だと考えてから訳すので、最初の文が 3〜4秒遅れる（2026-10-12 実測）。思考は切る
        extra = {"reasoning_effort": "none"} if model.startswith("openai/") else {}
        stream = await _gateway_clients[token].chat.completions.create(
            model=model, stream=True, **extra,
            messages=[{"role": "system", "content": NARRATOR_PROMPT}, {"role": "user", "content": user}])
        async with stream:
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
    else:
        async with narrator_client.messages.stream(
            model=model, max_tokens=4000,
            system=[{"type": "text", "text": NARRATOR_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        ) as stream:
            async for delta in stream.text_stream:
                yield delta


async def narrate(text: str, cancel: asyncio.Event):
    """語り手で話し言葉に変換し、文ができるたびに yield する（ストリーム）。"""
    model = narrator_model()
    t0 = time.time(); first = None; buf = ""
    stream = _narrate_stream(model, text)
    try:
        while not cancel.is_set():
            try:
                delta = await anext(stream)
            except StopAsyncIteration:
                break
            except Exception as e:
                if first is not None or model == NARRATOR_MODEL:
                    raise
                # 切り替え先が使えない（トークン切れ・モデル名の誤りなど）。声を止めないよう、既定の語り手で訳し直す
                print(f"[narrator] {model} が使えないので {NARRATOR_MODEL} で訳します: {str(e)[:200]}", flush=True)
                await stream.aclose()
                model = NARRATOR_MODEL
                stream = _narrate_stream(model, text)
                continue
            if first is None:
                first = time.time() - t0
                print(f"[narrator] {model} 最初の出力まで {first:.2f}秒（原稿 {len(text)}字）", flush=True)
            buf += delta
            parts = SENTENCE_END.split(buf)
            for s in parts[:-1]:
                if s.strip():
                    yield s.strip()
            buf = parts[-1]
    finally:
        await stream.aclose()
    if buf.strip() and not cancel.is_set():
        yield buf.strip()


# ---------- HTTP / WS ----------
# 画面（HTML）の CSP。画面が読み込むものは、この本体からのもの（画像は data:・blob: も。声は blob: で鳴らす）だけにする。
# 返事の Markdown や HTML に外のサイトの画像・CSS（@import・@font-face・url()）などが混じっても、ブラウザが取りに行かない
# （乗せられたハブが、URL に情報を載せて外へ出す道を塞ぐ。返事の style は画面でも落とす）。読み込みの種類ごとに書く。
# script は絞らない: 返事から script を動かさない守りは DOMPurify が受け持ち、画面の script は index.html の中にある。
# 画面は、ほかのページの枠（iframe など）に入れさせない（frame-ancestors。CSP を見ないブラウザ向けに X-Frame-Options: DENY も付ける）
INDEX_CSP = ("img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; font-src 'self' data:; connect-src 'self'; "
             "frame-src 'none'; frame-ancestors 'none'; object-src 'none'; worker-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'self'")
PAGE_HEADERS = {"Content-Security-Policy": INDEX_CSP, "X-Frame-Options": "DENY"}  # 画面（HTML）の応答に付ける 2 つ


class PageStatic(StaticFiles):
    """/static の配信。HTML のファイル（index.html）を返すときは、200 のほか、再検証の 304 と範囲の 206 にも PAGE_HEADERS を付ける。
    304 と複数範囲の 206 は text/html を名乗らない（下の PageCSP では拾えない）が、ブラウザは 304 のヘッダーで手元の写しのヘッダーを
    書き換える（付けないと、前の版で取っておいた写しのヘッダーのまま使われる）。どのファイルかは、URL の書き方（末尾の / など）ではなく、
    解決したあとのファイルで見る。"""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        res = super().file_response(full_path, stat_result, scope, status_code)
        if str(full_path).lower().endswith((".html", ".htm")):
            res.headers.update(PAGE_HEADERS)
        return res


app.mount("/static", PageStatic(directory=HERE / "static"), name="static")


class PageCSP:
    """ASGI のミドルウェア。HTML を返すすべての応答に INDEX_CSP と X-Frame-Options: DENY を付ける（/ のほかに /static/index.html でも同じ画面が開けるため）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_csp(m):
            if m["type"] == "http.response.start":
                h = list(m.get("headers", []))
                names = {k.lower() for k, _ in h}
                if any(k.lower() == b"content-type" and v.lower().startswith(b"text/html") for k, v in h):
                    if b"content-security-policy" not in names:
                        h.append((b"content-security-policy", INDEX_CSP.encode()))
                    if b"x-frame-options" not in names:
                        h.append((b"x-frame-options", b"DENY"))
                    m = {**m, "headers": h}
            await send(m)

        await self.app(scope, receive, send_csp)


app.add_middleware(PageCSP)


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html", headers={"Cache-Control": "no-cache", **PAGE_HEADERS})


@app.get("/sw.js")
def service_worker():
    """画面の部品を手元に取っておく仕組み。画面全体を受け持たせるため、/static ではなくここから配る。"""
    return FileResponse(HERE / "static" / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


def hub_turns(n: int = 1) -> list[tuple[str, str]]:
    """ユーザー本人の発言の (ID, 文) を新しいものから n 件。"""
    return HUB.turns(n)


def hub_via(turn: str | None) -> str:
    """発言 turn の来た所。機械（スタックチャン）からなら "device"。発言が無ければ空。"""
    return HUB.via(turn) if turn else ""


def hub_current(turn: str | None) -> bool:
    """発言 turn を受け付けたあとに、利用者が止めていない・次の発言をしていないか（承認に答える・閉じるの直前に見る）。発言が無ければ False。"""
    return HUB.current(turn) if turn else False


def admit_now(seen: int | None = None) -> int | None:
    """利用者の発言を受けた（相手がハブでなくても）。ハブが使えるなら受け付けの番号を進めて返す（それより前のハブの往復の許可と、
    まだ画面に届いていない返事・報告を失効させる）。seen は、画面が発言を始めたときに受け取っていた返事の番号（Talker.admit）。"""
    if not HUB.on():
        return None
    with COMMIT_LOCK:  # 受け付けの番号を進めるのも、操作の送り始め（確かめてから起動するまで）と直列にする（codex-review f0b77a7 の P1）
        return HUB.admit(seen) if seen is not None else HUB.admit()


async def admit_async(seen: int | None = None) -> int | None:
    """admit_now と同じ。鍵の空きを、イベントループを止めずに待つ（herdr の起動待ちで鍵が使われていても、ほかの画面の相手を続ける。codex-review 711ce71）"""
    if not HUB.on():
        return None
    async with invalidating("admit", _admit_order()):  # 読んだ順に確定する（別々の画面の受け付けで、先に読んだ発言に新しい番号を付けない）
        return HUB.admit(seen) if seen is not None else HUB.admit()


def heard_seen(d: dict) -> int:
    """画面の発言に付いた「発言を始めたとき（声は録音を始めたとき、文字は送るを押したとき）に受け取っていた返事の番号」。
    付いていない（更新前の画面・機械）・形が違えば -1（どれとも合わない。聞いた文を引き渡さないので、その発言では承認に答えない・閉じない。
    発言そのものは届き、ハブが聞き直す。a2a 3752b7a5 の 5 回目）。"""
    v = d.get("hid")
    return v if isinstance(v, int) and not isinstance(v, bool) else -1


# 同期の合図（画面がすぐ返す）を返してもらうまでの発言（受け付けの番号 → [返ってきた・切れたら立つ印, 返ってきたか]）。同じ接続の順で、
# 返事より前に画面が送ったもの（声の聞き取りの間に押した停止など）は、返事を読むまでに本体が読み終えている。承認に答える・閉じるは、使う発言が
# 決まってから、送る直前の確かめの前に、いまの発言の返事を待つ（聞き取りのあと受信がまた待つ間に、停止を読む前に答えを送らない。codex-review
# 62a9436・5fabc67 の P1）。返事の前に切れた発言は、送られた停止が届かずに消えたかもしれないので、答え・閉じるに使わない
SETTLING: dict[int, list] = {}
SETTLE_LAST = {"n": -1}  # 本体が仕事にした発言のうち、いちばん新しい受け付けの番号
SETTLE_LIMIT = 10.0  # 返事を待つ上限（秒）。返らなければ断る（止めたものとして扱う）


async def hub_settled() -> bool:
    """いまの発言の画面が、同期の合図を返したか（返すまで待つ。上限を超えたら False）。合図を送っていない発言（機械・更新前の画面）は待たない。
    承認に答える・閉じるは、相手と問いを読む前にこれで待ち（待つ間に相手・問いが入れ替わらないように）、送る直前の確かめでは settled_now で
    待たずに見る（途中で次の発言が入り、その画面がまだ返していなければ断る）。codex-review 2eccfb5 の P1"""
    v = SETTLING.get(SETTLE_LAST["n"])
    if v is None:
        return True
    return await asyncio.to_thread(v[0].wait, SETTLE_LIMIT) and v[1] is True


def settled_now() -> bool:
    """いまの発言の画面が、同期の合図をもう返したか（待たない）。"""
    v = SETTLING.get(SETTLE_LAST["n"])
    return v is None or (v[0].is_set() and v[1] is True)


def _if_still(still, fn, *args):
    """ワーカーのスレッドの中で、操作を始める直前にもう一度確かめてから fn を呼ぶ（スレッドの空きを待つ間に止めた・次の発言をしたら、
    呼ばずに None を返す。codex-review bcd26ac）。still が None なら確かめない。"""
    base = fn.func if isinstance(fn, functools.partial) else fn
    if still is not None and base is _HERDR:  # 本物の herdr: 確かめと起動を、停止の確定と同じ鍵の中で行う
        timeout = fn.keywords.get("timeout", 60) if isinstance(fn, functools.partial) else 60
        # 返事を待たない操作（承認のキー・閉じる・指示を送る）は、返るまで送り途中の印を持つ（停止は、返るのを待ってから「止めました」と返す）。
        # 鍵が使われていたら、承認のキー・閉じるは待たずに断り、指示を送るのは待つ
        return _herdr_gated(still, args, timeout, hold=True, wait_lock=args[:2] == ("agent", "prompt"))
    with COMMIT_LOCK:
        ok = still is None or still()
    if not ok:
        return None
    return fn(*args)


def hub_heard(turn: str | None) -> str:
    """利用者が発言 turn の直前に聞いたハブの返答（本体が受け取った原文）。分からなければ空（守りは断る側に倒れる）。"""
    return HUB.heard(turn) if turn else ""


def hub_reply_to(turn: str | None) -> str:
    """発言 turn へのハブの返答（ユーザーが聞いた文）。"""
    return HUB.reply_to(turn) if turn else ""


def hub_replying(turn: str | None) -> str | None:
    """発言 turn が、画面の「返信」で報告を指したものなら、その報告のセッション（分からなければ空）。返信でなければ None。"""
    return HUB.replying(turn) if turn else None


def hub_reply_ask(turn: str | None) -> dict | None:
    """発言 turn が、画面の「返信」で報告を指したものなら、その報告が見ていた問い {"pane", "fp", "session"}（分からない欄は空）。返信でなければ None。"""
    return HUB.reply_ask(turn) if turn else None


def agent_updated(a: dict) -> float | None:
    """そのセッションが最後に動いた時刻（会話の記録が最後に書き込まれた時刻、UNIX 秒）。記録の無いセッションは None。画面の「更新順」に使う。"""
    path = jsonl_path(a)
    try:
        return path.stat().st_mtime if path else None
    except OSError:
        return None


# ---------- 添付（画面から話し相手に渡すファイル。部品は scripts/attach.py） ----------
ATTACH_DIR = STATE_DIR / "attachments"
ATTACHED: dict[str, dict] = {}  # 画面に返した添付の id → {path, file, name, at}。発言に付いた id だけを、この中から引く（場所は画面から受け取らない）
ATTACH_ID_KEEP = 86400


@app.post("/api/attach")
async def api_attach(request: Request, name: str = "", pane: str = ""):
    """画面から添付するファイルを 1 つ受け取る（本文がそのままファイルの中身）。名前は拡張子と表示にだけ使い、置く名前は本体が付ける。
    受け取ったら id を返す。発言（text・turn）の attachments にその id を並べると、本体が場所を発言の末尾に書いて渡す。
    pane は、置くときの宛先（話し相手）。その id は、同じ宛先への発言にしか付けられない（セッション宛ての添付を Hermes に渡さない）。"""
    if not pane or len(pane) > 64:
        return JSONResponse({"ok": False, "reason": "pane", "why": "宛先がありません"}, status_code=400)
    ext = attach.ext_of(name)
    if ext not in attach.TYPES:
        return JSONResponse({"ok": False, "reason": "type", "why": "画像（png・jpg・gif・webp・heic）・PDF・テキスト（txt・md・csv）だけ添付できます"}, status_code=400)
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > attach.MAX:
            return JSONResponse({"ok": False, "reason": "too_big", "why": f"1 つ {attach.MAX // (1024 * 1024)}MB までです"}, status_code=413)
    if not data:
        return JSONResponse({"ok": False, "reason": "empty", "why": "中身がありません"}, status_code=400)
    why = attach.content_problem(ext, bytes(data))
    if why:
        return JSONResponse({"ok": False, "reason": "content", "why": why}, status_code=400)
    file = attach.new_name(ext)
    try:
        path = await asyncio.to_thread(attach.save, ATTACH_DIR, file, bytes(data))
    except (OSError, ValueError) as e:
        traceback.print_exc()
        return JSONResponse({"ok": False, "reason": "state", "why": f"置けませんでした（{type(e).__name__}）"}, status_code=500)
    aid = secrets.token_urlsafe(12)
    now = time.time()
    for k in [k for k, v in ATTACHED.items() if now - v["at"] > ATTACH_ID_KEEP]:
        ATTACHED.pop(k, None)
    ATTACHED[aid] = {"path": path, "file": file, "name": attach.shown_name(name), "at": now, "pane": pane}
    try:  # 片づけの失敗で、置けた添付を断らない
        await asyncio.to_thread(attach.tidy, ATTACH_DIR)
    except OSError:
        traceback.print_exc()
    hlog(f"添付を受け取りました: {file}（{len(data)} バイト）", flush=True)
    return JSONResponse({"ok": True, "id": aid, "name": ATTACHED[aid]["name"], "size": len(data)})


def attachments_of(d: dict) -> list[str] | None:
    """発言に付いた添付の id。付いていなければ []。形が違う・多すぎるなら None（送らずに断る）。"""
    v = d.get("attachments")
    if v is None:
        return []
    if not isinstance(v, list) or len(v) > attach.COUNT or not all(isinstance(x, str) and len(x) <= 64 for x in v):
        return None
    return v


async def attachment_lines(ids: list[str], talker, pane: str = "") -> tuple[str, str]:
    """発言の末尾に足す「添付（画面から）」の行と、断る理由。セッションには状態フォルダの場所、herdr の外の話し相手（ハブ）には、
    その相手が置いた場所（Hermes ならコンテナの中）を書く。"""
    if not ids:
        return "", ""
    items = [ATTACHED.get(i) for i in ids]
    if any(it is None for it in items):
        return "", "添付が見つかりません（本体を起動し直したか、時間がたちました）。送っていません。添付を選び直してください"
    if any(it["pane"] != pane for it in items):
        return "", "添付を置いたときと、送る相手が違います（途中で相手を替えましたか）。送っていません。添付を選び直してください"
    lines = []
    for it in items:
        if talker is None:
            where = str(it["path"])
        else:
            try:
                data = await asyncio.to_thread(Path(it["path"]).read_bytes)
                where = await asyncio.to_thread(talker.put_attachment, it["file"], data)
            except (OSError, RuntimeError) as e:
                return "", f"添付を渡せませんでした（{_clip(str(e), 120)}）。送っていません"
        lines.append(f"- {where}（{it['name']}）")
    return "\n\n添付（画面から）:\n" + "\n".join(lines), ""


def attachments_shape_problem(files: list[str] | None) -> str:
    """発言の attachments の形が違う（attachments_of が None を返した）なら、断る理由。"""
    return f"添付の形が違うか、多すぎます（1 回に {attach.COUNT} つまで）。送っていません" if files is None else ""


ACCEPT_LOCKS: dict[str, asyncio.Lock] = {}  # ハブ（herdr の外の話し相手）が受け取った発言の順番（添付を置く間も、前の発言を追い越させない）


async def hub_accepted(talker, text: str, pane: str, reply_at: float | None, files: list[str] | None, item: int | None = None,
                       said: str | None = None, via: str = "", admitted: int | None = None) -> tuple[str, str, bool]:
    """ハブが受け取った発言を、添付を置く → 往復する（置けなければ断る）まで進める。(返事, 失敗・断りの理由, 記録に書けたか)。
    受け取った順に 1 つずつ（ACCEPT_LOCKS）。往復と断りの記録は、さらに頭のやり取りの鍵（自動通知と共有）の順に載る。
    断るときは、ハブに、利用者が最後に聞いたのは本体の断りだと伝える（前の問いへの答えに、次の発言を使わせない）。"""
    async with ACCEPT_LOCKS.setdefault(pane, asyncio.Lock()):
        why = attachments_shape_problem(files)
        extra = ""
        if not why:
            extra, why = await attachment_lines(files, talker, pane)
        kw = {"said": said} if said is not None else {}  # 辞書で直した声の発言だけ、直す前の言葉を守りの台帳に渡す
        if why:
            # 断った発言も、機械からなら機械の印で台帳に載せる。番号は受け付けたその場のもの（順番待ちの間に止めたら、その発言は
            # もういまの発言ではない。止めたあとに断る答え（Esc）も送らせない。a2a 3752b7a5）
            await talker.refused(text, why, **kw, **({"via": via} if via else {}), **({"admitted": admitted} if admitted is not None else {}))
            return "", why, True
        # via は機械からの発言のときだけ渡す（画面からの往復は、これまでの頭の約束の形のまま）
        # admitted: 受け付けたその場の番号（添付の用意・順番待ちの間に止めた・話しかけたなら、返事を「利用者が聞いた文」にしない。a2a 30fd5b94）
        return await talker.exchange(text, reply_at, extra, item, **kw, **({"via": via} if via else {}), **({"admitted": admitted} if admitted is not None else {}))


def reply_item_of(d: dict) -> int | None:
    """画面の受け箱（💡）で押した件への返信の先（頭の記録の番号）。付いていなければ None。付いているのに正の整数でなければ -1
    （どの記録とも一致しないので、頭は送らずに止める。ふつうの発言にすり替えない）。"""
    if "reply_item" not in d:
        return None
    v = d["reply_item"]  # 付いているのに空（null）なら -1（画面で扱えない番号が null になっても、ふつうの発言にすり替えない）
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 < v < 2**31 else -1


def reply_at_of(d: dict) -> float | None:
    """画面の発言に付いた「返信」の先（報告の記録の時刻）。名前は受け取らない（頭が記録から引く）。
    付いていない（キーが無い・null）なら None（ふつうの発言）。付いているのに有限の数でない（真偽・文字・NaN・無限）なら NaN
    （どの報告とも一致しないので、頭は「返信先が見つからない」として送らずに止める。ふつうの発言にすり替えない）。"""
    v = d.get("reply_at")
    if v is None:
        return None
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else math.nan


@app.get("/api/agents")
def api_agents():
    out = [{**a, "key": agent_key(a), "updated": agent_updated(a)} for a in list_agents()]
    out.extend(t.entry() for t in TALKERS.values() if t.on())
    return JSONResponse(sorted(out, key=lambda a: not a["hub"]))  # ハブを先頭に


@app.get("/api/history")
def api_history(pane: str):
    """選んだセッションの会話履歴。正本はそのセッション自身の記録(JSONL)。herdr の外の話し相手は本体が書いた記録。"""
    if pane in TALKERS:
        return JSONResponse({"source": TALKERS[pane].name, "messages": TALKERS[pane].history()})
    agent = next((a for a in list_agents() if a["pane"] == pane), None)
    path = jsonl_path(agent) if agent else None
    if not path:
        return JSONResponse({"source": "none", "messages": []})  # 記録を読めない種類のエージェント
    return JSONResponse({"source": "jsonl", "messages": history(path) if path.exists() else []})


# ---------- 成果物（画像・動画）を画面で見る ----------
# 会話に出てきたファイルを、PC の画面がサムネイルと大きく見る画面で読む口。返すのは次を全部満たすものだけ。
#   - そのセッションのフォルダの中（symlink はたどった先で見る）。ハブと Hermes の会話では、どのセッションからの場所か分からないので、
#     いまあるセッションのどれかのフォルダの中を指す絶対パスか、「フォルダ名/フォルダからの場所」（フォルダ名が一つに決まるときだけ）
#   - フォルダから先の名前に、. で始まるもの（隠しフォルダ・隠しファイル）が無い
#   - 画像と動画の拡張子（ARTIFACT_TYPES）。型は拡張子で決め、中身から推し量らせない（nosniff）。SVG・HTML は返さない
#   - フォルダがホームか / そのもののセッションからは返さない（写真などを丸ごと見せないため）
# ブラウザが別のサイトからの読み込みと知らせてきたもの（Sec-Fetch-Site が same-origin・none 以外）は断る。<img> の読み込みには Origin が
# 付かず origin_guard を通るので、別のサイトのページから、ファイルがあるか・画像の大きさを探られないように。コンテナからは origin_guard が断る
ARTIFACT_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif",
                  ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}
_artifact_agents: tuple[float, list[dict]] = (0.0, [])  # (取った時刻, セッションの一覧)。サムネイルが並ぶと続けて呼ばれるので 5 秒は覚える
# （閉じたセッションのフォルダも、最大 5 秒は返しうる。herdr を毎回呼ばないための、決めたうえでの遅れ）


def artifact_file(pane: str, raw: str) -> tuple[Path | None, str]:
    """画面に返してよいファイルの、たどった先の場所。返さないときは (None, 理由)。"""
    global _artifact_agents
    if not raw or "\x00" in raw or len(raw) > 4096:
        return None, "場所の形が読めません"
    at, agents = _artifact_agents
    if time.monotonic() - at > 5:
        agents = list_agents()
        _artifact_agents = (time.monotonic(), agents)
    me = next((a for a in agents if a["pane"] == pane), None)
    hub = pane in TALKERS
    if me is None and not hub:
        return None, "そのセッションがありません"
    home, roots, named = Path.home().resolve(), [], []  # named: (ハブが見ているフォルダ名＝cwd の末尾。hub_sessions の project と同じ, たどった先)
    for a in agents if hub else [me]:
        if not a.get("cwd"):
            continue
        try:
            r = Path(a["cwd"]).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if r != home and r != Path(r.anchor):
            roots.append(r)
            named.append((a["cwd"].rstrip("/").rsplit("/", 1)[-1], r))
    if not roots:
        return None, "見てよいフォルダがありません"
    p = Path(raw)
    if not p.is_absolute():
        if hub:  # ハブは各セッションのフォルダ名を知っているので「フォルダ名/フォルダからの場所」で書ける。同じフォルダ名が二つ以上なら決めない
            name, _, rest = raw.partition("/")
            hits = {r for n, r in named if n == name}
            if len(hits) != 1 or not rest:
                return None, f"{AGENT}の会話では、絶対パスか「フォルダ名/場所」だけを見ます（そのフォルダ名のセッションが無いか、二つ以上あります）"
            roots = list(hits)  # その名前のフォルダの中だけを見る（../ や symlink で別のセッションへ回り込ませない）
            p = roots[0] / rest.lstrip("/")
        else:
            p = roots[0] / p
    try:
        real = p.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, "ファイルがありません"
    inside = [r for r in roots if r in real.parents]
    if not inside:
        return None, "セッションのフォルダの外です"
    if any(part.startswith(".") for part in real.relative_to(max(inside, key=lambda r: len(r.parts))).parts):
        return None, "隠しフォルダ・隠しファイルは見せません"
    if real.suffix.lower() not in ARTIFACT_TYPES:
        return None, "画像と動画のほかは見せません"
    try:
        if not stat.S_ISREG(real.stat().st_mode):
            return None, "ふつうのファイルではありません"
    except OSError:
        return None, "ファイルがありません"
    return real, ""


@app.api_route("/api/file", methods=["GET", "HEAD"])
def api_file(request: Request, pane: str = "", path: str = ""):
    site = request.headers.get("sec-fetch-site", "")
    if site and site not in ("same-origin", "none"):
        print(f"[file] 断りました: Sec-Fetch-Site {site}", flush=True)
        return JSONResponse({"ok": False, "reason": "別のサイトからは読めません"}, status_code=403)
    real, why = artifact_file(pane, path)
    if real is None:
        return JSONResponse({"ok": False, "reason": why}, status_code=404)
    return FileResponse(real, media_type=ARTIFACT_TYPES[real.suffix.lower()],
                        headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; sandbox", "Cache-Control": "no-cache"})


@app.get("/api/speakers")
def api_speakers():
    data = json.loads(urllib.request.urlopen(f"{TTS_URL}/speakers", timeout=10).read())
    return JSONResponse([{"id": st["id"], "name": f"{s['name']} / {st['name']}", "default": st["id"] == DEFAULT_SPEAKER}
                         for s in data for st in s["styles"]])


# ---------- ハブが使う道具（hubctl から呼ばれる） ----------
def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + f"…（以下 {len(text) - n} 字省略）"


def _clip_ends(text: str, head: int, tail: int) -> str:
    """頭と末尾を残して中を省く。返答の末尾には「残っていること」「お願い」が書かれることが多い。"""
    if len(text) <= head + tail:
        return text
    return text[:head] + f" …（中略 {len(text) - head - tail} 字）… " + text[-tail:]


def _worker(pane: str) -> dict | None:
    return next((a for a in list_agents() if a["pane"] == pane), None)


def asked_by(pane: str, user: str, mtime: float | None) -> str | None:
    """セッションの直近の依頼が、voice-loop（ハブの send・start）から送ったものなら voice-loop、そうでなければ other
    （ユーザーが端末や 1対1 で直接頼んだもの）。頼んだ件の台帳は終わって3日で消えるので、それより古い依頼や、依頼が無いときは None（分からない）。"""
    if not user or mtime is None:
        return None
    fp = text_fp(user)
    if any(t["pane"] == pane and t.get("fp") == fp for t in TASKS.values()):
        return "voice-loop"
    return "other" if time.time() - mtime < TASK_KEEP else None


@app.get("/api/hub/sessions")
def hub_sessions():
    """ハブ以外の全セッションの状態と、最後のやり取り。最後に記録が動いた時刻と、直近の依頼がどこから来たか（asked_by）も付ける。"""
    out = []
    for a in list_agents():
        path = jsonl_path(a)
        has = bool(path and path.exists())
        user, reply = last_exchange(read_lines(path, tail_bytes=600_000)) if has else ("", "")
        mtime = path.stat().st_mtime if has else None
        out.append({
            "pane": a["pane"], "project": a["cwd"].rsplit("/", 1)[-1], "title": a["title"], "status": a["status"], "kind": a["kind"],
            "idle_minutes": round((time.time() - mtime) / 60) if mtime else None,
            "updated_at": datetime.fromtimestamp(mtime).strftime("%m/%d %H:%M") if mtime else None,
            "asked_by": asked_by(a["pane"], user, mtime),
            "last_user": _clip(user, 200), "last_reply": _clip_ends(reply, 200, 500),
        })
    return JSONResponse(out)


@app.get("/api/hub/read")
def hub_read(pane: str, n: int = 6, full: bool = False, task: str = ""):
    a = _worker(pane)
    if not a:
        return JSONResponse({"error": f"セッションが見つかりません: {pane}"}, status_code=404)
    # 1ターンの文（途中経過も）は1つにまとめるので、長く働いたターンでは結論が末尾にある。頭だけ残すと結論が落ちるので、頭と末尾を残す
    head, tail = (2000, 6000) if full else (300, 1200)
    if task:
        return task_read(a, task, head, tail)
    path = jsonl_path(a)
    msgs = history(path, limit=n) if path and path.exists() else []
    return JSONResponse({"pane": pane, "project": a["cwd"].rsplit("/", 1)[-1], "title": a["title"], "status": a["status"],
                         "messages": [{"role": m["role"], "text": _clip_ends(m["text"], head, tail)} for m in msgs]})


def trim_range(msgs: list[dict], cap: int | None = None) -> list[dict]:
    """吹き出しを cap（既定は TASK_READ_MAX）までに絞る。最初の指示は必ず残し（その前の吹き出しは、収まるときだけ）、残りは新しいほうから。省いた所に印を置く。"""
    cap = cap or TASK_READ_MAX
    if len(msgs) <= cap:
        return msgs
    def gap(n):
        return {"role": "gap", "text": f"（ここで {n} 件を略しました）"}
    i = next((i for i, m in enumerate(msgs) if m.get("this")), None)
    out, rest = [], msgs
    if i is not None:
        out = msgs[:i + 1] if i + 1 < cap else [gap(i), msgs[i]]
        rest = msgs[i + 1:]
    room = cap - sum(1 for m in out if m["role"] != "gap")
    return out + ([gap(len(rest) - room), *rest[len(rest) - room:]] if len(rest) > room else rest)


def task_read(a: dict, tid: str, head: int, tail: int) -> JSONResponse:
    """hubctl read --task: この件の範囲（最初の指示を送った位置から今まで）を、発言ごとに区切り、この件の指示に印を付けて返す。
    吹き出しが多ければ、最初の指示と新しいほうを残し、あいだを略す（trim_range）。"""
    d = TASK_RANGE.get(tid)
    if not d or d["pane"] != a["pane"] or not d.get("text"):
        return JSONResponse({"error": f"この件が見つかりません: {tid}（hubctl tasks で件とセッションを確かめてください）"}, status_code=404)
    chain = slack_chain(d)
    gone = JSONResponse({"error": "この件を送ったときのセッションの記録を読めません（セッションが作り直された・記録がまだ無い）。"
                                  f"いまのセッションの直近のやり取りは hubctl read {a['pane']} で読めます"}, status_code=409)
    if not d.get("path") or any(e.get("session") != d.get("session") for e in chain) or not same_session(d):
        return gone
    try:
        msgs, found, _mixed = task_range(Path(d["path"]), chain[0]["offset"] if chain else d["offset"], [e["text"] for e in chain] + [d["text"]])
    except OSError:
        return gone
    if not found:  # この件の指示（まとめた件は全部）が記録に無い。別の件のやり取りを「この件の範囲」として見せない
        return JSONResponse({"error": "この件の指示を、セッションの記録で見つけられません（届いていない・まだ記録に無い・一部だけ）。"
                                      f"いまのセッションの直近のやり取りは hubctl read {a['pane']} で読めます"}, status_code=409)
    msgs = trim_range(msgs)
    return JSONResponse({"pane": a["pane"], "project": a["cwd"].rsplit("/", 1)[-1], "title": a["title"], "status": a["status"], "task": tid,
                         "messages": [{**m, "text": m["text"] if m["role"] == "gap" else _clip_ends(m["text"], head, tail)} for m in msgs]})


SENT_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="sent")  # 読み取りは専用の 1 本で順に（ほかの口の to_thread を待たせない）
SENT_WAIT = 30      # 1 回の読み取りを待つ秒（越えたら断る。読み取り自体は sent_log.BUDGET で区切られて終わる）
SENT_QUEUE_MAX = 3  # 待っている読み取りがこれ以上なら、すぐ断る
SENT_PENDING = [0]
SENT_LOCK = threading.Lock()


def read_sent(since, project: str = "", limit: int = 200):
    """出ていく側の記録（sent_log.sent）を、読み取りの作業 1 本で順に読む。頭の確かめ（層 3）が、頭の約束の道具として使う
    （sent_log の覚えは 1 本の作業から使う）。プロジェクトの置き場所が無ければ None。"""
    if not (PROJECTS_DIR and PROJECTS_DIR.is_dir()):
        return None
    return SENT_POOL.submit(sent_log.sent, PROJECTS_DIR, project, since, limit).result(timeout=60)


def _sent_work(*a):
    """専用の作業で読む。待っている数は、作業が終わったときに減らす（待つのをやめた要求も、終わるまで数に入れる）。"""
    try:
        return sent_log.sent(*a)
    finally:
        with SENT_LOCK:
            SENT_PENDING[0] -= 1


@app.get("/api/hub/sent")
async def hub_sent(project: str = "", since: float | None = None, limit: int = 30):
    """出ていく側の記録（hubctl sent）。置き場所の各案件の Teams の控え（teams-sync-log.md）から、当方の投稿を新しい順に返す。
    読むだけ。project は控えのある案件の名前と一字一句同じときだけ絞り、場所には使わない（scripts/sent_log.py）。
    専用の 1 本の作業で順に読み、待ちが多いとき・時間を越えたときは busy で断る。"""
    if not PROJECTS_DIR or not PROJECTS_DIR.is_dir():
        return JSONResponse({"error": "no_projects_dir"}, status_code=404)
    if since is not None and not (0 <= since < 1e11):
        since = None
    with SENT_LOCK:
        if SENT_PENDING[0] >= SENT_QUEUE_MAX:
            return JSONResponse({"error": "busy"}, status_code=503)
        SENT_PENDING[0] += 1
    fut = asyncio.get_running_loop().run_in_executor(SENT_POOL, _sent_work, PROJECTS_DIR, project.strip(), since, limit)
    try:
        r = await asyncio.wait_for(asyncio.shield(fut), SENT_WAIT)
    except asyncio.TimeoutError:
        return JSONResponse({"error": "busy"}, status_code=503)
    return JSONResponse(r, status_code=404 if r.get("error") else 200)


@app.get("/api/hub/screen")
def hub_screen(pane: str, lines: int = 30):
    a = _worker(pane)
    if not a:
        return JSONResponse({"error": f"セッションが見つかりません: {pane}"}, status_code=404)
    if a["status"] != "blocked":
        return JSONResponse({"pane": pane, "status": a["status"], "screen": read_screen(pane, lines)})
    text = read_prompt(pane)
    fp, seen, session = prompt_fp(text), SEEN_PROMPT.get(pane), a.get("session") or ""
    if not (session and seen_same(seen, fp, session)):  # ハブがこの問いを見た。答えられるのは、ユーザーの次の発言から（セッションが分からなければ、見直しでも起点を置き直す。codex-review）
        SEEN_PROMPT[pane] = (fp, hub_human_turn(), session)
    # 同じセッションの同じ問いを見直しただけなら、最初に見たときの発言を起点のままにする（答える前に見直すと「同じ発言の中」になって断られていた。
    # 2026-11-19）。同じ pane でセッションを作り直して見た目の同じ問いが出たときは、新しい問いとして起点を置き直す（codex-review）
    SEEN_AT[pane] = time.time()  # 最後に見た時刻（報告が、どの問いを見て書かれたかを結ぶ）
    return JSONResponse({"pane": pane, "status": "blocked", "screen": text, "prompt": read_ask(text)})


_probes: dict[str, tuple[float, str]] = {}  # 点検で実際に試した結果: 名前 → (時刻, 不具合の説明。空なら使える)


def _probe(name: str, fn, keep_ok: float = 60, keep_ng: float = 5) -> str:
    """実際に試す点検。画面を開くたびに聞き取りや Anthropic を叩かないよう、結果を少しのあいだ覚えておく。
    使えなくなった・理由が変わった・使えるように戻ったときだけ、日付と時刻つきでログに 1 行残す（あとで外の障害と時刻を突き合わせるため）。"""
    hit = _probes.get(name)
    if hit and time.time() - hit[0] < (keep_ng if hit[1] else keep_ok):
        return hit[1]
    try:
        problem = fn() or ""
    except Exception as e:
        problem = f"{type(e).__name__}: {str(e)[:160]}"
    _probes[name] = (time.time(), problem)
    if problem != (hit[1] if hit else ""):  # 初めての「使える」と、変わらない結果は書かない
        said = f"使えません — {' '.join(problem.split())}" if problem else "使えるように戻りました"
        print(f"[doctor] {name}: {said}  @{time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    return problem


def _probe_wav(seconds: float, amp: float = 0.0) -> bytes:
    """点検に渡す音（16kHz・1ch）。amp が 0 なら無音、ほかは決まった種の白い雑音（声は入っていない）。"""
    r = random.Random(20261122)
    n = int(16000 * seconds)
    frames = b"\0\0" * n if not amp else b"".join(struct.pack("<h", int(max(-1.0, min(1.0, r.gauss(0, amp))) * 32767)) for _ in range(n))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(frames)
    return buf.getvalue()


def _whisper_problem() -> str:
    """短い無音を実際に聞き取らせる（同じポートで別のサービスが動いているだけ、を「使える」と数えないため）。
    声の区間（vad）を使うときは、声の入っていない雑音も聞き取らせて、空になるかを見る（vad を読まない版の whisper は、黙って雑音を文字にすることがある。
    2026-11-22 に、この雑音は vad なしで「コミット」、vad=true で空になることを実際の whisper で確かめた）。空でも VAD が効いている証明にはならない
    （vad を読まなくても、この雑音をもともと空にする whisper もありうる）。気づけるのは、この雑音を文字にする場合だけ。"""
    silence = _probe_wav(0.3)
    try:
        reply = _whisper(silence, "probe.wav", timeout=30)
        ok = isinstance(reply, dict) and isinstance(reply.get("text"), str)
    except (urllib.error.HTTPError, ValueError):  # 何かは応答したが、whisper の返事の形ではない
        ok = False
    except OSError as e:
        return f"whisper のサーバーが {WHISPER} で動いていません（{str(e)[:80]}）。起動してください。入れ方は README の「準備」にあります"
    if not ok:
        if WHISPER_VAD:  # vad を付けなければ答える。モデルを読み込んでいないことが多い（一度きりの失敗のこともある。点検は 5 秒で取り直す）
            try:
                plain = _whisper(silence, "probe.wav", timeout=30, vad=False)
                ok_plain = isinstance(plain, dict) and isinstance(plain.get("text"), str)
            except (OSError, ValueError):
                ok_plain = False
            if ok_plain:
                return ("whisper は、声の区間（vad=true）を付けた確かめにだけ答えませんでした。声の区間を見分けるモデル（VAD）を読み込んでいないことが多いです。"
                        "起動に `-vm <Silero のモデル>` があるか確かめてください（README の「準備」）。使わないなら config.env に VL_WHISPER_VAD=off")
        return f"{WHISPER} は応答しますが、whisper の返事ではありません。別のサービスが同じポートを使っていないか確かめ、config.env の VL_WHISPER を合わせてください"
    if WHISPER_VAD:
        try:
            heard = re.sub(r"\s+", " ", _text_of(_whisper(_probe_wav(2.0, 0.1), "probe.wav", timeout=30))).strip()
        except (OSError, ValueError) as e:
            return f"whisper に声の入っていない雑音を渡す確かめに失敗しました（{str(e)[:80]}）。もう一度開いて確かめてください"
        if heard:
            return (f"whisper に声の入っていない雑音を渡すと「{heard[:30]}」と聞き取りました。声の区間（vad=true）が効いていません"
                    "（VAD に対応していない版の whisper.cpp など。`whisper-server --help` に `--vad-model` がある版を、`-vm` を足して起動してください）。"
                    "使わないなら config.env に VL_WHISPER_VAD=off")
    return ""


def _narrator_problem(model: str) -> str:
    """語り手に実際に届くかを、課金のない問い合わせ（モデルの情報の取得）で確かめる。認証とモデル名の両方が分かる。"""
    try:
        anthropic.Anthropic(timeout=10).models.retrieve(model)
    except anthropic.NotFoundError:
        return f"語り手のモデル名「{model}」が見つかりません。設定フォルダの narrator_model か、config.env の VL_NARRATOR_MODEL を見直してください"
    except anthropic.APIConnectionError as e:
        return f"Anthropic に届きません（{str(e)[:80]}）。ネットワークを確かめてください"
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, TypeError):  # TypeError: 認証の情報がどこにも無い
        return "Anthropic の認証が通りません。`ant auth login` でログインするか、config.env に ANTHROPIC_API_KEY を書いてください"
    except anthropic.AnthropicError as e:
        if isinstance(e, anthropic.APIStatusError) and e.status_code >= 500:  # 500・503・529 など。ログインし直しても直らない
            return (f"Anthropic 側で一時的な障害が起きています（{e.status_code}）。ログインし直す必要はありません。"
                    "しばらく待ってから点検を開き直してください。障害の状況は https://status.claude.com/ で見られます")
        return f"Anthropic に確かめられませんでした（{type(e).__name__}）。ログインがまだなら `ant auth login`、または config.env に ANTHROPIC_API_KEY を書いてください"
    return ""


def _integration_problem() -> str:
    """herdr の Claude 連携。これが無いと herdr がセッション ID を持てず、返事を記録から読めない。"""
    try:
        out = subprocess.run(["herdr", "integration", "status"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "herdr に聞けませんでした"
    line = next((l for l in out.splitlines() if l.startswith("claude:")), "")
    return "" if line and "not installed" not in line else "`herdr integration install claude` を実行してください（Claude Code のセッションを見分けて、返事を読むのに使います）"


@app.get("/api/doctor")
def doctor():
    """使い始めるのに要るものが揃っているかの点検。ok でない項目に、次にやることを添える。"""
    checks = []

    def add(name: str, ok: bool, todo: str = "", optional: bool = False):
        checks.append({"name": name, "ok": ok, "todo": "" if ok else todo, "optional": optional})

    up = "result" in herdr("agent", "list", timeout=10)
    add("herdr", up, "herdr を起動してください")
    if up:
        problem = _probe("integration", _integration_problem)
        add("herdr の Claude 連携", not problem, problem)
    problem = _probe("whisper", _whisper_problem)
    add("聞き取り（whisper）", not problem, problem)
    speakers = []
    try:
        speakers = [st["id"] for s in json.loads(urllib.request.urlopen(f"{TTS_URL}/speakers", timeout=5).read()) for st in s["styles"]]
    except Exception:
        pass
    if VOICE not in VOICE_ENGINES:
        add("声のエンジンの設定", False, f"config.env の VL_VOICE={VOICE!r} は知らない名前です。{' か '.join(VOICE_ENGINES)} にしてください（いまは AivisSpeech で動いています）")
    add(f"声（{VOICE_NAME}）", bool(speakers), f"{VOICE_NAME} が {TTS_URL} で動いていません。起動してください（Docker なら OrbStack などが動いているかも確認）。入れ方は README の「準備」にあります")
    if speakers:
        add("既定の声", DEFAULT_SPEAKER in speakers, f"既定の声（ID {DEFAULT_SPEAKER}）が {VOICE_NAME} にありません。config.env の VL_DEFAULT_SPEAKER を、入っている声の ID にしてください")
    model = narrator_model()
    if "/" in model:
        linked, left = (GATEWAY_DIR / ".vercel").is_dir(), _saved_gateway_token()[1]
        add(f"語り手（{model}）", linked and left > 0,
            f"{GATEWAY_DIR} で `vercel link` を実行して、Vercel のプロジェクトに紐づけてください" if not linked
            else "Vercel のトークンが切れています。10分以内に自動で取り直します（そのあいだは既定の語り手で訳します）")
    else:
        problem = _probe(f"narrator:{model}", lambda: _narrator_problem(model), keep_ok=300)
        add(f"語り手（{model}）", not problem, problem)
    HUB.doctor(add)
    add("セッションを立てる場所", bool(PROJECTS_DIR and PROJECTS_DIR.is_dir()),
        f"{AGENT}に新しいセッションを立てさせるなら、config.env の VL_PROJECTS_DIR にプロジェクトの置き場所を書いてください", optional=True)
    return JSONResponse({"ok": all(c["ok"] or c["optional"] for c in checks), "checks": checks, "config_dir": str(CONFIG_DIR), "narrator": model})


@app.post("/api/hub/start")
def hub_start():
    """ハブが居なければ立てる（画面の「ハブを起動」と hub-start から呼ばれる）。"""
    r = ensure_hub()
    hlog(f"起動の依頼: {json.dumps(r, ensure_ascii=False)}", flush=True)
    return JSONResponse(r, status_code=200 if r["ok"] else 503)


# ---------- 画面との接続・セッションとの一往復・読み上げ（1対1でも、ハブからの報告でも使う） ----------
_CONN_SEQ = __import__("itertools").count(1)


class Client:
    """接続中の画面ひとつ。"""

    def __init__(self, sock: WebSocket):
        self.sock = sock
        self.speaker: int | None = None  # この画面で選ばれている声
        self.voice = True  # この画面で読み上げるか（画面の 🔇 で切ると、返答は文字だけになる）
        self.visible = False  # この画面がいま見えているか。見えている画面が1つでもあれば、プッシュ通知は送らない（画面に声と文字で届くため）
        self.sync = False  # 同期の合図を返す画面か（名乗りで決まる）
        self.seen = 0.0  # 画面が最後に様子（hello）を知らせてきた時刻
        # 機械（スタックチャン）か。機械の口（VL_DEVICE_PORTS）で受けた接続は、名乗りに関係なく機械で、置き場所も口で決まる（origin_guard の device_place）。
        # 画面も hello で kind: "stackchan" と名乗れば機械として扱う（守りが強くなる側だけ）。機械は見えている画面に数えず、
        # 機械からの発言は、承認に答える・閉じるに使わない
        self.place = device_place(getattr(sock, "scope", None) or {})  # 機械の置き場所（home・car・desk）。口で決まった機械は名乗りで変えない
        self.bound = bool(self.place)
        self.kind = "device" if self.bound else "screen"
        self.audio = "wav"  # 返答の声の形。機械が audio: "opus" を名乗れば Ogg の Opus
        self.win = ""  # 窓の印（画面はタブごとに hello で名乗る。つなぎ直しても同じ窓として、最後に話しかけた窓を覚える）
        self.connected = time.time()
        self.last_turn = 0.0  # この接続が最後に話しかけた時刻（同じ窓の印の接続が複数あるとき、話しかけた接続を選ぶ）
        self.cid = f"conn:{next(_CONN_SEQ)}"  # 窓の印を名乗らない接続の印（id() は解放後に使い回されるので使わない。a2a 8f05ef33）

    async def send(self, **msg):
        await self.sock.send_text(json.dumps(msg, ensure_ascii=False))


CLIENTS: set[Client] = set()
# 窓の印ごとの、まだ届けていない知らせ（ハブ以外への指示を送らなかった・送れなかった。その窓の画面がつながっていなかった）。
# 次にその窓が名乗ったときに届ける（つなぎ直した・読み込み直した画面にも、指示の行方を伝える。a2a 6604a50a の 3 回目）
UNSENT_NOTICES: dict[str, list[dict]] = {}


async def tell_stop_gen(n: int):
    """どこかの画面で止めた。停止の回数を全部の画面に知らせる（画面が切れていれば知らせない）。"""
    for c in list(CLIENTS):
        with contextlib.suppress(Exception):
            await asyncio.wait_for(c.send(type="stopgen", n=n), 5)


async def tell_window(win: str, **msg):
    """窓の印 win の画面に知らせる（つなぎ直した画面にも）。どの画面もつながっていなければ控え、次に名乗ったときに届ける。"""
    sent = False
    for c in [c for c in list(CLIENTS) if win_of(c) == win]:
        with contextlib.suppress(Exception):
            await c.send(**msg)
            sent = True
    if not sent:
        q = UNSENT_NOTICES.setdefault(win, [])
        q.append(msg)
        del q[:-5]
        while len(UNSENT_NOTICES) > 50:  # 古い窓の分から捨てる
            UNSENT_NOTICES.pop(next(iter(UNSENT_NOTICES)))
PANE_LOCKS: dict[str, asyncio.Lock] = {}  # ひとつのセッションへの「送って返信を読む」は同時に1件だけ
REPORTING = False  # ハブが自動通知に応答している最中か
REPORT_IDLE: asyncio.Event | None = None  # 報告が終わったときに立つ（Slack から来た送信が、報告の終わりを待つのに使う）。イベントループの中で作る
SLACK_QUEUE_WAIT = 45.0  # Slack から来た送信が、報告の終わりを待つ上限（秒）。送り（herdr の上限 30 秒）と合わせて hubctl send の待ち（100 秒）に収める
REPORT_SEQ = 0  # 報告の通し番号（報告を始めるたびに進む）
# 送る前のハブ以外への指示（番号 → 印 {"key", "sent", "dropped", "handed", "lock", "notify"}）。どの画面の「停止」でも全部を取り消す。
# 接続が切れても、送る仕事が終わるまで残す（切れた画面の指示も、ほかの画面の停止で送らせない。a2a 38ef4720）
DIRECT_PENDING: dict[int, dict] = {}
DIRECT_SEQ = {"n": 0}
STOP_GEN = {"n": 0}  # どこかの画面で「停止」を押した回数（声の聞き取りの間に止めたかを、指示を作るときに見る）


def drop_pending(d: dict):
    """送る前のハブ以外への指示 d を取り消す。まだ送っていなければ送らせず（送るスレッドの commit が見る）、指示を出した画面に知らせる。"""
    with d["lock"]:
        if d["sent"] or d["dropped"] or DIRECT_PENDING.get(d["key"]) is not d:  # 送った・取り消した・送る仕事に渡さずに終わった
            return
        d["dropped"] = True
    DIRECT_PENDING.pop(d["key"], None)
    d["notify"]()


REPORT_SPEECH: set = set()  # いま読み上げている報告の止める合図（画面の「停止」で立てる。codex-review bcd26ac）
REPORT_FAILED: dict[int, bool] = {}  # 番号ごとに、報告が失敗・時間切れで終わったか（直近の分だけ持つ）
LAST_DONE: dict[str, tuple] = {}  # セッションごとの、最後に報告した (ターンの目印, 返答)。同じ返答を二度知らせないため
_tasks: set[asyncio.Task] = set()


def pane_lock(pane: str) -> asyncio.Lock:
    return PANE_LOCKS.setdefault(pane, asyncio.Lock())


# ask がワーカーのスレッドの中で送る直前に呼ぶ確かめ（False なら送らずに ("cancelled", "")）。画面の直接の指示で、スレッドの空きを待つ間に
# 止めた・次の発言をしたら送らない（codex-review 6096e1d）。送る仕事を作るときに値が写るので、その指示の分だけに効く
ASK_COMMIT: contextvars.ContextVar = contextvars.ContextVar("ask_commit", default=None)


def _agent_state(r) -> str:
    """herdr の待ちの返事から、セッションの状態（idle・working・blocked・done）を取り出す。"""
    res = (r.get("result") or {}) if isinstance(r, dict) else {}
    return str((res.get("agent") or {}).get("agent_status") or res.get("agent_status") or "")


async def ask(agent: dict, text: str) -> tuple[str, str]:
    """セッションに送り、返信が出そろうまで待つ。("done", 返信) / ("blocked", 画面の末尾) / ("error", 理由) を返す。
    ASK_COMMIT が置かれていれば、送る直前にスレッドの中で確かめ、False なら送らずに ("cancelled", "")。"""
    commit = ASK_COMMIT.get()
    talker = TALKERS.get(agent["pane"])
    if talker:  # herdr の外の話し相手。止まる（blocked）ことはない
        try:
            return "done", await asyncio.to_thread(talker.ask, text)
        except Exception as e:  # 待ちきれなかった（TimeoutError）なども、失敗として返す（報告の記録に残すため）
            return "error", str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}"
    pane = agent["pane"]
    path = jsonl_path(agent)
    offset = path.stat().st_size if path and path.exists() else 0  # 新しいセッションは最初の発言まで記録が無い
    # 送る（herdr が受け取ったら返る。ハブ経由の送りと同じ）。送り途中の印を持つので、停止は受け取られるのを待ってから「止めました」と返す
    # （起動してから受け取るまでの間に止めても、「止めました」のあとに届かない。codex-review 57c6291 の P1）
    # 送ったその時点から working（または blocked）を見届けるまで待つ（送った直後の idle に反応しない。送りと見届けを別々に呼ぶと、その間に
    # 終わった短い返事を 5 秒見逃す。codex-review fdc6b04）。5 秒で見逃したら（agent_prompt_stalled。受け取りは済んでいる）、終わりを待つ
    r = await asyncio.to_thread(_if_still, commit, functools.partial(herdr, timeout=30), "agent", "prompt", pane, text,
                                "--wait", "--until", "working", "--until", "blocked", "--timeout", "10000")  # herdr の上限は、見届けの 5 秒より長く（先に時間切れを返さない。codex-review 8a47002）
    if r is None:
        return "cancelled", ""
    err = (r.get("error") or {}).get("code")
    if err and err != "agent_prompt_stalled" or (not err and "result" not in r):
        return "error", json.dumps(r, ensure_ascii=False)[:200]
    if _agent_state(r) != "blocked":  # 承認待ちで止まったのを見たら、その結果を使う（待ち直すと、別の画面で答えたあとの状態を読む。codex-review 711ce71）
        r = await asyncio.to_thread(herdr, "agent", "wait", pane, "--timeout", "600000", timeout=610)
    if (r.get("error") or {}).get("code") or "result" not in r:
        return "error", json.dumps(r, ensure_ascii=False)[:200]
    state = _agent_state(r)
    if state == "blocked":
        return "blocked", read_prompt(pane)[-1500:]
    reply = ""
    for _ in range(10):  # JSONL への書き込みは状態変化より少し遅れることがある
        if not (path and path.exists()):
            # 立てたばかりのセッションは、最初の発言まで herdr がセッション ID を持たず記録も無い。ここで引き直す
            agent = next((a for a in list_agents() if a["pane"] == pane), agent)
            path = jsonl_path(agent)
        reply = reply_since(path, offset) if path and path.exists() else ""
        if reply:
            break
        await asyncio.sleep(0.5)
    return "done", reply or read_screen(pane, 40)[-2000:]


async def plain_sentences(text: str, cancel: asyncio.Event):
    """語り手を通さず、文のまま区切って返す（本体がその場で答えた決まった問いの答え。もう話し言葉になっている）。"""
    for part in re.split(r"(?<=[。！？!?])\s*", text):
        if cancel.is_set():
            return
        if part.strip():
            yield part.strip()


async def speak_to(clients: list[Client], text: str, cancel: asyncio.Event, plain: bool = False):
    """話し言葉に直し、文ごとに合成して画面へ送る。声は画面ごとの選択に従う。切れた画面は飛ばす。
    読み上げを切った画面には音声を送らない（文字は先に reply で届いている）。聞く画面が無ければ、語り手も合成も呼ばない。
    plain なら語り手を通さず、文のまま合成する（本体の決まった答え。語り手の待ち・失敗に左右されない）。"""
    alive = list(clients)

    async def each(fn):
        for c in list(alive):
            try:
                await fn(c)
            except Exception:
                if len(clients) == 1:
                    raise  # 1対1では呼び出し側がエラーとして扱う
                alive.remove(c)

    await each(lambda c: c.send(type="speaking"))  # これ以降の音声がこのターンのもの（止めた後に届く古い音声と区別する）
    n = 0
    if any(c.voice for c in alive):
        async with aclosing(plain_sentences(text, cancel) if plain else narrate(text, cancel)) as sentences:  # 途中で抜けたら、語り手の生成もすぐ止める
            async for sentence in sentences:
                # 読み上げの途中で切った画面は、次の文から外す。聞く画面が無くなれば、そこで止める
                voice = {c: DEFAULT_SPEAKER if c.speaker is None else c.speaker for c in alive if c.voice}  # VOICEVOX では 0 も声の ID
                if not voice:
                    break
                wavs = {sp: await asyncio.to_thread(synth, sentence, sp) for sp in set(voice.values())}
                # Opus を望む接続（機械）があれば、その声だけ縮める。縮められなければ WAV のまま（機械は頭の 4 バイトで見分ける）
                opus = {sp: await asyncio.to_thread(to_opus, wavs[sp]) for sp in {voice[c] for c in voice if getattr(c, "audio", "wav") == "opus"}}
                if cancel.is_set() or not alive:
                    break
                body = lambda c: (opus.get(voice[c]) or wavs[voice[c]]) if getattr(c, "audio", "wav") == "opus" else wavs[voice[c]]
                await each(lambda c: c.sock.send_bytes(body(c)) if c in voice and c.voice else asyncio.sleep(0))
                n += 1
    # 止めて抜けたら、その印を付ける（画面は、停止の結果（止めました・確かめを頼む）を「話し終わりました」で上書きしない。codex-review 10a2010）
    await each(lambda c: c.send(type="done", sentences=n, **({"stopped": True} if cancel.is_set() else {})))


# 報告の声を鳴らすのは 1 か所（設計書 R1・課題 K9。2026-11-23 本人「推奨で」）。文字はすべての画面に出し、声は最後に話しかけた窓だけで鳴らす。
# 車の機械（機械の口で car と決まった接続）がつながったら、つながった時刻を「話しかけた」と同じに数える（車に乗っている間は車）。
# 起動のあと、まだどの窓も話しかけていなければ、これまでどおりすべての窓で鳴らす（取りこぼさない）。VL_RING=all で、いつもすべての窓
RING = "all" if os.environ.get("VL_RING", "one").strip().lower() == "all" else "one"
WIN_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
WIN_SPOKE: dict[str, float] = {}  # 窓の印 → 最後に話しかけた時刻（画面・声の発言。ボタンは数えない）


def win_of(c) -> str:
    """窓の印。口で決まった機械は置き場所（家と車で 1 つずつ）、画面は名乗った印、名乗らない古い画面は接続ごと。"""
    if getattr(c, "bound", False):
        return "device:" + str(getattr(c, "place", ""))
    return getattr(c, "win", "") or getattr(c, "cid", "") or f"conn:{id(c)}"


def ring_targets(clients: list) -> list:
    """報告の声を鳴らす窓（1 つ）。最後に話しかけた窓（車の機械はつながった時刻も数える）の、いちばん新しい接続。
    どの窓も話しかけていなければ、渡されたすべて。🔇 の窓が選ばれたら、声はどこにも鳴らさない（その窓で文字を読む）。"""
    if RING == "all" or not clients:
        return list(clients)

    def act(c) -> float:
        t = WIN_SPOKE.get(win_of(c), 0.0)
        if getattr(c, "bound", False) and getattr(c, "place", "") == "car":
            t = max(t, getattr(c, "connected", 0.0))
        return t
    best = max(clients, key=lambda c: (act(c), getattr(c, "last_turn", 0.0), getattr(c, "seen", 0.0), getattr(c, "connected", 0.0)))
    return [best] if act(best) > 0 else list(clients)


# 本体がその場で答える決まった問い（設計書 R4・R11 の段 0、S2。2026-11-23 本人「推奨で」）。発言の全体が決まった形のときだけで、ほかは頭へ（迷ったら頭）。
# 答えたやり取りも頭の台帳と記録に載せ、利用者が聞いた文を空にする（その前の頭の問いへの「はい」に使わせない）。VL_QUICK=off で使わない
QUICK = os.environ.get("VL_QUICK", "on").strip().lower() != "off"
QUICK_CURSOR: dict = {}  # 「次」をどこまで読んだか（quick.answer が書き換える）


def quick_snapshot() -> dict:
    """決まった問いに答えるための、本体の状態の写し（セッションの一覧・承認待ちの種類・頼んだ件の見張り・判断待ち）。"""
    rows = []
    agents, agents_ok = list_agents_checked()
    for a in agents:
        st, ask = str(a["status"]), ""
        if a["status"] == "blocked" or stepping(a):  # 画面で 1 問ずつ答えている途中（stepping）は、問いが残っているときだけ待ち（/api/prompts と同じ。codex-review）
            ask = _ask_of(a)["kind"]
            if a["status"] == "blocked" or ask != "none":
                st = "blocked"
        r = {"pane": a["pane"], "name": f"{a['cwd'].rsplit('/', 1)[-1]}（{a['title']}）" if a.get("title") else a["cwd"].rsplit("/", 1)[-1], "status": st}
        if st == "blocked":
            r["ask"] = ask
        rows.append(r)
    try:
        box = HUB.open_items()
    except Exception:
        box = {"items": [], "problem": True}
    return {"sessions": rows, "sessions_problem": not agents_ok,
            "watching": sum(1 for t in TASKS.values() if t["status"] in TASK_ACTIVE), "inbox": box.get("items", []),
            "inbox_problem": bool(box.get("problem")), "inbox_partial": bool(box.get("partial"))}


async def hub_quick(talker, kind: str, text: str, pane: str, said: str | None = None, via: str = "",
                    admitted: int | None = None) -> tuple[str, str, bool]:
    """決まった問いに本体が答える（頭に送らない）。受け取った順（ACCEPT_LOCKS）に、発言と答えを頭の台帳と記録に載せる。(答え, 理由, 記録に書けたか)。
    admitted は受け付けたその場の番号（台帳にはこの番号で載せる。順番待ちの間に止めたら、その発言はもういまの発言ではない）。"""
    async with ACCEPT_LOCKS.setdefault(pane, asyncio.Lock()):
        kw = {"said": said} if said is not None else {}
        try:
            snap = await asyncio.to_thread(quick_snapshot)
            answer = quick.answer(kind, snap, QUICK_CURSOR, time.time())
        except Exception as e:  # 状態を読めなかった。断りとして台帳に載せ、前の確認を失効させる（失敗の知らせのあとの発言を、古い確認への答えにしない。codex-review）
            traceback.print_exc()
            why = f"いまの状態を読めませんでした（{type(e).__name__}）。少しおいて、もう一度聞いてください"
            await talker.refused(text, why, **kw, **({"via": via} if via else {}), **({"admitted": admitted} if admitted is not None else {}))
            return "", why, True
        saved = await talker.answered_here(text, answer, **kw, **({"via": via} if via else {}), **({"admitted": admitted} if admitted is not None else {}))
        hlog(f"すぐ答えた（{kind}）: {len(answer)}字", flush=True)
        return answer, "", saved


# 頼んでから返事が来るまでの、本体の一言（ハブとの往復だけ）。Hermes の返事は中央値 14 秒かかり、その間黙っていると、聞こえたのか分からない
# （2026-11-23 に測ると、Hermes の道具の知らせは頼んでから 2 秒前後で届く）。PROGRESS_AFTER 秒たっても返事が無ければ、決まった一言を話す。
# Hermes への頼み方は変えない（守りが「利用者が聞いた返事」として見る文は、Hermes の返事のまま。この一言は記録にも台帳にも載せない）。0 で使わない
PROGRESS_TEXT = "確認しています"
_progress = os.environ.get("VL_PROGRESS_AFTER", "3").strip()
try:
    PROGRESS_AFTER = max(0.0, float(_progress))
except ValueError:
    PROGRESS_AFTER = 3.0
_progress_wav: dict[int, bytes] = {}  # 声ごとに一度だけ合成して取っておく（決まった一言なので）


async def speak_progress(clients: list[Client], pane: str, cancel: asyncio.Event, state: dict):
    """返事を待つ間の一言。状態の文を出し、読み上げる画面には、声を中に入れた 1 つの通知（cue）を送る。語り手は通さない（決まった文なので）。
    返事の声の合図（speaking・done）と音声のかたまりは使わない（画面が受け付ける状態を変えず、止めた報告の続きを鳴らさない。画面は一言だけを鳴らし、
    鳴り終わったら返事を待つ表示に戻す）。返事を待つのと並べて動かす（声の合成が遅くても、返事を遅らせない）。state["stop"] が立ったら、
    まだ送っていない声は捨てる（返事のあとに一言が届かないように）。声を送り始めたら state["sending"]、送ったら state["sent"]。"""
    for c in list(clients):
        try:
            await c.send(type="status", text=PROGRESS_TEXT + "…")
            if not c.voice or cancel.is_set() or state["stop"]:
                continue
            sp = DEFAULT_SPEAKER if c.speaker is None else c.speaker
            if sp not in _progress_wav:
                _progress_wav[sp] = await asyncio.to_thread(synth, PROGRESS_TEXT, sp)
            if not c.voice or cancel.is_set() or state["stop"]:  # 合成の間に、読み上げを切った・止めた・返事が来た
                continue
            state["sending"] = True  # ここから送り終わるまで、返事の側は待つ（短い）
            try:
                await c.send(type="cue", pane=pane, text=PROGRESS_TEXT + "…", audio=base64.b64encode(_progress_wav[sp]).decode())
                state["sent"] = True
            finally:
                state["sending"] = False
        except Exception as e:  # 一言は添え物。声が作れない・画面が切れても、返事を待つのは続ける
            print(f"[progress] 一言を送れませんでした: {type(e).__name__}: {str(e)[:120]}", flush=True)


def write_json(path: Path, data):
    """書きかけを残さないよう、一時ファイルに書いてから置き換える。中身は利用者だけが読める（0600）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.chmod(0o600)
    tmp.replace(path)


# ---------- プッシュ通知（画面を閉じていても、報告と承認待ちを知らせる） ----------
# Web Push（VAPID）。届け先は、画面の 🔔 で許可した端末（ブラウザが持つ購読）。どの画面も見えていないときだけ送る。
# 通知に載せるのは見出し（案件｜件名と、何が起きたか）だけ。本文や指示の中身は、開いてから読む。
# 中身は端末の鍵で暗号化されるので、配送する Apple などは読めない
PUSH_KEY_FILE = CONFIG_DIR / "push" / "vapid_private.pem"  # 作り直すと、それまでの購読は届かなくなる
PUSH_SUBS = STATE_DIR / "push-subscriptions.json"
# 配送の側が困ったときの連絡先（mailto: か、パスの無い https:）。配送サービスにはそのまま見える。未設定なら届かない宛先を名乗る
PUSH_SUBJECT = os.environ.get("VL_PUSH_SUBJECT") or "mailto:voice-loop@localhost"
PUSH_TTL = 6 * 3600  # 端末が圏外などで受け取れないとき、配送の側で待ってもらう上限
PUSH_MAX_SUBS = 10
# 送ってよい配送先（ブラウザの配送サービス）。購読の届け先を、この Mac から任意の場所へ送る口にしないため
PUSH_HOSTS = tuple(h.strip().lower() for h in (os.environ.get("VL_PUSH_HOSTS") or
                   "push.apple.com,fcm.googleapis.com,push.services.mozilla.com,notify.windows.com").split(",") if h.strip())
BLOCKED_POLL = 10  # 承認・入力待ちを見に行く間隔（秒）
# 画面は見えている間、20秒ごとに「見えている」を知らせ直す。これより長く途切れたら、見えていないとみなす
# （iPhone で裏に回したとき、「隠れた」の知らせが届かないまま接続が残っても、通知が止まりっぱなしにならないように）
VISIBLE_TTL = 45
REPORT_PUSH_WAIT = 30  # 報告の通知は、ハブの報告をこれより長くは待たずに送る（ハブが話し中・応答が遅いとき）
BLOCKED_NOTIFY_AFTER = float(os.environ.get("VL_BLOCKED_NOTIFY_AFTER", "60"))  # 止まってからこの秒数たっても止まったままなら知らせる
_push_lock = threading.Lock()  # 鍵を作る・購読を書き換えるのは一度にひとつ


def push_vapid():
    """VAPID の鍵。無ければ作る（初めて 🔔 を押したとき）。"""
    from py_vapid import Vapid02

    with _push_lock:
        if not PUSH_KEY_FILE.exists():
            PUSH_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
            PUSH_KEY_FILE.parent.chmod(0o700)
            v = Vapid02()
            v.generate_keys()
            tmp = PUSH_KEY_FILE.with_name(PUSH_KEY_FILE.name + ".tmp")
            v.save_key(str(tmp))
            tmp.chmod(0o600)
            tmp.replace(PUSH_KEY_FILE)
        return Vapid02.from_file(str(PUSH_KEY_FILE))


def push_public_key() -> str:
    """画面が購読するときに渡す公開鍵（applicationServerKey。非圧縮の点を base64url で）。"""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    raw = push_vapid().public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def push_subs() -> list[dict]:
    """保存している購読。読めなければ空（通知は届かないが、本体は止めない）。"""
    try:
        rows = json.loads(PUSH_SUBS.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError, RecursionError):
        traceback.print_exc()
        return []
    return [s for s in rows if push_problem(s) is None] if isinstance(rows, list) else []


# 届け先の URL の形。配送サービスの URL に使われる文字だけを許す（@・\・空白・制御文字・# を含むものは、
# 部品ごとに宛先の読み方が食い違い、許した名前と別の場所へ送られうるので断る）。番号は 443 だけ
PUSH_URL = re.compile(r"https://(?P<host>[a-z0-9-]+(?:\.[a-z0-9-]+)+)(?::443)?(?P<path>/[A-Za-z0-9._~/:?=&%+!-]*)", re.I)


def _sent_host(endpoint: str) -> str:
    """実際に送る部品（requests）が読む宛先の名前。読み方が食い違っていないかを確かめるため。"""
    import requests

    try:
        return (urllib.parse.urlsplit(requests.Request("POST", endpoint).prepare().url).hostname or "").lower()
    except Exception:
        return ""


def push_problem(sub) -> str | None:
    """購読の形がおかしければ、その理由。"""
    if not isinstance(sub, dict) or not isinstance(sub.get("endpoint"), str) or not isinstance(sub.get("keys"), dict):
        return "購読の形が読めません"
    endpoint = sub["endpoint"]
    m = PUSH_URL.fullmatch(endpoint) if len(endpoint) <= 2000 else None
    host = m.group("host").lower() if m else ""
    if not m or not any(host == h or host.endswith("." + h) for h in PUSH_HOSTS) or _sent_host(endpoint) != host:
        return f"知らない配送先です（{_clip(endpoint, 60)}）"
    keys = sub["keys"]
    if not all(isinstance(keys.get(k), str) and 0 < len(keys[k]) <= 200 for k in ("p256dh", "auth")):
        return "購読の鍵が読めません"
    return None


def push_add(sub: dict):
    with _push_lock:
        rows = [s for s in push_subs() if s["endpoint"] != sub["endpoint"]]
        rows.append({"endpoint": sub["endpoint"], "keys": {"p256dh": sub["keys"]["p256dh"], "auth": sub["keys"]["auth"]}, "added": time.time()})
        write_json(PUSH_SUBS, rows[-PUSH_MAX_SUBS:])


def push_drop(endpoints: list[str]):
    with _push_lock:
        rows = push_subs()
        keep = [s for s in rows if s["endpoint"] not in endpoints]
        if len(keep) != len(rows):
            write_json(PUSH_SUBS, keep)


def _push_send(subs: list[dict], payload: dict, urgency: str) -> tuple[int, list[str]]:
    """購読ごとに送る。(届けた数, もう使えない購読の endpoint)。"""
    from pywebpush import WebPushException, webpush

    vapid = push_vapid()
    sent, gone = 0, []
    for s in subs:
        host = urllib.parse.urlsplit(s["endpoint"]).hostname
        try:
            # vapid_claims は送るたびに作る（webpush が送り先ごとの aud を書き込むため、使い回すと別の配送先に合わない）
            webpush(s, json.dumps(payload, ensure_ascii=False), vapid_private_key=vapid, vapid_claims={"sub": PUSH_SUBJECT},
                    ttl=PUSH_TTL, headers={"Urgency": urgency}, timeout=10)
            sent += 1
        except WebPushException as e:
            code = getattr(e.response, "status_code", None)
            if code in (404, 410):  # 端末が購読をやめた・期限が切れた
                gone.append(s["endpoint"])
            print(f"[push] {host} に送れませんでした（{code}）: {str(e)[:160]}", flush=True)
        except Exception as e:  # 通信の失敗など。ほかの端末には送り続ける
            print(f"[push] {host} に送れませんでした: {type(e).__name__}: {str(e)[:160]}", flush=True)
    return sent, gone


def topic_path(tid: str | None) -> str:
    """通知を押したときに開く場所。件があればその件（/#t=<件>）、無ければ画面のはじめ。"""
    return f"/#t={tid}" if isinstance(tid, str) and topics.ID_RE.fullmatch(tid) else "/"


PUSH_OPEN = re.compile(r"/(?:#t=t[0-9a-f]{10}|#inbox|#i=[1-9][0-9]{0,9})?")  # 通知を押したときに開く場所（sw.js と画面も同じ形だけを受ける）


async def push_all(title: str, body: str, tag: str, urgency: str = "normal", only: list[dict] | None = None, url: str = "/") -> str:
    """どの画面も見えていなければ、購読している端末すべてに知らせる。結果は sent / visible / none / failed / error。
    only を渡すと、見えている画面の有無に関係なく、その購読だけに送る（🔔 を押したときの試しの通知）。
    url は、通知を押したときに開く場所（この画面の中の場所だけ。件 /#t=<件>、受け箱 /#inbox、判断待ちの 1 件 /#i=<番号>。ほかの形は /）。"""
    if only is None and any(c.visible and time.time() - c.seen < VISIBLE_TTL for c in CLIENTS):
        return "visible"
    subs = only if only is not None else await asyncio.to_thread(push_subs)
    if not subs:
        return "none"
    payload = {"title": _clip(title, 80), "body": _clip(body, 120), "tag": tag,
               "url": url if PUSH_OPEN.fullmatch(url) and (not url.startswith("/#t=") or topics.ID_RE.fullmatch(url[4:])) else "/"}
    try:
        sent, gone = await asyncio.to_thread(_push_send, subs, payload, urgency)
        if gone:
            await asyncio.to_thread(push_drop, gone)
    except Exception:  # 鍵を読めないなど。知らせられないだけで、呼び出し側は止めない
        traceback.print_exc()
        return "error"
    hlog(f"通知 {tag}: {sent}/{len(subs)} 件に送りました", flush=True)
    return "sent" if sent else "failed"


@app.get("/api/push")
def api_push():
    """画面が購読するための公開鍵と、いま登録されている端末の数。"""
    try:
        key = push_public_key()
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"ok": False, "reason": f"通知の鍵を用意できません: {type(e).__name__}"})
    try:  # 連絡先の形は、送るときに使う部品（py_vapid）の決まりで確かめる。おかしければ、押した時点で分かるように
        push_vapid().sign({"sub": PUSH_SUBJECT, "aud": "https://push.example"})
    except Exception:
        return JSONResponse({"ok": False, "reason": f"config.env の VL_PUSH_SUBJECT（{PUSH_SUBJECT}）の形が使えません。mailto:名前@ドメイン か、パスの無い https://ドメイン にしてください"})
    return JSONResponse({"ok": True, "key": key, "count": len(push_subs())})


@app.post("/api/push/subscribe")
async def api_push_subscribe(body: dict):
    """画面の 🔔 で許可した端末を登録する。test なら、その端末に試しの通知を送る（画面は開くたびに test なしで送り直す）。"""
    sub = body.get("subscription")
    why = push_problem(sub)
    if why:
        return JSONResponse({"ok": False, "reason": why}, status_code=400)
    try:
        await asyncio.to_thread(push_add, sub)
    except OSError as e:
        traceback.print_exc()
        return JSONResponse({"ok": False, "reason": f"登録を書けませんでした: {type(e).__name__}"})
    out = {"ok": True}
    if body.get("test"):
        out["test"] = await push_all("voice-loop", "通知を受け取れるようになりました", "test", only=[sub])
    return JSONResponse(out)


@app.post("/api/push/unsubscribe")
async def api_push_unsubscribe(body: dict):
    try:
        await asyncio.to_thread(push_drop, [str(body.get("endpoint", ""))])
    except OSError as e:
        traceback.print_exc()
        return JSONResponse({"ok": False, "reason": f"登録を書き換えられませんでした: {type(e).__name__}"})
    return JSONResponse({"ok": True})


async def watch_blocked():
    """承認・入力待ちで止まったセッションを、プッシュ通知で知らせる。止まって BLOCKED_NOTIFY_AFTER 秒たっても止まったままのときに1回
    （Mac の前で答えている承認では鳴らさない。止まったままなら重ねない）。ハブ経由で頼んだ件も、ここで知らせる
    （見張りの報告はハブに話させるだけで、通知は送らない）。届け先が無い間は見に行かない。見始めたときにもう止まっていたものは知らせない。"""
    held: dict[str, tuple[str, float, bool]] | None = None  # pane → (止まっているセッション, 止まりだしたのを見た時刻, 知らせたか)
    while True:
        await asyncio.sleep(BLOCKED_POLL)
        try:
            if not await asyncio.to_thread(push_subs):
                held = None  # 届け先が登録されたら、その時点の様子から見始める
                continue
            agents = await asyncio.to_thread(list_agents)
            if not agents:  # herdr に届かないと空で返る。ここで忘れると、戻ったときに止まったままのものを全部知らせ直すので、前の様子を残す
                continue
            first, before, held = held is None, held or {}, {}
            for a in agents:
                if a["status"] != "blocked":
                    continue
                p, who = a["pane"], a.get("session") or a["cwd"]
                old = before.get(p)
                who, since, told = old if old and old[0] == who else (who, time.time(), first)
                if not told and time.time() - since >= BLOCKED_NOTIFY_AFTER:
                    told = True  # 見えている画面があって送らなかったときも（その画面の上に承認の欄が出ている）
                    name = f"{a['cwd'].rsplit('/', 1)[-1]}｜{a['title']}"
                    await push_all(name, "承認・入力待ちで止まっています", f"blocked-{p}", urgency="high",
                                   url=topic_path(topics.current_for_session(a.get("session") or "")))  # 押すと、そのセッションの件（問いの欄）を開く（直接送った別の話のあとなら画面のはじめ）
                held[p] = (who, since, told)
        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()


# ---------- 頼んだ件の台帳（画面の 📋 と hubctl tasks） ----------
# ハブ経由で送った指示・立てたセッション・答えた承認と、その見張りの状態。見張りはメモリの中だけなので、
# 本体が止まると黙って消える。台帳は状態フォルダに書き、次に起動したとき「見張りが切れた」と分かるようにする
TASKS_FILE = STATE_DIR / "delegations.json"
TASK_KEEP = 3 * 86400  # 終わった件を残す期間
TASK_MAX = 100
TASK_ACTIVE = {"watching", "background", "blocked"}  # 見張りが続いている状態
TASK_FIELDS = {"id": str, "kind": str, "pane": str, "project": str, "title": str, "text": str, "started": (int, float),
               "updated": (int, float), "status": str, "note": str}
TASKS: dict[str, dict] = {}
TASK_RANGE: dict[str, dict] = {}  # 件 → 見張りの d（送った位置・記録・まとめた依頼）。本体の中だけ（台帳のファイルには書かない）
TASK_READ_MAX = 12  # hubctl read --task で見せる吹き出しの上限（最初の指示までと、新しいほうから）


def tasks_save():
    """残す範囲（動いている件はすべて。終わった件は3日以内で、合わせて最大100件になるまで新しい順に）に絞って書く。書けなくても止めない。"""
    now = time.time()
    rows = sorted(TASKS.values(), key=lambda t: t["started"], reverse=True)
    active = [t for t in rows if t["status"] in TASK_ACTIVE]
    ended = [t for t in rows if t["status"] not in TASK_ACTIVE and now - t["updated"] < TASK_KEEP][:max(0, TASK_MAX - len(active))]
    rows = sorted(active + ended, key=lambda t: t["started"], reverse=True)
    TASKS.clear()
    TASKS.update({t["id"]: t for t in rows})
    for old in [k for k in TASK_RANGE if k not in TASKS]:  # 台帳から外れた件は、範囲でも読めなくする
        TASK_RANGE.pop(old, None)
    try:
        write_json(TASKS_FILE, rows)
    except OSError:
        traceback.print_exc()


def text_fp(text: str) -> str:
    """送った文の指紋（空白をそろえて比べる）。セッションの記録の依頼と照らす。"""
    return hashlib.sha1(" ".join(text.split()).encode()).hexdigest()[:16]


def task_new(d: dict, kind: str) -> str:
    tid, now = os.urandom(6).hex(), time.time()
    TASKS[tid] = {"id": tid, "kind": kind, "pane": d["pane"], "project": d["project"], "title": d["title"], "text": _clip(d["text"], 300),
                  "started": now, "updated": now, "status": "watching", "note": "", "fp": text_fp(d["text"])}
    tasks_save()
    TASK_RANGE[tid] = d  # hubctl read --task で、この件の範囲を読むため。台帳から外れると、tasks_save がここからも外す
    return tid


def task_set(tid: str | None, status: str, note: str = ""):
    """件の状態を進める。変わらなければ書かない。"""
    t = TASKS.get(tid or "")
    if not t or (t["status"], t["note"]) == (status, note or t["note"]):
        return
    t["status"], t["updated"] = status, time.time()
    if note:
        t["note"] = note
    tasks_save()


def _load_tasks():
    """起動のとき、台帳を戻す。見張りの途中だった件は、もう誰も見ていないので「見張りが切れた」にする。"""
    try:
        rows = json.loads(TASKS_FILE.read_text())
    except FileNotFoundError:
        return
    except (OSError, ValueError, RecursionError):
        traceback.print_exc()
        return
    for t in rows if isinstance(rows, list) else []:
        if not (isinstance(t, dict) and all(isinstance(t.get(k), ty) for k, ty in TASK_FIELDS.items())):
            continue  # 壊れた件は飛ばす
        t = {**{k: t[k] for k in TASK_FIELDS}, **({"fp": t["fp"]} if isinstance(t.get("fp"), str) else {})}
        if t["status"] in TASK_ACTIVE:
            t["status"], t["updated"], t["note"] = "lost", time.time(), "本体が止まったので、見張りが切れました"
        TASKS[t["id"]] = t
    tasks_save()


_load_tasks()
topics.load()


@app.get("/api/hub/tasks")
async def hub_tasks():
    """頼んだ件（新しい順）。画面の 📋 と hubctl tasks が読む。
    台帳を書き換えるのはすべてイベントループの上（見張りと同じ）。別のスレッドで動かすと、見張りが足した件を片づけで消しうる"""
    now = time.time()
    if any(t["status"] not in TASK_ACTIVE and now - t["updated"] >= TASK_KEEP for t in TASKS.values()):
        tasks_save()  # ほかの更新が無くても、3日を過ぎた件はここで外す
    rows = sorted(TASKS.values(), key=lambda t: t["started"], reverse=True)
    return JSONResponse([{**t, "age_minutes": int((now - t["started"]) // 60), "since_minutes": int((now - t["updated"]) // 60)} for t in rows])


# ---------- ハブ経由で頼んだ件の見張りと報告 ----------
TASK_DONE = re.compile(r"<tool-use-id>([^<]+)</tool-use-id>")
WATCHING: dict[str, int] = {}  # セッションごとの、動いている見張りの数
WATCH_GEN: dict[str, int] = {}  # セッションごとの、見張りの世代。指示を送るたびに進み、古い世代の見張りは引き上げる
SENT_GEN: dict[str, int] = {}  # セッションごとの、指示を送ろうとした回数（ハブ経由の send・start と、画面・声からの直接の発言）。送る前に進める（届いたか分からないときも進んだまま）。Slack への報告が、新しい件の結果を古い件として送らないために見る。承認への答えは同じ件の続きなので進めない
INFLIGHT: dict[str, int] = {}  # セッションごとの、いま送っている途中の指示の数（ハブ経由の送りは herdr が受けるまで、画面・声からの直接の発言は返事が来るまで。待つ側が取り消されても、送る処理が終わるまで数える）。重なって送った件の Slack への報告に、どちらの結果か分からない本文を載せないために見る
OPEN_WATCH: dict[str, dict] = {}  # セッションごとの、最後にハブ経由で送った件。完了の報告を始める前に次の件が送られたら、次の件にまとめる（merge_open）


def delegation_state(path: Path, offset: int, text: str) -> tuple[bool, str, bool, bool]:
    """offset 以降の記録から、送った指示の行方を読む。
    (指示が読まれたか, 返答, そのあとユーザーの次の発言があったか, 裏の作業が残っているか)。
    相手が自分から再開した分（バックグラウンド作業の完了など）も、次の発言が来るまでは同じ件の返答として数える。
    「裏の作業」は run_in_background で始めたコマンドのうち、完了の知らせ（task-notification）がまだ来ていないもの。"""
    head = " ".join(text.split())[:60]
    found = superseded = False
    turn: list[str] = []
    final: list[str] = []
    background: set[str] = set()
    for line in read_lines(path, offset=offset):
        if found:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                d = {}
            content = (d.get("message") or {}).get("content")
            if d.get("type") == "assistant" and isinstance(content, list):
                background |= {b.get("id") for b in content if b.get("type") == "tool_use" and (b.get("input") or {}).get("run_in_background") is True}
            elif d.get("type") == "user" and isinstance(content, str) and content.lstrip().startswith("<task-notification>"):
                background -= set(TASK_DONE.findall(content))
        for kind, t in iter_events([line]):
            if kind == "user":
                if found:
                    superseded = True
                    break
                found = " ".join(t.split()).startswith(head)
            elif found:
                if kind == "tool":
                    final = []
                else:
                    turn.append(t)
                    final.append(t)
        if superseded:
            break
    return found, "\n\n".join(final or turn), superseded, bool(background)


def start_watch(d: dict, kind: str = "send"):
    """見張りを始め、頼んだ件の台帳に載せる。kind は send（指示）・spawn（立てて最初の指示）・answer（承認・質問への答え）。"""
    d["task"] = task_new(d, kind)
    task = asyncio.create_task(watch_delegation(d))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def watch_delegation(d: dict):
    """相手のセッションが片づくまで見張り、終わるか、承認・入力待ちで止まったら、ハブに報告させる。"""
    WATCHING[d["pane"]] = WATCHING.get(d["pane"], 0) + 1
    try:
        await _watch_delegation(d)
    except Exception:
        hlog(f"見張りが失敗: {d['project']}｜{d['title']}", flush=True)
        traceback.print_exc()
        if TASKS.get(d.get("task") or "", {}).get("status") != "done":
            task_set(d.get("task"), "failed", "見張りの途中で失敗しました（本体のログを見てください）")
        if not d.get("_slack_failed_sent") and not d.get("_slack_done"):  # 報告の途中の失敗は report が知らせた。完了の報告を送ったあとの失敗は重ねない
            relay_slack(d, f"{d['project']}｜{d['title']}", "の見張りが途中で失敗しました", "", slack_report.SCREEN)
    finally:
        WATCHING[d["pane"]] -= 1
        d["_closed"] = True  # 見張りが終わった件は、次の件にまとめない
        if OPEN_WATCH.get(d["pane"]) is d:
            OPEN_WATCH.pop(d["pane"], None)


def _superseded(d: dict, label: str):
    """同じセッションに次の指示が送られ、この件の見張りを引き上げる（まだ完了を報告していないとき）。"""
    if d.get("_merged_into"):  # 次の件にまとめた。報告はそちらがまとめてする（Slack の行き先が移るときの一言は、まとめたときに送った）
        task_set(d.get("task"), "replaced", "同じセッションへの次の指示と、まとめて報告します")
        return
    task_set(d.get("task"), "replaced", "同じセッションに次の指示が送られたので、見張りはそちらの件に移りました")
    relay_slack(d, label, "の見張りは、同じセッションへの次の指示に引き継がれました", "", slack_report.SCREEN)


async def _watch_delegation(d: dict):
    label = f"{d['project']}｜{d['title']}"
    generic = d.get("generic", False)  # 送った指示を記録で追わない見張り（承認に答えたあとなど）
    deadline = time.time() + WATCH_TIMEOUT
    told_blocked, saw_working, quiet = False, d["was_working"], 0
    reported: str | None = None  # 最後に報告した返答。None はまだ一度も「完了」を報告していない
    follow_ups = 0
    bg_since: float | None = None
    told_bg = False
    while time.time() < deadline:
        await asyncio.sleep(3 if reported is None else 5)
        if WATCH_GEN.get(d["pane"]) != d["gen"]:
            if reported is None:
                _superseded(d, label)
            return  # 同じセッションに新しい指示が送られた。見張りは最新の1つに任せる（古い件の名前で通知を重ねない）
        a = next((x for x in await asyncio.to_thread(list_agents) if x["pane"] == d["pane"] and x["cwd"] == d["cwd"]), None)
        if d.get("_merged_into"):  # 一覧を読む間に、次の件にまとめられた。ここから先（報告）に進まない（次の回の頭で引き上げる）
            continue
        if not a:  # ハブがユーザーの許可で閉じたものは、知らせない
            if reported is None and d["pane"] in CLOSED:
                task_set(d.get("task"), "closed", f"{AGENT}が閉じました")
                relay_slack(d, label, f"のセッションは、{AGENT}が閉じました", "", slack_report.SCREEN)
            return await report(d, "gone") if reported is None and d["pane"] not in CLOSED else None
        if a["status"] == "working":
            saw_working, told_blocked, quiet = True, False, 0
            if TASKS.get(d.get("task") or "", {}).get("status") in ("blocked", "background"):  # 答えてもらった・裏の作業から戻った
                task_set(d.get("task"), "watching")
            continue
        path = jsonl_path(a)
        has = bool(path and path.exists())
        # Slack への報告が、この件の指示のあとの発言を確かめるのに使う。送ったときと違うセッション（作り直された）なら使わない
        d["path"] = str(path) if has and not generic and a.get("session") and a.get("session") == d.get("session") else None
        read, reply, superseded, pending = delegation_state(path, d["offset"], d["text"]) if has and not generic else (False, "", False, False)
        if reported is not None and superseded:
            return  # 報告のあと、ユーザーが相手に直接次の発言をした。そこから先はユーザー自身の作業なので知らせない
        if a["status"] == "blocked":
            if not told_blocked:
                told_blocked = True
                await report(d, "blocked")
            continue
        quiet += 1
        if reported is not None:
            # 一度報告したあと。相手が自分から再開して続きの返答を出したら、追って知らせる
            if follow_ups >= 5:
                return
            if reply.strip() and reply != reported and not pending:
                follow_ups += 1
                LAST_DONE[d["pane"]] = (last_turn_id(path), reply)
                await report(d, "more")
                reported = reply
            continue
        # 待機に戻っていて、(a) 指示への返答が記録にある、(b) 指示は読まれたが文章の返答が無いまま落ち着いた、
        # (c) 記録では追えないが一度は動いて落ち着いた（作業中に重ねて頼むと相手は1つの返答にまとめる。記録を読めない
        #     種類のセッションもここ）、のどれかなら一区切りとみなす
        if not ((read and reply.strip()) or (read and quiet >= 3) or (not read and saw_working and quiet >= 5)):
            continue
        if pending:
            # ターンは終えたが、裏の作業が残っている（「待機を始めました」など）。ここで「完了」と知らせず、本当の結果を待つ。
            # 長引いたら一度だけ「まだ動いている」と知らせる
            bg_since = bg_since or time.time()
            if not told_bg and time.time() - bg_since >= BG_NOTICE_AFTER:
                told_bg = True
                await report(d, "background")
            continue
        now = (last_turn_id(path), last_exchange(read_lines(path, tail_bytes=600_000))[1]) if has else None
        if now and LAST_DONE.get(d["pane"]) == now:  # 重ねて頼んだ件が、先に報告した返答の中で片づいていた
            hlog(f"報告を省略（先に報告した返答に含まれる）: {label}", flush=True)
            task_set(d.get("task"), "done", "同じセッションへの前の件と、まとめて報告しました")
            relay_slack(d, label, "は、同じセッションへの前の件とまとめて片づきました", "", slack_report.SCREEN)
            return
        if now:
            LAST_DONE[d["pane"]] = now
        await report(d, "done")
        if not read:
            return  # 記録で追えない件は、続きも見分けられないのでここまで
        reported = reply
    hlog(f"見張りを終了（{WATCH_TIMEOUT // 3600}時間）: {label}", flush=True)
    if reported is None and WATCH_GEN.get(d["pane"]) != d["gen"]:  # 最後に待つ間に、次の指示が送られた
        _superseded(d, label)
    elif reported is None:
        task_set(d.get("task"), "timeout", f"{WATCH_TIMEOUT // 3600}時間たっても片づかなかったので、見張りをやめました")
        relay_slack(d, label, f"は {WATCH_TIMEOUT // 3600} 時間たっても片づかなかったので、見張りをやめました", "", slack_report.SCREEN)


TASK_BY_REPORT = {"done": "done", "more": "done", "background": "background", "blocked": "blocked", "gone": "gone"}
PUSH_BY_REPORT = {"done": "完了しました", "more": "続きの返答がありました", "background": "裏で作業を続けています", "gone": "セッションが見つからなくなりました"}


async def report(d: dict, kind: str):
    """頼んだ件の台帳を進め、ハブに報告させ、どの画面も見えていなければプッシュ通知でも知らせる。
    承認・入力待ち（blocked）の通知は watch_blocked が送る（見張りの無いセッションの分もまとめて、二重にしないため）。"""
    if kind in ("done", "more", "gone"):
        d["_closed"] = True  # 完了を報告し始めた件は、次の件にまとめない（最初の await より前に立てる。merge_open と入れ違わない）
    t = TASKS.get(d.get("task") or "")
    if not (t and t["status"] == "done" and TASK_BY_REPORT[kind] != "done"):  # 報告済みの件は、その後の続きの作業で止まっても完了のまま
        task_set(d.get("task"), TASK_BY_REPORT[kind])
    st = {"settled": False, "job": None, "next": None}  # settled = 送った（か、届け先が無い・送れなかった）ので、もう送らない

    async def send(outcome: str):
        tail = {"no_hub": f"（{AGENT}が居ないので、声の報告はありません）", "failed": f"（{AGENT}から報告を受け取れませんでした）",
                "waiting": f"（{AGENT}の報告は、このあと届きます）"}.get(outcome, "")
        while True:
            tid = d.get("topic")  # 件ごとの通知（同じセッションの別の件の通知を上書きしない）。押すとその件を開く
            r = await push_all(f"{d['project']}｜{d['title']}", PUSH_BY_REPORT[kind] + tail,
                               f"topic-{tid}" if topics.ID_RE.fullmatch(tid or "") else f"report-{d['pane']}", url=topic_path(tid))
            if r != "visible":
                st["settled"] = True
                return
            if not st["next"]:  # 画面が見えていて送らなかった。あとでハブの報告ができたとき、もう一度確かめる
                return
            outcome, st["next"] = st["next"], None
            tail = {"no_hub": f"（{AGENT}が居ないので、声の報告はありません）", "failed": f"（{AGENT}から報告を受け取れませんでした）"}.get(outcome, "")

    def notify(outcome: str):
        """1件につき1回送る。ハブの報告ができた時点で（読み上げを待たずに）、送るのは裏で（声を遅らせない）。
        画面が見えていて送らなかったときは、次の機会（30秒の時点 → ハブの報告ができた時点）にもう一度確かめる"""
        if st["settled"] or kind not in PUSH_BY_REPORT:
            return
        if st["job"] and not st["job"].done():
            st["next"] = outcome  # 送っている途中。画面が見えていて送らなかったら、これでもう一度確かめる
            return
        st["job"] = job = asyncio.create_task(send(outcome))
        _tasks.add(job)
        job.add_done_callback(_tasks.discard)

    async def late():  # ハブが話し中・応答が遅いときも、通知は待たせすぎない
        await asyncio.sleep(REPORT_PUSH_WAIT)
        notify("waiting")

    timer = asyncio.create_task(late())
    d["_slack_pending"] = True  # この報告の分を、まだ Slack に送っていない（relay_slack が落とす）
    try:
        outcome = await _report(d, kind, notify)
    except Exception:  # ハブへの報告が例外で終わっても、通知は送る
        notify("failed")
        if chain_dest(d) and d.get("_slack_pending"):  # Slack に送る前に失敗した。送ったあとの失敗なら重ねない
            relay_slack(d, f"{d['project']}｜{d['title']}", "の報告の途中で失敗しました", "", slack_report.SCREEN)
            d["_slack_failed_sent"] = True
        raise
    finally:
        timer.cancel()
    notify(outcome)


def report_about(d: dict) -> str:
    """自動通知の「何についての知らせか」。"""
    if not d["text"]:
        return "ユーザーが答えた承認・質問"
    chain = slack_chain(d)
    if chain:  # 同じセッションに続けて送った指示。報告はまとめて 1 つ（Slack から頼んだ件があるときだけ）
        asks = [*chain, d][-slack_report.ASKS_SHOWN:]
        more = f"ほか {len(chain) + 1 - len(asks)} 件と、" if len(chain) + 1 > len(asks) else ""
        return f"同じセッションに続けて送られた {len(chain) + 1} 件の指示（{more}" + "".join(f"「{_clip(e['text'], 60)}」" for e in asks) + "）"
    return f"あなたが送った指示「{_clip(d['text'], 120)}」"


async def _report(d: dict, kind: str, notify=lambda outcome: None) -> str:
    """ハブに自動通知を送り、ハブの報告を、開いている画面へ声で届ける。画面が無ければ履歴に残るだけ。
    結果は ok / no_hub（ハブが居ない）/ failed（ハブから報告を受け取れなかった）/ skipped（もう止まっていない）。
    notify は、ハブの報告を受け取れた時点で（読み上げる前に）呼ぶ。"""
    global REPORTING, REPORT_SEQ
    label = _clip(f"{d['project']}｜{d['title']}", 80)
    hub = await HUB.report_target()
    # この件の範囲（指示を送った位置から今まで）を読ませ、この指示への答えを、間にあったほかの発言への答えと分けて報告させる。
    # 範囲を持たない件（承認・質問への答え・送ったときのセッションが分からない件）は、今までどおり最後の吹き出しを読ませる
    tid = d.get("task")
    by_task = bool(d["text"] and d.get("session") and d.get("path")) and (tid or "") in TASK_RANGE
    each = "どの指示の結果かが分かるように、" if slack_chain(d) else ""
    done = (f"hubctl read {d['pane']} --task {tid} --full で、この指示を送ったところから今までを読み、{each}この指示への答えを結論から、"
            "判断に要る材料（選択肢・おすすめと理由・数字・期限・うまくいかなかったこと）は略さずに、間にあったほかの発言への答えは混ぜずに（あれば最後に一言で分けて）、"
            if by_task else f"hubctl read {d['pane']} -n 1 --full で結果を確かめ、判断に要る材料（選択肢・おすすめと理由・数字・期限・うまくいかなかったこと）は略さずに、")
    headline, todo = {"done": ("が完了しました", done),
                      "more": ("に続きの返答がありました",
                               f"先ほど報告した件の続きです。hubctl read {d['pane']} --task {tid} --full で、この指示を送ったところから今までを読み、前に報告したあとの続きを、"
                               "判断に要る材料は略さずに、間にあったほかの発言への答えは混ぜずに（あれば最後に一言で分けて）、"
                               if by_task else f"先ほど報告した件の続きです。hubctl read {d['pane']} -n 1 --full で確かめ、判断に要る材料は略さずに、"),
                      "background": ("は裏で作業を続けています", f"しばらくたっても結果が出ていません。hubctl read {d['pane']} で様子を確かめ、まだ動いていることを一言で、"),
                      "blocked": ("が承認・入力待ちで止まっています", f"hubctl screen {d['pane']} で何を聞かれているか確かめ、"),
                      "gone": ("のセッションが見つからなくなりました", "閉じられた可能性があることを、")}[kind]
    if not hub:
        hlog(f"報告できません（ハブが居ない）: {label} {kind}", flush=True)
        relay_slack(d, label, headline, "", ask=kind == "blocked")
        if kind in ("done", "more", "gone"):
            d["_slack_done"] = True
        return "no_hub"
    # 一行・短文にする（長文や改行は「貼り付けられた内容」として包まれる）。結果の本文は通知に載せず、ハブが道具で読む。
    # 「‖」より前は画面にも出す見出し、後ろはハブへの依頼
    about = report_about(d)
    note = (f"{NOTE_PREFIX} {label} {headline} ‖ {todo}ユーザーに声で報告してください。"
            f"（自動通知です。ユーザーの発言ではありません。{about}についての知らせです。"
            "この通知をきっかけに、新しい指示を送ったり承認に答えたりはしません）")
    async with pane_lock(hub["pane"]):
        if kind == "blocked":  # 順番を待つ間にユーザーが答えていたら、もう止まっていない。古い知らせは送らない
            now = next((a for a in await asyncio.to_thread(list_agents) if a["pane"] == d["pane"]), None)
            if not now or now["status"] != "blocked":
                hlog(f"報告を省略（もう止まっていない）: {label}")
                return "skipped"
        saved = HUB.report_begin(note[len(NOTE_PREFIX):].split("‖", 1)[0].strip(), label)  # 見出しと報告を記録に書けたか
        REPORT_SEQ += 1
        seq, state, reply = REPORT_SEQ, "error", ""
        REPORTING = True
        report_idle().clear()
        asked_from = time.time()
        try:
            state, reply = await ask(hub, note)
        finally:
            REPORT_FAILED[seq] = state != "done" or not reply.strip()  # 失敗・時間切れ・空の返事で終わった報告を待っていた送信は、通さない（下の「failed」と同じ判定）
            for old in [k for k in REPORT_FAILED if k < seq - 50]:
                REPORT_FAILED.pop(old, None)
            REPORTING = False
            report_idle().set()
        hlog(f"報告 {kind}: {label} → {state}（画面 {len(CLIENTS)}）", flush=True)
        # 止まった報告は、ハブがこの報告の間に見た問い（画面の指紋）と、そのときのセッションを記録に残す。報告への「↩ 返信」は、
        # その問いがいまも同じセッションに出ているときだけ、その問いへの答えに使える（_hub_answer）。ハブが画面を見ずに書いた報告は結ばない
        seen = SEEN_PROMPT.get(d["pane"]) if kind == "blocked" else None
        ask_link = ({"fp": seen[0], "session": seen[2]}
                    if seen and len(seen) > 2 and seen[2] and seen[2] == now.get("session") and SEEN_AT.get(d["pane"], 0) >= asked_from else None)
        saved = await HUB.report_end(state, reply, label, saved, str(d.get("pane", "")), str(d.get("topic") or ""), ask=ask_link)
        if state != "done" or not reply.strip():
            relay_slack(d, label, headline, "", ask=kind == "blocked")
            if kind in ("done", "more", "gone"):
                d["_slack_done"] = True
            return "failed"
        notify("ok")
        relay_slack(d, label, headline, reply, ask=kind == "blocked")  # 報告は REPORTING の中で作られたもの（その間、ハブは次の指示を送れない）
        if kind in ("done", "more", "gone"):
            d["_slack_done"] = True  # 完了の報告を送った。このあとの見張りの失敗は重ねて知らせない（途中経過のあとの失敗は知らせる）
        clients = list(CLIENTS)
        msg = dict(type="reply", pane=hub["pane"], text=reply, report=True, about=label, saved=saved, topic=str(d.get("topic") or ""))
        ring: list = []
        while clients and not ring:  # 声は 1 か所（最後に話しかけた窓）。その窓に送れなければ（切れていた）、残りから選び直す（codex-review）
            for c in ring_targets(clients):
                try:
                    await c.send(**msg)
                    ring.append(c)
                except Exception:
                    clients.remove(c)
        for c in [c for c in clients if c not in ring]:  # ほかの窓には文字だけ（quiet: 読み上げの印を付けない）
            try:
                await c.send(**msg, quiet=True)
            except Exception:
                clients.remove(c)
        if ring:
            # 読み上げの途中で鳴らす窓が切れても（1 対 1 の speak_to は失敗を投げる）、見張りには伝えない（codex-review）。別の窓では読み直さない:
            # 文字はすべての窓に届いている。読み直すと、文字だけで届けた窓に読み上げの印が付かず（a2a 8f05ef33）、
            # 語り手・合成そのものの失敗では窓の数だけ作り直して、ハブの順番を長く止める（codex-review）
            speech = asyncio.Event()  # 「停止」で止める（どの画面の停止でも。利用者は一人）
            REPORT_SPEECH.add(speech)
            try:
                await speak_to(ring, reply, speech)
            except Exception as e:
                hlog(f"報告の声を送り終えられませんでした（{type(e).__name__}）。文字はすべての窓に届いています", flush=True)
            finally:
                REPORT_SPEECH.discard(speech)
        return "ok"


_UNREADABLE = object()


def _said(content) -> str:
    """発言の中身（文か、文の部品の並び）を 1 つの文に。貼り付けの包みは外す。形が違えば空（どの指示とも一致しない）。"""
    if isinstance(content, str):
        said = content
    elif isinstance(content, list):
        said = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    else:
        return ""
    m = PASTED.match(said)
    return m.group(1) if m else said


def user_side(row: dict):
    """記録の 1 行が利用者の側の行なら、その文。違えば None。形が読めなければ _UNREADABLE。
    作業中に届いた発言（端末に打った文・herdr から送った指示・別のエージェントからの発言）は、user の行でなく、
    queued_command の付け足し（attachment）として残る。裏の作業の完了の知らせ（task-notification）は、Claude Code が出すものなので数えない。
    サブエージェントの行（isSidechain）と、Claude Code が足す行（isMeta・会話の要約の isCompactSummary）も数えない。どれも記録の印で、文の形では見ない。"""
    if row.get("isSidechain") or row.get("isMeta") or row.get("isCompactSummary"):  # Claude Code が立てる印（打ち込んだ文では立たない）
        return None
    kind = row.get("type")
    if kind == "user":
        msg = row.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list) and content and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        return _said(content)
    if kind == "attachment":
        att = row.get("attachment")
        if not isinstance(att, dict):
            return _UNREADABLE
        if att.get("type") != "queued_command":
            return None
        origin = att.get("origin")
        if att.get("commandMode") == "task-notification" or (isinstance(origin, dict) and origin.get("kind") == "task-notification"):
            return None
        return _said(att.get("prompt"))
    return None


def task_range(path: Path, offset: int, texts: list[str]) -> tuple[list[dict], bool, bool]:
    """この件の範囲（最初の指示を送った位置 offset から今まで）を、発言ごとに区切る。(吹き出し, found, mixed) を返す。
    texts は、この件の指示（同じセッションに続けて送ってまとめた件は、古い順に全部。1 件だけのことが多い）。
    吹き出しは {"role": "me", "this": この件の指示か, "queued": 作業中に届いたか, "text"} か、{"role": "ai", "text"}。
    返答は、ターンの途中でも、発言の位置で区切る（作業中に届いた指示は、道具の結果のあと・次の返答の前に記録される）。
    found: この件の指示が全部、その順に記録にある（全文が一致するもの。先頭が同じ別の指示と取り違えない）。
    mixed: この件の指示と返答のほかに、利用者の側の行がある（端末・画面・声から直接の発言・ほかの件の指示・ほかのセッションやエージェントからの知らせ・
           読めない行）か、最初の指示が作業中に届いた（別の作業の返答と同じターンに続く）。
    数えないのは、道具の結果・サブエージェントの行・裏の作業の完了の知らせ・Claude Code が足す行（user_side を見る）。
    画面の履歴（iter_events）と違い、<system-reminder>・<bash-input> などで始まる発言も飛ばさない（端末に打たれた文が、その形で始まっていても見落とさない）。"""
    want = [" ".join(t.split()) for t in texts]
    msgs: list[dict] = []
    seen, mixed = 0, False
    for line in read_lines(path, offset=offset):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            row = None  # 壊れた・書きかけの行
        said = user_side(row) if isinstance(row, dict) else _UNREADABLE
        if said is None:
            if row.get("type") == "assistant" and not (row.get("isSidechain") or row.get("isMeta")):
                msg = row.get("message")
                content = msg.get("content") if isinstance(msg, dict) else None
                parts = [b["text"].strip() for b in content if isinstance(b, dict) and b.get("type") == "text"
                         and isinstance(b.get("text"), str) and b["text"].strip()] if isinstance(content, list) else []
                if parts and msgs and msgs[-1]["role"] == "ai":
                    msgs[-1]["text"] += "\n\n" + "\n\n".join(parts)
                elif parts:
                    msgs.append({"role": "ai", "text": "\n\n".join(parts)})
            continue
        if said is _UNREADABLE:
            mixed = True
            msgs.append({"role": "me", "this": False, "queued": False, "text": "（読めない行）"})
            continue
        queued = row.get("type") == "attachment"
        this = seen < len(want) and " ".join(said.split()) == want[seen]
        if this:
            mixed = mixed or (seen == 0 and queued)  # 2 件目からは、最初の指示のターンの中に届いてよい
            seen += 1
        else:
            mixed = True
        msgs.append({"role": "me", "this": this, "queued": queued, "text": said.strip() or "（空の発言）"})
    return msgs, seen == len(want), mixed


def same_session(d: dict) -> bool:
    """いまその pane に居るのが、この件を送ったときと同じセッション（同じ場所・同じセッション ID・同じ記録）か。
    端末でセッションが作り直されると、ハブが読むのは新しいセッションなので、古い記録の確かめは当てにならない。"""
    a = next((x for x in list_agents() if x["pane"] == d["pane"]), None)
    if not a or a.get("cwd") != d.get("cwd") or not d.get("session") or a.get("session") != d.get("session"):
        return False
    p = jsonl_path(a)
    return bool(p) and str(p) == d.get("path")


def chain_dest(d: dict) -> tuple[str, str] | None:
    """Slack の届け先。この件が Slack から来たならそのスレッド。来ていなければ、まとめた前の件のうち一番新しい Slack のスレッド。
    どれも slack_report.origin が決めた DM の中だけ。"""
    if d.get("slack"):
        return d["slack"]
    return next((e["slack"] for e in reversed(d.get("chain") or []) if e.get("slack")), None)


def slack_chain(d: dict) -> list[dict]:
    """まとめて報告する前の件（古い順）。Slack に届け先があるときだけ。無ければ今までどおり、この件だけを報告する（声と画面だけの件は変えない）。"""
    return list(d.get("chain") or []) if chain_dest(d) else []


def merge_open(d: dict, prev: dict | None):
    """同じセッションへの前の件が、まだ完了の報告を始めていなければ、この件にまとめる（前の件の見張りは、報告せずに引き上げる）。
    まとめた報告の Slack の届け先が別のスレッドに移るときは、前の届け先に「次の依頼のスレッドで報告する」と一言送る。
    同期で呼ぶ（前の件の見張りが報告を始めるのと、まとめるのとが入れ違わないように。report は最初の await より前に _closed を立てる）。"""
    if (not prev or prev.get("_closed") or prev.get("_merged_into") or not prev.get("text")
            or prev.get("cwd") != d["cwd"] or prev.get("session") != d.get("session")):
        return
    d["chain"] = [*prev.get("chain", []), {k: prev.get(k) for k in ("text", "offset", "slack", "overlap", "session", "sent_gen", "topic")}]
    prev["_merged_into"] = d
    old, new = chain_dest(prev), chain_dest(d)
    if old and old != new:
        post_slack(old, slack_report.pointer_text(f"{prev['project']}｜{prev['title']}", d["text"]), f"{d['project']}｜{d['title']}（行き先の知らせ）")


def post_slack(dest: tuple[str, str], text: str, label: str):
    """Slack に裏で送る（声と画面を遅らせない）。"""
    async def go():
        r = await asyncio.to_thread(slack_report.post, dest, text)
        hlog(f"Slack へ報告: {label} → {r}", flush=True)

    job = asyncio.create_task(go())
    _tasks.add(job)
    job.add_done_callback(_tasks.discard)


def relay_slack(d: dict, label: str, headline: str, reply: str, fallback: str = slack_report.NO_REPORT, ask: bool = False):
    """Slack から頼まれた件なら、報告を同じスレッドにも送る（裏で。声と画面の報告を遅らせない）。送り先は slack_report.origin が決めた DM だけ。
    同じセッションに続けて頼んでまとめた件（chain）は、一番新しい Slack のスレッドに 1 回だけ送り、まとめた依頼を並べる。
    ハブの報告（reply）は、ハブが hubctl read --task でこの件の範囲を読み分けて書いたもの。本文を載せないのは、この件の記録を読めないときだけ
    （送ったときと別のセッションになった・記録が無い・読めない・送った指示が記録に見つからない）。そのときは定型文にする。
    この件の範囲に、ほかのやり取りが混ざったら、本文の頭に注意の 1 行を付ける（ハブが別の件の答えと取り違えていたら、あなたが気づけるように）:
    - セッションの記録で、この件の指示のほかに利用者の側の行がある（端末・画面・声から直接の発言・ほかの件の指示・ほかのセッションやエージェントからの知らせ・読めない行）か、
      最初の指示が作業中に届いた（別の作業の返答と同じターンに続く）
    - 報告を待つ間に、同じセッションへ次の指示が送られようとした（SENT_GEN。送る前に進む・増えるだけ）か、送る途中でほかの指示と重なった（overlap）か、
      まとめた依頼の間に、画面・声から直接の発言があった（SENT_GEN が続いていない）
    末尾に、この件を画面で開くリンクを付ける（ask: 承認・入力待ちなら「画面でこの問いに答える」）。取り返せない答えは、Slack ではなく画面と声で受ける"""
    dest = chain_dest(d)
    d["_slack_pending"] = False
    if not dest:
        return
    chain = slack_chain(d)
    asks = [(e["text"], slack_report.HERE if e.get("slack") == dest else slack_report.OTHER_THREAD if e.get("slack") else slack_report.FROM_SCREEN)
            for e in [*chain, d]] if chain else ()
    quiet = SENT_GEN.get(d["pane"]) == d.get("sent_gen") and not d.get("overlap")  # 報告を待つ間に、同じセッションへほかの送りが無い
    if chain:
        gens = [e.get("sent_gen") for e in chain] + [d.get("sent_gen")]
        quiet = (quiet and not any(e.get("overlap") for e in chain)
                 and all(isinstance(g, int) for g in gens) and gens == list(range(gens[0], gens[0] + len(gens))))
    one_session = not any(e.get("session") != d.get("session") for e in chain)  # まとめた依頼が同じセッションの記録にある（まとめるときにも確かめている）

    async def go():
        link = await asyncio.to_thread(topics.link, d.get("topic"))
        tail = {"link": link or "", "ask": ask}
        text = slack_report.text_for(label, headline, "", fallback, asks, **tail)
        if reply.strip():
            text = slack_report.text_for(label, headline, "", slack_report.NOT_FOUND, asks, **tail)
            path = d.get("path")
            try:
                first = chain[0]["offset"] if chain else d["offset"]
                texts = [e["text"] for e in chain] + [d["text"]]
                if path and one_session and await asyncio.to_thread(same_session, d):
                    _msgs, found, mixed = await asyncio.to_thread(task_range, Path(path), first, texts)
                    if found:
                        text = slack_report.text_for(label, headline, reply, asks=asks, note=slack_report.MIXED if mixed or not quiet else "", **tail)
            except Exception:  # 読めない記録（消えた・読めない）は、本文を載せずに定型文を送る
                hlog(f"Slack への報告: 記録を読めません（{label}）", flush=True)
        if d.get("_merged_into"):  # 次の件にまとめた件は、自分では送らない（途中経過も。報告は次の件がまとめてする）。送る直前に見る（記録を確かめる間にまとめられることもある）
            return
        r = await asyncio.to_thread(slack_report.post, dest, text)
        hlog(f"Slack へ報告: {label} → {r}", flush=True)

    job = asyncio.create_task(go())
    _tasks.add(job)
    job.add_done_callback(_tasks.discard)


def track_send(pane: str, coro) -> asyncio.Future:
    """セッションへ送る処理を、終わるまで INFLIGHT に数える。待っている側が取り消されても（画面の「止める」・切断）、
    送る処理は裏で続くので、数を戻すのは処理そのものが終わったとき。待つ側は asyncio.shield で待つ。"""
    INFLIGHT[pane] = INFLIGHT.get(pane, 0) + 1
    fut = asyncio.ensure_future(coro)
    fut.add_done_callback(lambda _f: INFLIGHT.__setitem__(pane, INFLIGHT[pane] - 1))
    return fut


async def dispatch(a: dict, text: str, kind: str = "send", slack: tuple[str, str] | None = None) -> dict:
    """セッションに指示を送り、片づくまでの見張りを始める（頼んだ件の台帳にも載せる）。
    slack は、Slack から頼まれたときの (DM, スレッド)。片づいたときの報告を、そこにも送る。"""
    pane, project = a["pane"], a["cwd"].rsplit("/", 1)[-1]
    path = jsonl_path(a)
    offset = path.stat().st_size if path and path.exists() else 0
    SENT_GEN[pane] = sent_gen = SENT_GEN.get(pane, 0) + 1  # 送る前に進める。前の件の Slack への報告は、ここから先、結果の本文を送らない
    overlap = INFLIGHT.get(pane, 0) > 0  # ほかの指示を送っている途中。届く順が送り始めた順と逆になりうるので、この件の Slack への報告にも本文を載せない
    r = await asyncio.shield(track_send(pane, asyncio.to_thread(herdr, "agent", "prompt", pane, text, timeout=30)))
    err = (r.get("error") or {}).get("code") or (None if "result" in r else "herdr が応答しません")
    hlog(f"send → {pane} ({project}): {text[:120]!r} {'ERR ' + err if err else 'ok'}", flush=True)
    if err:  # unknown: herdr がはっきり断ったのでなく、応答が無かった（届いたかどうか分からない）
        return {"ok": False, "reason": err, "unknown": not isinstance(r.get("error"), dict)}
    WATCH_GEN[pane] = WATCH_GEN.get(pane, 0) + 1  # このセッションの、これより前の見張りは引き上げさせる
    d = {"pane": pane, "cwd": a["cwd"], "project": project, "title": a["title"], "text": text, "offset": offset,
         "was_working": a["status"] == "working", "gen": WATCH_GEN[pane], "sent_gen": sent_gen, "overlap": overlap, "slack": slack,
         "session": a.get("session")}
    merge_open(d, OPEN_WATCH.get(pane))  # 前の件がまだ報告前なら、この件にまとめる（ここから start_watch までは await しない）
    d["topic"] = topic_for(d, keep_direct=SENT_GEN.get(pane) != sent_gen)  # 送り終えるのを待つ間に、画面・声から直接送られていた（印を残す）
    OPEN_WATCH[pane] = d
    start_watch(d, kind)
    if slack_chain(d):
        task_set(d.get("task"), "watching", f"同じセッションへの前の {len(slack_chain(d))} 件の指示と、まとめて報告します")
    return {"ok": True, "project": project, "title": a["title"], "was_working": a["status"] == "working", "task": d["task"], "slack": bool(slack)}


def topic_for(d: dict, keep_direct: bool = False) -> str:
    """この依頼の件を決める（2026-11-19 本人が A を選んだ: 続きなら同じ件、それ以外は新しい件）。続きは、次の順に見る:
    1. まだ報告の来ていない同じセッションへの前の依頼にまとめた（merge_open。#54 の報告のまとめと同じ）→ その件
    2. Slack の同じスレッドから頼まれた（スレッドの無い、DM の一番上の依頼は続きと見なさない）→ そのスレッドの件
    3. 画面の「↩ 返信」で報告に返した発言を、ハブが扱っている間の依頼（Slack から頼まれた依頼は除く）→ その報告の件
    どれでもなければ新しい件。続きにするのは、前の件と同じセッション（空でないセッション ID が同じ）への依頼だけ。Slack のスレッドと返信中かは
    ハブ（乗せられうる Hermes）の申告に頼るので、別のセッションへの依頼を前の件に結ぶと、前の件のリンク（Slack・通知）の行き先を別のセッションの
    問いに書き換えられてしまう。続きの件は、Slack のスレッドを新しいほうに置き換える。同期で呼ぶ（dispatch の中、await しない）。
    keep_direct: この依頼を送り終えるのを待つ間に、同じセッションへ画面・声から直接送られていた。直接送った印を外さない（新しい件にも付ける）"""
    pane, session, slack = d["pane"], d.get("session") or "", d.get("slack")
    for tid in (next((e.get("topic") for e in reversed(d.get("chain") or []) if e.get("topic")), None),
                topics.by_slack(slack, session) if slack and session else None,
                HUB.reply_topic() if not slack else None):
        t = topics.get(tid)
        if t and session and t["session"] == session and topics.touch(tid, pane, session, slack, clear_direct=not keep_direct):
            if keep_direct:  # 続きの件（いま一番新しい件になった）にも、はっきり印を付ける（あとの件に付いた印だけでは、この件が印なしになる）
                topics.mark_direct(session)
            return tid
    tid = topics.new(pane, session, d["project"], d["text"] or f"{d['project']}｜{d['title']}", slack)
    if keep_direct:
        topics.mark_direct(session)
    return tid


def report_idle() -> asyncio.Event:
    global REPORT_IDLE
    if REPORT_IDLE is None:
        REPORT_IDLE = asyncio.Event()
        REPORT_IDLE.set()
    return REPORT_IDLE


async def wait_report_if_slack(body: dict) -> bool:
    """報告中なら、Slack の DM から来た送信（hubctl send）だけ、報告が終わるまで順番を待つ（上限 SLACK_QUEUE_WAIT 秒）。送ってよければ True。
    声・画面からの送信は報告と同じ順番待ちを通るので、ここで報告中なのは、報告を書いているハブ自身か、voice-loop の順番待ちを通らない
    Slack の会話だけ。ハブ自身が「Slack から」と偽っても、自分が書いている報告はその送信が返るまで終わらないので、上限まで待って断られる。
    待っている間の報告が失敗・時間切れで終わったときも断る。
    限り: 報告中のハブが送信を裏で出して（または時間をずらして）から報告を終えれば、送信は報告のあとに通る。これは REPORTING が
    もともと要求の来た瞬間だけを見る守り（報告の流れでうっかり送るのを止める）だからで、この関数で新しく開く穴ではない。"""
    if not REPORTING:
        return True
    if not slack_report.origin(body.get("origin")):
        return False
    first, deadline = REPORT_SEQ, time.monotonic() + SLACK_QUEUE_WAIT
    while REPORTING:  # 報告が続けて来ても、終わったすきに送る
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        try:
            await asyncio.wait_for(report_idle().wait(), timeout=left)
        except asyncio.TimeoutError:
            return False
    # 待っている間の報告のどれかが失敗・時間切れで終わっていたら、通さない（報告中のハブが偽った送信が、報告の失敗で通らないように）
    return not any(REPORT_FAILED.get(n, True) for n in range(first, REPORT_SEQ + 1))


@app.post("/api/hub/send")
async def hub_send(body: dict):
    """指示を送る。完了は待たず、片づいたら（または止まったら）ハブに通知して報告させる。"""
    if not await wait_report_if_slack(body):  # 通知への応答の中から、次の指示を出させない（ユーザーの知らないところで連鎖するのを防ぐ）
        return JSONResponse({"ok": False, "reason": "reporting"})
    pane, text = str(body.get("pane", "")), " ".join(str(body.get("text", "")).split())
    a = await asyncio.to_thread(_worker, pane)
    if not a:
        return JSONResponse({"ok": False, "reason": f"セッションが見つかりません: {pane}"}, status_code=404)
    if not text:
        return JSONResponse({"ok": False, "reason": "指示が空です"}, status_code=400)
    if len(text) > PROMPT_MAX:
        return JSONResponse({"ok": False, "reason": "too_long", "length": len(text), "max": PROMPT_MAX})
    if a["status"] == "blocked":  # 承認や入力を待っている。herdr も送信を拒否する
        return JSONResponse({"ok": False, "reason": "blocked", "screen": read_prompt(pane)[-1200:]})
    return JSONResponse(await dispatch(a, text, slack=slack_report.origin(body.get("origin"))))


# ---------- 承認・質問への回答（ハブは決めない。ユーザーの答えを相手に伝えるだけ） ----------
# 取り消しにくい操作に見える語。当たったら、ユーザーにもう一度確かめてからでないと「はい」を通さない
RISKY = re.compile(r"(\brm\b|\btrash\b|delete|\bdrop\b|truncate|\bpush\b|deploy|publish|release|\bsend|mail|merge|reset --hard|force|terraform|\baws\b|gcloud|kubectl|migrat|prod|本番|削除|送信|公開|配信|反映|支払|振込|請求)", re.I)
# 取り消しにくい操作の種類ごとに、画面の語（RISKY の語を分けたもの）と、ハブが問いの「〇〇してよいですか」の〇〇で名指しする語
# 名指しの語は、この語そのもの（完全一致）だけ（「非公開化」の中の「公開」のような、打ち消し・逆の操作を含む語は数えない。codex-review）
RISKY_KINDS = (
    (re.compile(r"\brm\b|\btrash\b|delete|\bdrop\b|truncate|削除", re.I), re.compile(r"削除|消去")),
    (re.compile(r"\bsend|mail|送信|配信", re.I), re.compile(r"送信|配信|送付|返信|投稿")),
    (re.compile(r"\bpush\b|deploy|publish|release|merge|prod|本番|公開|反映|terraform|\baws\b|gcloud|kubectl|migrat", re.I),
     re.compile(r"反映|公開|デプロイ|リリース|プッシュ|マージ|適用|移行")),
    (re.compile(r"reset --hard|force", re.I), re.compile(r"上書き|強制|リセット")),
    (re.compile(r"支払|振込|請求"), re.compile(r"支払|振込|請求")),
)


def risky_named(asked: str, heard: str) -> bool:
    """画面の問い asked に出ている取り消しにくい操作の種類が、どれも、利用者が聞いた問い heard の「〇〇してよいですか」の〇〇で名指しされているか
    （画面が rm なら「削除してよいですか」。別の種類の名指し・返答のどこかに語があるだけ・プロジェクト名や題の語は数えない）。
    名前と URL を除くと種類が 1 つも残らないときは、名指しされていないとみなす（聞き返す）。"""
    acts = consent.asked_acts(heard)
    op = re.sub(r"https?://\S+", " ", QUOTED.sub(" ", asked))  # 種類は、引用符で囲まれた名前と URL を除いた操作の部分から読む（名前の release・prod などを操作と数えない。codex-review）
    kinds = [act for screen, act in RISKY_KINDS if screen.search(op)]
    return bool(kinds) and all(any(act.fullmatch(w) for w in acts) for act in kinds)


SEEN_PROMPT: dict[str, tuple] = {}  # ハブが見た、止まっている画面: pane → (画面の指紋, 最初に見たときのユーザーの最後の発言, そのときのセッション)


def seen_same(seen: tuple | None, fp: str, session: str) -> bool:
    """ハブが見た問い seen が、いまの問い（指紋 fp・セッション session）と同じか。セッションの分からない記録（古い形）は同じとみなさない。"""
    return bool(seen) and seen[0] == fp and len(seen) > 2 and seen[2] == session
SEEN_AT: dict[str, float] = {}  # ハブが止まっている画面を最後に見た時刻: pane → 時刻（止まった報告が、どの問いを見て書かれたかを結ぶ）
CONFIRMING: dict[str, tuple[str, str | None]] = {}  # 取り消しにくい操作の聞き返し中: pane → (画面の指紋, そのときのユーザーの最後の発言)
CONFIRM_WAIT = 3.0  # 番号の無い確認で、カーソルが Yes に来たのを確かめるまで待つ秒数


async def confirm_yes(pane: str, fp0: str, still=None) -> tuple[bool, str]:
    """番号の無い確認（parse_confirm）で Yes を選ぶ。初めに読んだ画面が答えを決めた画面（指紋 fp0）と同じで、カーソルが Yes に無ければ
    ↓か↑を 1 回送り、画面を読み直して、同じ問いでカーソルが Yes に来たのを確かめてから Enter を送る。
    思った画面でなければ、そこで止める（Enter は送らない）。返すのは (Enter まで送れたか, 止めた理由)。
    still は、キーを送る直前ごとに呼ぶ確かめ（答えに使う発言のあとに利用者が止めた・次の発言をしたら False）。False なら送らずに (False, "stopped")。"""
    moved = False  # カーソルのキーを送ったか（止めたとき、キーを 1 つも送っていなければ "stopped"、送ったあとなら "stopped:moved"）
    text = await asyncio.to_thread(read_prompt, pane)
    c = parse_confirm(text)
    if prompt_fp(text) != fp0 or not c:
        return False, "答える前に画面が変わった（何も送っていない）"
    yes = c["order"].index("Yes")
    if c["cursor"] != yes:
        # カーソルを動かしたあとの画面は、選択肢の 2 行のカーソルだけが入れ替わり、ほかは 1 字も変わらないはず（道具の見出しの ⏺ の点滅だけは数えない）
        want = confirm_norm(confirm_text(text, yes))
        if still is not None and not still():  # 送る直前（ここから送るまで待ちを挟まない）
            return False, "stopped"
        r = await asyncio.to_thread(_if_still, still, herdr, "agent", "send-keys", pane, "down" if yes > c["cursor"] else "up")
        if r is None:
            return False, "stopped"
        moved = True
        if "result" not in r:
            return False, "herdr が応答しない（Enter は送っていない）"
        end = time.monotonic() + CONFIRM_WAIT
        while True:
            t2 = await asyncio.to_thread(read_prompt, pane)
            c2 = parse_confirm(t2)
            if c2 and c2["question"] == c["question"] and c2["order"] == c["order"] and c2["cursor"] == yes and confirm_norm(t2) == want:
                break
            if time.monotonic() >= end:
                return False, "カーソルが Yes に来たのを確かめられない（Enter は送っていない）"
            await asyncio.sleep(0.2)
    if still is not None and not still():  # Enter を送る直前（読み直し・待ちのあいだに止めたら送らない）
        return False, "stopped:moved" if moved else "stopped"
    r = await asyncio.to_thread(_if_still, still, herdr, "agent", "send-keys", pane, "enter")
    if r is None:
        return False, "stopped:moved" if moved else "stopped"
    return ("result" in r), ("" if "result" in r else "herdr が応答しない（Enter が届いたか分からない）")


def asked_part(text: str) -> str:
    """画面のうち、いま聞かれている部分（最後のツール呼び出しの行から下）。それより上の会話は判定に使わない。
    ツール呼び出しの行は、字下げなしで印（⏺）から始まる行。コマンドの本文や結果の行は字下げされるので、中に印を書かれても区切りにしない。
    実行を待つ道具の行の印は点滅し、消えている間は見つからない。そのときは 1 つ前の印の行から、印の行が 1 つも映っていなければ
    映っている全部を返す（広くなることはあっても、狭くはしない。狭くなると、取り消しにくい語を見落とす）。"""
    lines = text.splitlines()
    start = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].startswith("⏺")), 0)
    return "\n".join(lines[start:])


TOOL_MARK = re.compile(r"^⏺", re.M)  # 字下げなしの行の頭の印（発言と、道具の見出し）


def prompt_fp(text: str) -> str:
    """止まっている画面の指紋。承認の画面では、実行を待つ道具の見出しの行の印（⏺）が点滅し、消えている間は空白になる。
    印のあるなしで指紋が変わらないよう、承認の画面に限って、字下げなしの行の頭の印だけは数えない（承認の枠の中の行は、必ず字下げされる）。
    字下げされた行（コマンドの本文など）の印と、印より後ろの文字は、1 字でも違えば別の指紋になる。
    選択式の質問などの画面は、問いの文が字下げなしで出るので、印も文字として数える。"""
    if parse_confirm(text):  # 番号の無い確認は、道具の見出しの ⏺ の点滅と、カーソルの位置（答える直前に別に確かめる）では変わらない。
        # 承認の判定より先に見る（上の会話に番号つきの Yes/No が残っていると、承認の画面とも読めてしまう。read_ask と同じ順。codex-review）。
        # ほかの字・空白・行の区切りは潰さずに数える（a2a）
        return hashlib.sha1(("confirm\n" + confirm_norm(confirm_text(text, None))).encode()).hexdigest()
    if is_permission(text):
        text = TOOL_MARK.sub("", text)
    return hashlib.sha1(" ".join(text.split()).encode()).hexdigest()


def hub_human_turn() -> str | None:
    return next((turn for turn, _ in hub_turns()), None)


@app.post("/api/hub/answer")
async def hub_answer(body: dict):
    """_hub_answer を、画面のボタンの返答（_answer）と同じ鍵の中で動かす。確かめてからキーを送るまでが、2 つの経路で重ならないように
    （一方が答えて次の画面（承認など）に変わった直後に、もう一方が古い問いのつもりで番号を送らない。2026-11-05 Codex）。"""
    async with _answer_lock:
        return await _hub_answer(body)


async def _hub_answer(body: dict):
    """止まっているセッションの問いに、ユーザーの答えを伝える。
    守ること: ハブが見てユーザーに伝えた問いにだけ答える（見たのと同じ画面で、かつ見たあとにユーザーが発言している）。
    「はい」と番号は、ユーザーがその発言の直前に聞いたハブの返答が、プロジェクト名を挙げた問いで（not_asked）、
    発言全体が、その答えの文法に収まるときだけ（not_said。scripts/consent.py）。断る（いいえ）は、まだ答えていない問いになら
    順番を問わず送れる（答えたばかりの同じ問いには、画面のボタンからの答えも含めて ANSWERED_HOLD 秒送らない）。
    取り消しにくい操作の「はい」は、聞き返してユーザーがもう一度発言してから（どちらの発言も上の確かめを通ること）。「今後は聞かずに許可」は選ばない。"""
    if REPORTING:
        return JSONResponse({"ok": False, "reason": "reporting"})
    pane, answer, confirmed = str(body.get("pane", "")), str(body.get("answer", "")).strip().lower(), bool(body.get("confirmed"))
    if not await hub_settled():  # いまの発言の画面が同期の合図を返すまで待つ（相手と問いを読む前に。待つ間に問いが進んでも、読むのはそのあと）
        return JSONResponse({"ok": False, "reason": "stopped"})
    a = await asyncio.to_thread(_worker, pane)
    if not a:
        return JSONResponse({"ok": False, "reason": f"セッションが見つかりません: {pane}"}, status_code=404)
    # 止まっていないかを、機械からの発言かより先に返す。返事の中で文で聞いて入力を待っているだけのセッション（「A で進めますか？」）への返事は、
    # 答えではなくふつうの指示（hubctl send。機械からでも送れる）。機械を先に返すと、ハブが「画面の承認のボタンで」と、無いボタンへ案内していた（2026-11-24）
    if a["status"] != "blocked":
        return JSONResponse({"ok": False, "reason": "not_blocked", "status": a["status"]})
    if await asyncio.to_thread(hub_via, hub_human_turn()) == "device":  # 機械（スタックチャン）からの発言は答えに使わない。答えは画面で（README「守り」）。早めに断るだけで、確かめの本番は下の発言の取り直しのあと
        return JSONResponse({"ok": False, "reason": "device"})
    trust = await asyncio.to_thread(pane_shows_trust, pane)
    if trust is None:  # 何を聞かれているか読めないまま、答えを送らない
        return JSONResponse({"ok": False, "reason": "unreadable"})
    if trust:  # フォルダを信頼するかは、利用者が端末で答える
        return JSONResponse({"ok": False, "reason": "trust"})
    text = await asyncio.to_thread(read_prompt, pane)
    (turn, utter), = (await asyncio.to_thread(hub_turns, 1) or [(None, "")])[:1]
    if await asyncio.to_thread(hub_via, turn) == "device":  # 答えに使うこの発言そのもので確かめる（入口で見たあとに、機械からの発言が入れ替わりで届いても使わない。a2a 356c8a73）
        return JSONResponse({"ok": False, "reason": "device"})
    if not await asyncio.to_thread(hub_current, turn):  # この発言のあとに利用者が止めた・次の発言をした（頭が回の途中で答えを送らない。a2a 30fd5b94）
        return JSONResponse({"ok": False, "reason": "stopped"})
    fp, prompt, project = prompt_fp(text), read_ask(text), a["cwd"].rsplit("/", 1)[-1]
    conf = prompt["kind"] == "confirm"  # 番号の無い「Yes / No」の確認（はい・いいえだけで答える）
    if answer in ("no", "n"):
        key, said = "esc", "いいえ（取り消し）"
    else:
        seen = SEEN_PROMPT.get(pane)
        if not seen_same(seen, fp, a.get("session") or ""):  # 見ていない、または見たあとで問いが変わった（同じ pane でセッションを作り直した、も含む）
            SEEN_PROMPT[pane] = (fp, turn, a.get("session") or "")
            return JSONResponse({"ok": False, "reason": "unseen", "screen": text})
        if seen[1] == turn:  # 見たのと同じ発言の中で答えようとしている = ユーザーはまだ中身を聞いていない
            return JSONResponse({"ok": False, "reason": "same_turn", "screen": text})
        if answer not in ("yes", "y") and not answer.isdigit():
            return JSONResponse({"ok": False, "reason": "bad_answer"})
        if prompt["kind"] == "unknown" and not conf:  # 問いの形を読めない（番号の選択肢も、番号の無い Yes / No も無い）。何を選ぶことになるか分からないので送らない
            return JSONResponse({"ok": False, "reason": "unknown_prompt", "screen": text})
        if answer in ("yes", "y") and prompt["kind"] != "permission" and not conf:
            return JSONResponse({"ok": False, "reason": "not_permission", "options": prompt["options"]})
        if answer.isdigit() and (prompt["kind"] != "question" or int(answer) not in prompt["options"]):
            return JSONResponse({"ok": False, "reason": "bad_choice", "kind": "confirm" if conf else prompt["kind"], "options": prompt["options"]})
        heard = await asyncio.to_thread(hub_heard, turn)
        rp = await asyncio.to_thread(hub_reply_ask, turn)
        if rp is not None and not (rp.get("pane") == pane and rp.get("fp") == fp and rp.get("session") and rp.get("session") == (a.get("session") or "")):
            # 返信の発言は、返信先の報告が「いまこのセッションに出ているこの問い」を見て書かれたものなら、その問いへの答えに使う（2026-11-19 本人）。
            # 別の問い（同じセッションが先へ進んだ）・別のセッション（同じ pane で作り直した・同じプロジェクトの別のセッション）・
            # どの問いを見て書いたか分からない報告（止まった報告でない・ハブが画面を見ずに書いた・前の版の報告）への返信は使わない
            return JSONResponse({"ok": False, "reason": "reply", "project": project, "screen": text})
        if not consent.asks(heard, project):  # ユーザーは、この相手の問いだと分かる文を聞いていない
            return JSONResponse({"ok": False, "reason": "not_asked", "project": project, "screen": text})
        if not (consent.affirms(utter, "answer", project, heard) if answer in ("yes", "y") else consent.chooses(utter, int(answer), prompt["options"], project)):
            return JSONResponse({"ok": False, "reason": "not_said", "said": _clip(utter, 80), "project": project})
        if answer in ("yes", "y"):
            # 取り消しにくい操作は、聞き返してからにする。ただし、番号の無い確認の問いの文に引用符で囲まれた対象の名前があり（Permanently delete "〇〇"?）、
            # 利用者が答えた問い（直前に聞いた文）が、その名前をそのまま入れて、画面と同じ種類の操作を「〇〇してよいですか」で名指ししていたら
            # （「『〇〇』を削除してよいですか？」）、その答えを確かめとする（2026-11-19 本人。security-review を受けて、対象も照らす形にした）。
            # 対象を読み取れない画面（Bash のコマンドなど）・名前を言い換えた問い・別の種類の名指しでは省かない
            # 名前と操作は、同じ 1 つの問いの文に入っていること（説明の文で名前に触れ、問いでは別の対象を聞いた・問いが 2 つ以上でどれへの答えか
            # 分からない、では省かない。codex-review）
            asked = asked_part(text)
            target = confirm_targets(text) if conf else []
            # 返答の問いがちょうど 1 つで、その文が「（〇〇で、）『名前』（のページ）を（完全に）△△してよいですか？」の形そのもので、名前が画面の
            # 対象の名前（1 つだけ）と完全に一致するときだけ（部分一致・名前を含む別の名前・除く言い方では省かない。security-review・codex-review）
            q1 = consent.asked_target(heard)
            if RISKY.search(asked) and not (len(target) == 1 and q1 and consent.same_name(q1[1], target[0]) and risky_named(asked, heard)):
                held = CONFIRMING.get(pane)
                if not (confirmed and held and held[0] == fp and held[1] != turn):
                    CONFIRMING[pane] = (fp, turn) if not (held and held[0] == fp) else held
                    return JSONResponse({"ok": False, "reason": "confirm", "screen": text})
            key, said = ("confirm", "はい（Yes を選んだ）") if conf else ("1", "はい（今回だけ許可）")
        else:
            n = int(answer)
            key, said = str(n), f"{n}番「{prompt['options'][n]}」"
    if answered(pane, fp):  # 画面のボタンと共有する「答えた問い」。答えたあと画面が書き換わるまでに、同じ問いへもう一度送らない
        return JSONResponse({"ok": False, "reason": "answered"})
    # キーを送る直前に、答えに使う発言のあとに利用者が止めた・次の発言をしていないかを、もう一度確かめる（入口で確かめたあとの読み取りの
    # 待ちの間に止めることがある。ここから送るまで待ちを挟まない。確認の段は、キーを送る直前ごとに確かめる。a2a 30fd5b94 の 3 回目）
    # 同期の合図は入口で待った。途中で次の発言が入り、その画面がまだ返していなければ断る（ここでは待たない）
    if not settled_now() or not hub_current(turn):
        return JSONResponse({"ok": False, "reason": "stopped"})
    if key == "confirm":
        prev = ANSWERED.get(pane)
        mark_answered(pane, fp)  # カーソルを動かし始めたら、同じ画面に重ねて送らない（途中で止めても）
        mine = ANSWERED.get(pane)
        ok, why = await confirm_yes(pane, fp, still=lambda: hub_current(turn))
        hlog(f"answer → {pane} ({project}): {said} {'ok' if ok else '止めました: ' + why}", flush=True)
        if not ok:
            if why == "stopped" and ANSWERED.get(pane) is mine:  # キーを 1 つも送らずに止めた。答えた印を外す（画面のボタン・ハブの断りを 15 秒止めない。codex-review fb69347）
                if prev:
                    ANSWERED[pane] = prev
                else:
                    ANSWERED.pop(pane, None)
            return JSONResponse({"ok": False, "reason": "stopped" if why.startswith("stopped") else "stuck", "why": why})
    else:
        r = await asyncio.to_thread(_if_still, lambda: hub_current(turn), herdr, "agent", "send-keys", pane, key)
        if r is None:  # スレッドの空きを待つ間に止めた・次の発言をした（送っていない）
            return JSONResponse({"ok": False, "reason": "stopped"})
        hlog(f"answer → {pane} ({project}): {said} {'ok' if 'result' in r else 'ERR'}", flush=True)
        if "result" not in r:
            return JSONResponse({"ok": False, "reason": "herdr が応答しません"})
        mark_answered(pane, fp)
    SEEN_PROMPT.pop(pane, None)
    CONFIRMING.pop(pane, None)
    if not WATCHING.get(pane):  # ハブ経由で頼んだ件でなければ見張りが無いので、答えたあとの結果を知らせるために付ける
        path = jsonl_path(a)
        start_watch({"pane": pane, "cwd": a["cwd"], "project": project, "title": a["title"], "text": "", "generic": True,
                     "offset": path.stat().st_size if path and path.exists() else 0, "was_working": True,
                     "gen": WATCH_GEN.setdefault(pane, 0), "topic": topics.current_for_session(a.get("session") or "")}, "answer")  # 答えた問いは、そのセッションの一番新しい件のもの（直接送った別の話のあとなら、どの件にも結ばない）
    return JSONResponse({"ok": True, "project": project, "title": a["title"], "said": said})


# ---------- 画面のボタンからの返答（利用者がタップで答える） ----------
# ボタンを出して答えられるのは、Claude Code の承認の画面と言い切れるときと、問いが 1 つで 1 つを選ぶ質問と言い切れるときだけ。
# 画面の文字の読み取りは揺れるので、それ以外（複数選択・問いが複数・preview 付きの質問・フォルダの信頼・読めない・わからない・
# セッション ID が無い）は中身を見せるだけで、キーは送らない
QUESTION_ONLY = ("Type something.", "Chat about this")  # 選択式の質問（AskUserQuestion）にだけある選択肢
QUESTION_CHIP = re.compile(r"^\s*(?:←\s+)?☐ ")  # 質問の見出しの印（複数選択・問いが複数のときは「←  ☐ 朝  ☐ 夜  ✔ Submit  →」）


def question_head(lines: list[str]) -> int | None:
    """末尾の操作案内から上へたどって、いちばん近い質問の見出しの行。"""
    return next((i for i in range(len(lines) - 2, -1, -1) if QUESTION_CHIP.match(lines[i])), None)


def dialog_part(text: str) -> str | None:
    """質問の画面のうち、見出しの印から下。印が無ければ None。"""
    lines = text.split("\n")
    head = question_head(lines)
    return None if head is None else "\n".join(lines[head:])


def is_permission(text: str) -> bool:
    """Claude Code の承認の画面か。parse_prompt の「1 が Yes で No もある」に加えて、承認の画面にしか無い形を求める
    （「Do you want to …」の行・最後の選択肢が No・質問の選択肢と操作案内（Enter to select）が無い）。
    選択肢が Yes と No の選択式の質問を、承認と取り違えないため（2026-10-21 実画面で確認）。"""
    opts = parse_prompt(text)["options"]
    if not opts or opts.get(1, "").lower() != "yes" or not opts[max(opts)].lower().startswith("no"):
        return False
    if any(v in QUESTION_ONLY for v in opts.values()):
        return False
    asked = asked_part(text).splitlines()
    foot = next((l for l in reversed(asked) if "Esc to cancel" in l), "")
    return "Enter to select" not in foot and any(l.strip().startswith("Do you want to") for l in asked)


STEPPING: dict[str, float] = {}  # 1 問ずつ答えている途中のセッション: pane → 最後に押した時刻
STEPPING_FOR = 600  # その間は、herdr が止まっていると見ていなくても画面を読む（確認の画面は、herdr が止まっていると見ないことがある）


def stepping(a: dict) -> bool:
    return time.time() - STEPPING.get(a["pane"], 0) < STEPPING_FOR


def _ask_of(a: dict) -> dict:
    """止まっているセッション a の問い。kind は permission（ボタンを出す）/ question / trust / unknown / none（止まっていない）/ unreadable。
    選択肢の問い（AskUserQuestion）は、セッション ID があり、いまの 1 問（か確認の画面）を ask_form.step で読めたときだけ step（1 問ずつ
    答えるボタン）を付ける。読めない画面には答え方を付けない（前の「選択肢の番号だけを送る」答え方は 0.26.1 で外した。step が断る画面にも
    番号を送れてしまうため。答え方は 1 つ）。
    1 問ずつ答えている途中のセッション（stepping）は、herdr が止まっていると見ていなくても、選択肢の問いの画面かを読む。"""
    if a["status"] != "blocked":
        if not (stepping(a) and a.get("session")):
            return {"kind": "none"}
        try:
            dtext = read_dialog(a["pane"])
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return {"kind": "none"}
        st = ask_form.step(dtext)
        if st["stage"] not in ("question", "review"):
            return {"kind": "none"}
        return {"kind": "question", "text": _clip(ask_form.dialog(dtext), 1200), "fp": prompt_fp(read_prompt(a["pane"])),
                "risky": bool(RISKY.search(ask_form.dialog(dtext))), "step": ask_form.public(st), "sfp": ask_form.fp(dtext)}
    trust = pane_shows_trust(a["pane"])
    if trust is None or trust:
        return {"kind": "unreadable" if trust is None else "trust"}
    text = read_prompt(a["pane"])
    asked = asked_part(text)
    if is_permission(text) and a.get("session"):
        kind = "permission"
    elif "Enter to select" in text or any(v in text for v in QUESTION_ONLY):  # 選択式の質問の印がある
        kind = "question"
    else:  # 言い切れない（選択肢が映りきっていない、セッションを見分けられない、など）。ボタンは出さない
        kind = "unknown"
    out = {"kind": kind, "text": _clip(asked, 1200), "fp": prompt_fp(text), "risky": bool(RISKY.search(asked))}
    if kind != "permission" and a.get("session"):  # 選択肢の問い（AskUserQuestion）の、いまの 1 問か確認の画面（確認の画面には操作案内が無い）
        try:
            dtext = read_dialog(a["pane"])
        except (OSError, RuntimeError, subprocess.SubprocessError):
            dtext = ""
        st = ask_form.step(dtext) if dtext else {"stage": "unknown"}
        if st["stage"] in ("question", "review"):
            kind = out["kind"] = "question"
            out.update(step=ask_form.public(st), sfp=ask_form.fp(dtext))
            out["risky"] = out["risky"] or bool(RISKY.search(ask_form.dialog(dtext)))
    if kind == "question":
        dialog = dialog_part(text)
        if dialog:  # 質問は見出しの印から見せる（会話の上の部分は出さない）。取り消しにくい印は、省く前の問いと選択肢の全体でも見る
            out["text"] = _clip(dialog, 1200)
            out["risky"] = out["risky"] or bool(RISKY.search(dialog))
    return out


def read_dialog(pane: str, timeout: float = 20) -> str:
    """選択肢の問いの画面（答えを送る途中の確かめに使う）。read_prompt より多く読む（見本の枠で縦に長くなるため）。
    読めなかったら（herdr が失敗で終わった）例外にする（空の画面を「問いが消えた」と取り違えないため）。"""
    p = subprocess.run(["herdr", "agent", "read", pane, "--source", "visible", "--lines", "80"], capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"画面を読めない（herdr の終わりの数 {p.returncode}）")
    rows = [ODD_BREAKS.sub("\ufffd", l) for l in p.stdout.split("\n")]
    out = "\n".join(l.rstrip() for l in rows if l.strip() and not STATUS_LINE.match(l))
    if not out:  # 空（状態表示の行だけも）は、読めなかったとみなす（「問いが消えた」と取り違えない。visible_screen と同じ）
        raise RuntimeError("画面が空だった")
    return out


def _asked(pane: str, key: str) -> tuple[dict | None, dict]:
    """pane と key が指すセッションと、その問い。key はセッション ID で必須（同じ場所でセッションが立て直されていたら別物として断る。
    ハブも、話し相手の選択に使う "hub" ではなく、いまのセッション ID で見分ける）。"""
    a = next((a for a in list_agents() if a["pane"] == pane), None)
    if not a or not key or a.get("session") != key:
        return None, {"kind": "gone"}
    return a, _ask_of(a)


@app.get("/api/topic")
async def api_topic(id: str = ""):
    """件の URL（/#t=<件>）で画面を開いたとき、画面がその件を引く口（画面だけ。コンテナからは使えない）。
    返すのは、どのセッションの件か（pane・セッション）と題だけ。開いた画面がするのは、選ぶ・出すだけ。"""
    t = topics.get(id)
    if not t:
        return JSONResponse({"ok": False, "reason": "gone"})
    rep = await asyncio.to_thread(HUB.topic_report, t["id"])  # その件の一番新しい報告（画面の履歴に出ていない古い報告も）
    # current: そのセッションの一番新しい件か。いまの問いは一番新しい件のものとする（同じセッションの古い件のリンクで、あとの件の問いを見せない）
    # 画面・声からそのセッションへ直接送ったあと（direct の印）は、いまの問いがこの件のものか分からないので、問いを見せず報告を開く。
    # 報告を読む間に直接送られた印も見るよう、待ちから戻ってから件を読み直して決める
    def eligible() -> bool:
        now_t = topics.get(t["id"]) or {}
        return bool(t["session"]) and bool(now_t) and topics.latest_for_session(t["session"]) == t["id"] and not now_t.get("direct")

    current, ask = eligible(), None
    if current:  # いまの問い（指紋つき）も本体が決めて返す。画面は、この指紋の問いだけを光らせる（あとで変わった問い・別の話の問いは光らせない）
        a = next((x for x in await asyncio.to_thread(list_agents) if x["pane"] == t["pane"] and x.get("session") == t["session"]), None)
        if a and (a["status"] == "blocked" or stepping(a)):
            q = await asyncio.to_thread(_ask_of, a)
            if q.get("kind") != "none" and isinstance(q.get("fp"), str):
                ask = {"pane": a["pane"], "key": t["session"], "fp": q["fp"],  # fp は画面の末尾の指紋。sfp は 1 問ずつの問い全体の指紋（長い問いの違いも見分ける）
                       **({"sfp": q["sfp"]} if isinstance(q.get("sfp"), str) else {})}
        if not eligible():  # 問いを読む間に直接送られた・次の件ができた
            current, ask = False, None
    return JSONResponse({"ok": True, **{k: t[k] for k in ("id", "pane", "session", "project", "title")}, "report": rep, "current": current, "ask": ask})


@app.get("/api/prompts")
def api_prompts():
    """止まっているセッションすべての問い。画面はこれを上に並べ、承認の画面にだけボタンを出す。key はセッション ID。"""
    out = []
    for a in list_agents():
        if a["status"] == "blocked" or stepping(a):
            asked = _ask_of(a)
            if asked["kind"] != "none":
                out.append({"pane": a["pane"], "key": a.get("session") or "", "name": f"{a['cwd'].rsplit('/', 1)[-1]}｜{a['title']}", **asked})
    return JSONResponse(out)


_answer_lock = asyncio.Lock()  # 確かめてからキーを送るまでを、一度にひとつだけにする
ANSWERED: dict[str, tuple[frozenset, float, str]] = {}  # 画面のボタンとハブ経由で答えた問い: pane → (指紋の組, 時刻, 経路)。経路で共有する
ANSWERED_HOLD = 15  # 答えたあと、相手の状態が変わるまでのあいだ、同じ問いへの重ねた答えを断る秒数


def answered(pane: str, fp: str, by: str = "") -> bool:
    """この問い（指紋）に、さっき答えたか（ANSWERED_HOLD 秒のうち）。by を渡すと、その経路で記録したものは数えない。"""
    done = ANSWERED.get(pane)
    return bool(done) and fp in done[0] and time.time() - done[1] < ANSWERED_HOLD and not (by and done[2] == by)


def mark_answered(pane: str, *fps: str, by: str = "") -> None:
    """答えた問いを記録する。1 問ずつの答え（by="step"）は、その画面の指紋（sfp）と、ハブ・承認のボタンが比べる指紋（prompt_fp）の両方で
    記録する（指紋の取り方が違うので、片方だけだと、もう一方の経路から同じ画面へ重ねて送れる。2026-11-17 codex-review）。
    prompt_fp は画面の下の一部から取るので、次の問いに進んでも同じことがある。届いたあとも残し、ハブ・承認のボタンを止める。
    続く 1 問ずつの答えは sfp だけで見る（_answer は、1 問ずつの答えが記録したものを prompt_fp では数えない）。"""
    ANSWERED[pane] = (frozenset(f for f in fps if f), time.time(), by)


@app.post("/api/answer")
async def api_answer(body: dict):
    """画面のボタンで、止まっているセッションの問いに答える。
    守ること: 画面に出したのと同じ問いにだけ答える（指紋で確かめる）。許可は「今回だけ」（1）で、「今後は聞かずに許可」は選ばない。
    取り消しにくい操作に見えるときは、もう一度押してから（confirmed）。承認の画面と見分けられないもの（質問・フォルダの信頼など）には答えない。
    同じ問いに二度答えない。"""
    async with _answer_lock:
        return await _answer(body)


async def _answer(body: dict):
    pane, key, fp = str(body.get("pane", "")), str(body.get("key", "")), str(body.get("fp", ""))
    choice, confirmed = str(body.get("choice", "")), bool(body.get("confirmed"))
    if answered(pane, fp, by="step" if choice == "step" else ""):  # 二度押し・重ねた送信（1 問ずつの答えどうしは、step_answer が sfp で見る）
        return JSONResponse({"ok": False, "reason": "answered"})
    if choice not in ("allow", "deny", "step"):  # 選択肢の問いには step だけ（前の choose は 0.26.1 で外した）
        return JSONResponse({"ok": False, "reason": "bad_choice"})
    a, asked = await asyncio.to_thread(_asked, pane, key)
    if asked["kind"] in ("gone", "none", "unreadable", "trust"):
        return JSONResponse({"ok": False, "reason": asked["kind"]})
    if choice == "step":
        return JSONResponse(await step_answer(a, asked, body, confirmed))
    if asked["kind"] != "permission":  # 承認の画面と見分けられないときは、何も送らない
        return JSONResponse({"ok": False, "reason": "not_permission"})
    if asked["fp"] != fp:  # 画面に出したあとで問いが変わった
        return JSONResponse({"ok": False, "reason": "changed"})
    if choice == "allow":
        if asked["risky"] and not confirmed:
            return JSONResponse({"ok": False, "reason": "confirm"})
        keys, said = "1", "許可（今回だけ）"
    else:
        keys, said = "esc", "断る"
    r = await asyncio.to_thread(herdr, "agent", "send-keys", pane, keys)
    hlog(f"answer(画面) → {pane} ({a['cwd'].rsplit('/', 1)[-1]}): {said} {'ok' if 'result' in r else 'ERR'}", flush=True)
    if "result" not in r:
        return JSONResponse({"ok": False, "reason": "herdr"})
    mark_answered(pane, fp)
    return JSONResponse({"ok": True, "said": said})


# ---------- 選択肢の問い（AskUserQuestion）に、画面から 1 問ずつ答える（2026-11-17 本人の要望） ----------
# いまターミナルに出ている 1 問（か確認の画面）だけを画面から読んでボタンにし、1 回押すごとに確かめながらキーを送る（scripts/ask_form.py）。
# 答えを受けるのは画面のボタン（/api/answer、choice=step）だけ。本体が利用者の端末から直接受け取れる面でだけ答える（Slack は Hermes を通るので、
# 本体は利用者の意思を確かめられない。2026-11-17 本人と決めたビジョン: Slack からは画面のその問いへのリンクで来てもらう）。
STEP_ACTS = ("pick", "toggle", "next", "free", "submit")


async def step_answer(a: dict, asked: dict, body: dict, confirmed: bool) -> dict:
    """1 回押したぶんを送る。見せたのと同じ画面（指紋）にだけ。取り消しにくい問いは、印の付け外しのほかは、もう一度押してから。呼ぶのは _answer_lock の中。"""
    if "step" not in asked:
        return {"ok": False, "reason": "not_form"}
    sfp = str(body.get("sfp", ""))
    if asked["sfp"] != sfp:  # 見せたあとで画面が変わった（端末で答えた・次の問いに進んだ）
        return {"ok": False, "reason": "changed"}
    kind = body.get("act")
    if kind not in STEP_ACTS:
        return {"ok": False, "reason": "bad_choice"}
    arg = body.get("n") if kind in ("pick", "toggle") else body.get("text") if kind == "free" else None
    if asked["risky"] and kind != "toggle" and not confirmed:
        return {"ok": False, "reason": "confirm"}
    pane = a["pane"]
    if answered(pane, sfp):
        return {"ok": False, "reason": "answered"}

    async def read():
        return await asyncio.to_thread(read_dialog, pane, ask_form.READ_MAX)

    async def keys(k):
        r = await asyncio.to_thread(herdr, "agent", "send-keys", pane, k, timeout=ask_form.READ_MAX)
        if "result" not in r:
            raise ask_form.Stuck("herdr が応答しない")

    async def text(t):
        r = await asyncio.to_thread(herdr, "pane", "send-text", pane, t, timeout=ask_form.READ_MAX)
        if "result" not in r and r.get("code") != 0:  # send-text はうまくいくと何も出さない（終わりの数 0）。2026-11-17 実機で確かめた
            raise ask_form.Stuck("herdr が応答しない")

    mark_answered(pane, sfp, asked["fp"], by="step")  # 送り始めたら、同じ画面に重ねて送らない（途中で止まっても。ハブの経路からも）
    SEEN_PROMPT.pop(pane, None)  # ハブがこの前に見た問いへの答えを、このあとの問いに送らない（ハブは問いを見直してから答える）
    CONFIRMING.pop(pane, None)
    STEPPING[pane] = time.time()
    project = a["cwd"].rsplit("/", 1)[-1]
    try:
        said = await ask_form.act(kind, arg, read, keys, text, sfp)  # 最初の読み取りも、見せた画面の指紋と照らす
    except Exception as e:  # 送る前に止めた・途中で止めた（Stuck）か、読み取り・送信の失敗
        why = str(e) if isinstance(e, ask_form.Stuck) else f"結果が分からない（{type(e).__name__}）"
        hlog(f"answer(画面・1 問ずつ) → {pane} ({project}): 止めました: {why}", flush=True)
        return {"ok": False, "reason": "stuck", "why": why}
    hlog(f"answer(画面・1 問ずつ) → {pane} ({project}): {said} ok", flush=True)
    return {"ok": True, "said": said, "project": project, "title": a.get("title", "")}


# ---------- セッションの起動 ----------
def spawn_session(directory: Path, project: str, model: str) -> dict:
    """プロジェクトのディレクトリで Claude Code を立てる。同名のワークスペースがあればその中のタブに、無ければ作る。"""
    wss = herdr("workspace", "list").get("result", {}).get("workspaces")
    if wss is None:
        return {"ok": False, "reason": "herdr が応答しません", "created": False}
    ws = next((w for w in wss if w.get("label") == project), None)
    if ws:
        r = herdr("tab", "create", "--workspace", ws["workspace_id"], "--cwd", str(directory), "--label", "ハブが起動", "--no-focus")
    else:
        r = herdr("workspace", "create", "--cwd", str(directory), "--label", project, "--no-focus")
    found = re.search(r'"pane_id":\s*"([^"]+)"', json.dumps(r))
    if not found:  # herdr がはっきり断った（error が返った）なら作っていない。応答が無い・読めないときは、作ったかどうか分からない
        return {"ok": False, "reason": f"タブを作れませんでした: {json.dumps(r, ensure_ascii=False)[:200]}", "created": False if isinstance(r.get("error"), dict) else None}
    pane = found.group(1)
    time.sleep(2)  # シェルが立ち上がるのを待つ
    name = f"{SPAWN_PREFIX}{project}-{int(time.time()) % 100000}"
    r = herdr("agent", "start", name, "--kind", "claude", "--pane", pane, "--timeout", "90000", "--", "--model", model, timeout=120)
    if pane_shows_trust(pane):
        return {"ok": False, "reason": "trust", "pane": pane}
    if "result" not in r:
        return {"ok": False, "reason": f"セッションを起動できませんでした: {json.dumps(r, ensure_ascii=False)[:200]}", "pane": pane}
    return {"ok": True, "pane": pane, "name": name}


SPAWN_SESSION_WAIT = 5.0  # Slack から立てたとき、最初の指示を送る前に、セッション ID が見えるまで待つ上限（秒）


async def spawned_agent(pane: str, wait: float = 0.0) -> dict | None:
    """立てたセッション。wait があれば、セッション ID が見えるまで（最大 wait 秒）待つ。Slack への報告は、送ったときの ID と
    報告のときの ID が同じときだけ本文を載せるので、ID が分からないまま送った件は定型文になる（あとから ID を覚え直さない）。"""
    end = time.time() + wait
    while True:
        a = next((x for x in await asyncio.to_thread(list_agents) if x["pane"] == pane), None)
        if not a or a.get("session") or time.time() >= end:
            return a
        await asyncio.sleep(0.5)


@app.post("/api/hub/spawn")
async def hub_spawn(body: dict):
    """PROJECTS_DIR 直下の既存プロジェクトに新しいセッションを立て、最初の指示があれば送る。"""
    if REPORTING:  # 立てるは、Slack から来ても待たない（起動と最初の送りまで含めると、hubctl start の待ち時間に収まらないため）
        return JSONResponse({"ok": False, "reason": "reporting"})
    if not PROJECTS_DIR or not PROJECTS_DIR.is_dir():
        return JSONResponse({"ok": False, "reason": "no_projects_dir"})
    project = str(body.get("project", "")).strip().strip("/")
    model = str(body.get("model") or "opus").strip().lower()
    text = " ".join(str(body.get("text", "")).split())
    directory = PROJECTS_DIR / project
    if not project or "/" in project or project.startswith(".") or not directory.is_dir():
        names = sorted(d.name for d in PROJECTS_DIR.iterdir() if d.is_dir() and not d.name.startswith("."))
        return JSONResponse({"ok": False, "reason": "no_project", "projects": names})
    if model not in SPAWN_MODELS:
        return JSONResponse({"ok": False, "reason": "bad_model", "models": sorted(SPAWN_MODELS)})
    if len(text) > PROMPT_MAX:
        return JSONResponse({"ok": False, "reason": "too_long", "length": len(text), "max": PROMPT_MAX})
    r = await asyncio.to_thread(spawn_session, directory, project, model)
    hlog(f"spawn → {project} ({model}): {json.dumps(r, ensure_ascii=False)[:160]}", flush=True)
    if not r["ok"]:
        return JSONResponse(r)
    out = {"ok": True, "pane": r["pane"], "project": project, "model": model, "sent": False}
    if text:
        slack = slack_report.origin(body.get("origin"))
        a = await spawned_agent(r["pane"], wait=SPAWN_SESSION_WAIT if slack else 0)
        sent = await dispatch(a, text, "spawn", slack) if a else {"ok": False, "reason": "起動したセッションが見つかりません"}
        out["sent"], out["send_error"] = sent["ok"], None if sent["ok"] else sent.get("reason")
        out["slack"] = bool(sent.get("slack"))  # 片づいたときの報告を Slack のスレッドにも送るか
    return JSONResponse(out)


# ---------- セッションを閉じる（取り消せないので、ユーザーがはっきり許可してから） ----------
CLOSING: dict[str, tuple[tuple, str | None]] = {}  # 閉じていいかを聞いている: pane → (どのセッションか, 聞いたときのユーザーの最後の発言)
CLOSED: set[str] = set()  # ハブが閉じた pane。見張りに「見つからなくなった」と知らせさせない
# 許可の答えの読み取り。断りの語が1つでもあれば閉じない。はっきり許す語が無いときも閉じない（どちらも聞き直し）
# 閉じていいかの問い（「閉じていいですか」「閉じてもいい？」「閉じていいか、聞かせてください」「閉じますか」）。
# 「閉じませんでした」のような報告や、「閉じると何が起きるか説明しましょうか」を、問いと取り違えないため
CLOSE_QUESTION = re.compile(r"閉じ(て(も)?(いい|よろしい)(です)?|ます|ましょう)(か|[?？])")


def asks_to_close(reply: str, project: str) -> bool:
    """ハブの返答（ユーザーが聞いた文）が、プロジェクト名を挙げて、閉じていいかを問うているか。"""
    return project.lower() in reply.lower() and bool(CLOSE_QUESTION.search(reply))


@app.post("/api/hub/close")
async def hub_close(body: dict):
    """セッションを閉じる（pane を閉じる。タブやワークスペースが空になれば herdr がそれも閉じる。会話の記録は残る）。
    守ること（ハブの判断に任せず、ハブの記録で確かめる）:
      1回目は閉じずに、閉じる相手を返す（ハブはそれをユーザーに伝えて許可を聞く）。
      閉じるのは confirmed で、聞いたときのユーザーの発言の、すぐ次の発言の中だけ（同じ発言の中では閉じない。あいだに別の発言をはさんだら聞き直し）。
      さらに、聞いたときのハブの返答（ユーザーが聞いた文）が、プロジェクト名を挙げて閉じていいかを問うていて、
      ユーザーの次の発言全体が、許す文法に収まること（断る・保留・問い返し・禁止形は通さない。scripts/consent.py）。
      セッション ID の無い相手は、入れ替わっても見分けられないので閉じない。"""
    if REPORTING:
        return JSONResponse({"ok": False, "reason": "reporting"})
    pane, confirmed = str(body.get("pane", "")), bool(body.get("confirmed"))
    if confirmed and not await hub_settled():  # いまの発言の画面が同期の合図を返すまで待つ（閉じる相手を読む前に。待つ間に入れ替わっても、読むのはそのあと）
        return JSONResponse({"ok": False, "reason": "stopped"})
    held = CLOSING.get(pane)
    (now, said), (before, _) = (await asyncio.to_thread(hub_turns, 2) + [(None, "")] * 2)[:2]
    if confirmed and await asyncio.to_thread(hub_via, now) == "device":  # 機械からの発言では閉じない（閉じていいかは画面で答える）
        return JSONResponse({"ok": False, "reason": "device"})
    if confirmed and now and not await asyncio.to_thread(hub_current, now):  # この発言のあとに利用者が止めた・次の発言をした（頭が回の途中で閉じない。a2a 30fd5b94）
        return JSONResponse({"ok": False, "reason": "stopped"})
    heard = await asyncio.to_thread(hub_reply_to, held[1]) if confirmed and held and held[1] else ""
    rp = await asyncio.to_thread(hub_replying, now) if now else None
    if heard and rp is not None:  # 返信の発言なら、聞いた文は返信先の報告（古い報告への「はい」で、閉じるの問いに答えない）
        heard = await asyncio.to_thread(hub_heard, now)
    elif heard and now and await asyncio.to_thread(hub_heard, now) != heard:
        # 「はい」の直前に聞いた文が、閉じていいかの問いではない（問いのあとに報告が届いた・ほかのセッションに話した・止めた）。
        # 報告への「はい」で、前の問いのセッションを閉じない。聞き直してもらう（a2a 3752b7a5）
        heard = ""
    a = await asyncio.to_thread(_worker, pane)  # 相手は最後に見る（閉じるまでに入れ替わる隙を小さくする）。ハブ自身は閉じない
    if not a:
        return JSONResponse({"ok": False, "reason": "gone"}, status_code=404)
    if not a.get("session"):
        return JSONResponse({"ok": False, "reason": "unidentified"})
    who, project = (a["cwd"], a["session"]), a["cwd"].rsplit("/", 1)[-1]
    why = "confirm"
    if confirmed and held and held is CLOSING.get(pane) and held[0] == who:
        if held[1] == now:  # 聞いたのと同じ発言の中 = ユーザーはまだ何を閉じるか聞いていない
            return JSONResponse({"ok": False, "reason": "same_turn"})
        if now and held[1] and held[1] == before:
            if rp is not None:  # 返信の発言は、閉じる許可に使わない。ハブはふつうの発言で聞き直す（聞き直しの起点は、いまの発言に移す）
                why = "reply"
            elif not asks_to_close(heard, project):
                why = "not_asked"
            elif not consent.affirms(said, "close", project):
                why = "not_yes"
            elif not settled_now() or not hub_current(now):  # 閉じる直前に、もう一度（読み取りの待ちの間に止めた・次の発言をした・その画面がまだ同期を返していない。ここから閉じるまで待ちを挟まない）
                why = "stopped"
            else:
                del CLOSING[pane]  # 閉じる前に外す（重ねて届いた同じ許可で、二度閉じない）
                CLOSED.add(pane)
                r = await asyncio.to_thread(_if_still, lambda: hub_current(now), herdr, "pane", "close", pane)
                if r is None:  # スレッドの空きを待つ間に止めた・次の発言をした（閉じていない）
                    CLOSED.discard(pane)
                    return JSONResponse({"ok": False, "reason": "stopped"})
                hlog(f"close → {pane} ({project}): 許可「{_clip(said, 40)}」 {'ok' if 'result' in r else 'ERR ' + json.dumps(r, ensure_ascii=False)[:120]}", flush=True)
                if "result" not in r:
                    CLOSED.discard(pane)
                    return JSONResponse({"ok": False, "reason": "herdr"})
                SEEN_PROMPT.pop(pane, None)
                CONFIRMING.pop(pane, None)
                return JSONResponse({"ok": True, "project": project, "title": a["title"]})
    CLOSING[pane] = (who, now)  # ここから聞く（聞き直す）。閉じてよいのは、ユーザーの次の発言の中だけ
    path = jsonl_path(a)
    user = last_exchange(read_lines(path, tail_bytes=600_000))[0] if path and path.exists() else ""
    return JSONResponse({"ok": False, "reason": why, "project": project, "title": a["title"], "status": a["status"],
                         "last_user": _clip(user, 200), "said": _clip(said, 80)})


# ---------- 状態のファイル ----------
def _read_json(path: Path, default):
    try:
        d = json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except (OSError, ValueError, RecursionError):
        traceback.print_exc()
        return default
    return d if isinstance(d, type(default)) else default



# ---------- 代理からの知らせ（hubctl notice） ----------
# 代理（Hermes）が、見張りの回（cron）で見つけた判断待ちなどを知らせる口。本体は鳴らすだけで、知らせを残さない（0.35.2 から。一覧も、その口も無い。
# 判断待ちは受け箱に、見張りが止まった口は受け箱の帯に出る）。開いている画面とプッシュ通知には題だけを載せ、一言は受けるが使わない。知らせには押して動くものが無い（送る・答える・立てる・閉じるの口とはつながらない）。
# 通知を押すと受け箱（💡）が開く。判断待ちの記録の番号が 1 つ添えてあれば、その件を開く（開くだけ。中身は本体が記録から読む。scripts/hermes_inbox.py）。
# 残すのは、次の番号と、直近 1 時間に受けた時刻だけ（1 時間に受ける数の上限のため）。
NOTICES_FILE = STATE_DIR / "notices.json"
NOTICE_TITLE, NOTICE_TEXT = 60, 400  # 字数の上限（超えた分は切る）
NOTICE_ITEMS = 200  # 知らせに添えられる判断待ちの記録の番号の数（2 つ以上なら受け箱を開くので、数は開き先を変えない。見張りの 1 回で扱う数より多く）
NOTICE_PER_HOUR = 20  # 1 時間に受ける数。見張りが暴れても、通知が鳴り続けないように
_notice_lock = threading.Lock()
_NOTICE_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\ud800-\udfff]")  # 制御文字・向きを変える文字・対になっていないサロゲート
NOTICE_FILE_MAX = 1_000_000  # 記録のファイルがこれより大きければ読まない（空として扱う）


def _notice_clean(v, n: int) -> str | None:
    """1 行にそろえ、制御文字などを空白にして、n 字で切る。文字でなければ None。"""
    if not isinstance(v, str):
        return None
    v = " ".join(_NOTICE_CTRL.sub(" ", v).split())
    return v if len(v) <= n else v[: n - 1] + "…"


NOTICE_N_MAX = 2**53 - 1  # 番号の上限（画面の JavaScript で正確に扱える整数まで）


def _notice_time(t, now: float) -> bool:
    """ふつうの範囲の数で、1 日より先の未来でない時刻か（時計が少し戻っても落とさない）。
    範囲は比べるだけで確かめる（巨大な整数を float に直すと例外になる。NaN は比べると偽）。"""
    return isinstance(t, (int, float)) and not isinstance(t, bool) and -1e11 < t < 1e11 and t - now < 86400


def _notice_n(n) -> bool:
    return isinstance(n, int) and not isinstance(n, bool) and 0 < n <= NOTICE_N_MAX


def notices() -> dict:
    """{"next": 次の番号, "sent": 直近 1 時間に受けた時刻}。
    記録は本体の状態フォルダにあるが、壊れていても・手で書き換えられていても、形を確かめ直してから使う（前の版が残した知らせの行は読まない）。"""
    try:
        d = _read_json(NOTICES_FILE, {}) if not NOTICES_FILE.exists() or NOTICES_FILE.stat().st_size <= NOTICE_FILE_MAX else {}
    except OSError:
        d = {}
    if not isinstance(d, dict):
        d = {}
    now = time.time()
    # 時計が戻っても上限が外れないよう、未来の時刻（1 日まで）も数え続ける
    sent = [float(t) for t in (d.get("sent") if isinstance(d.get("sent"), list) else []) if _notice_time(t, now) and now - t < 3600]
    nxt = d.get("next")
    return {"next": nxt if _notice_n(nxt) else 1, "sent": sent}


def notices_tidy() -> bool:
    """知らせの記録に、次の番号と受けた時刻のほかのもの（0.35.0 までの版が残した知らせの題・一言など）があれば、取り除いて書き直す。
    番号と、直近 1 時間に受けた時刻はそのまま残す（1 時間に受ける数の上限を、書き直しで外さない）。書き直したら True。起動のたびに 1 回。"""
    with _notice_lock:
        if not NOTICES_FILE.exists():
            return False
        try:
            raw = _read_json(NOTICES_FILE, {}) if NOTICES_FILE.stat().st_size <= NOTICE_FILE_MAX else {}
        except OSError:
            raw = {}
        d = notices()
        if raw == d:
            return False
        write_json(NOTICES_FILE, d)
        return True


@app.post("/api/hub/notice")
async def hub_notice(body: dict):
    """hubctl notice。代理が知らせる題と一言を受け、画面が開いていればそこへ、閉じていればプッシュ通知で知らせる（どちらも題だけ。知らせは残さない）。"""
    title, text = _notice_clean(body.get("title"), NOTICE_TITLE), _notice_clean(body.get("text", ""), NOTICE_TEXT)
    if not title or text is None:
        return JSONResponse({"ok": False, "reason": "bad_request"}, status_code=400)
    items = body.get("items", [])
    if not isinstance(items, list) or len(items) > NOTICE_ITEMS or not all(isinstance(n, int) and not isinstance(n, bool) and 0 < n < 2**31 for n in items):
        return JSONResponse({"ok": False, "reason": "bad_items", "max": NOTICE_ITEMS}, status_code=400)
    with _notice_lock:
        d = notices()
        if len(d["sent"]) >= NOTICE_PER_HOUR or d["next"] >= NOTICE_N_MAX:
            return JSONResponse({"ok": False, "reason": "too_many", "max": NOTICE_PER_HOUR})
        row = {"n": d["next"], "at": time.time()}
        write_json(NOTICES_FILE, {"next": row["n"] + 1, "sent": d["sent"] + [row["at"]]})
    hlog(f"知らせ [{row['n']}]（題 {len(title)}字・一言 {len(text)}字）", flush=True)
    for c in list(CLIENTS):
        try:
            await c.send(type="agent_notice", n=row["n"], title=title, sender=HUB.label)
        except Exception:
            pass
    # 押すと受け箱を開く。判断待ちの記録の番号が 1 つだけ添えてあれば、その件を開く（開くだけ。中身は本体が記録から読む）
    items = list(dict.fromkeys(items))
    push = await push_all(f"{HUB.label} から", title, f"notice-{row['n']}", url=f"/#i={items[0]}" if len(items) == 1 else "/#inbox")
    return JSONResponse({"ok": True, "n": row["n"], "push": push})


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    client = Client(sock)
    CLIENTS.add(client)
    cancel = asyncio.Event()  # 進行中のターンを止める合図。ターンごとに作り直す
    current: asyncio.Task | None = None
    send = client.send

    async def turn(text: str, pane: str, cancel: asyncio.Event, key: str | None, reply_at: float | None = None, files: list[str] | None = None,
                   job: asyncio.Task | None = None, plain: bool = False, admitted: int | None = None, direct: dict | None = None):
        try:
            talker = TALKERS.get(pane)
            if talker:  # herdr のセッションではなく、本体の外の話し相手（ハブ）。job は受け取ったその場で作った仕事（start_turn）
                await talk_turn(talker, job, pane, cancel, plain, admitted)
                return
            why = attachments_shape_problem(files)
            extra = ""
            if not why:
                extra, why = await attachment_lines(files, None, pane)
            if direct is not None and direct["dropped"]:  # 添付の用意の間に止めた（「送っていません」と知らせ済み）。「送りました」で上書きしない（codex-review 6b2ddfe）
                return
            if why:  # 送らずに断る（切れてつなぎ直した画面にも届ける）
                await tell_window(win_of(client), type="error", text=why)
                return
            await run_turn(text, pane, cancel, key, extra, direct)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # 語り手・音声合成・herdr の失敗を黙って止めず、画面とログに出す
            traceback.print_exc()
            with contextlib.suppress(Exception):  # 画面が切れていれば知らせない
                await tell_window(win_of(client), type="error", text=f"{type(e).__name__}: {e}"[:300])
        finally:
            if direct is not None and not direct["handed"]:  # 送る仕事に渡さずに終わった（断った・相手がいない・取り消した）。一覧から外す
                DIRECT_PENDING.pop(direct["key"], None)

    async def talk_turn(talker, job: asyncio.Task | None, pane: str, cancel: asyncio.Event, plain: bool = False, admitted: int | None = None):
        # plain: 本体がその場で答えた決まった問い。読み上げは語り手を通さない
        if job is None:  # 受け取ったときに相手が使えなかった
            await send(type="error", gone=True, text=talker.off_text)
            return
        await send(type="status", text=talker.sending_text())
        cue, cue_task, dropped = None, None, False

        async def settle_cue():
            """返事（か失敗）が決まった。一言の声を送っている途中なら、送り終わるのを待つ（声の順が入れ替わらないように。短い）。
            それ以外（状態の文を送っている・合成している・まだ始まっていない）なら、一言をまるごと取り消す（返事のあとに届かないように）。"""
            if cue.get("settled"):  # 失敗の道と finally の両方から呼ばれる。一度だけ
                return
            cue["stop"] = cue["settled"] = True
            if not cue["sending"]:
                cue_task.cancel()
            await asyncio.gather(cue_task, return_exceptions=True)
        if PROGRESS_AFTER > 0:  # しばらく返事が無ければ、本体の一言（返事の中身・記録・守りには関わらない）
            finished, _ = await asyncio.wait({job}, timeout=PROGRESS_AFTER)  # 期限切れと、job 自身の例外を取り違えない（例外は下で job から受ける）
            if not finished and not cancel.is_set():
                cue = {"stop": False, "sending": False, "sent": False}
                cue_task = asyncio.create_task(speak_progress([client], pane, cancel, cue))
        try:
            # 画面が閉じても、止めても、返事は最後まで待って記録する（相手は答えて覚えているので、記録が欠けると画面と食い違う）
            reply, why, saved = await asyncio.shield(job)
        except asyncio.CancelledError:  # このターンが取り消された（止めて次の発言など）。一言は待たずに取り消す（次のターンの返事のあとに届かないように）
            if cue:
                cue["stop"] = True
                cue_task.cancel()
                dropped = True
            raise
        except Exception:  # 往復が例外で終わった（呼び出し側が画面に知らせる。画面はエラーで、鳴らしている・待っている声を捨てる）
            if cue:
                await settle_cue()
            raise
        finally:
            if cue and not dropped:
                await settle_cue()
        if why:
            await send(type="error", text=why[:300], saved=saved)
            return
        if cancel.is_set():
            return
        # saved=False なら、画面は記録から描き直さずに足す。hid は返事の番号（画面は控えて、次の声の発言の頭に付ける）
        await send(type="reply", pane=pane, text=reply, saved=saved, **({"hid": admitted} if admitted is not None else {}))
        if admitted is not None:  # 画面に送り終えた。ここで初めて、返事を利用者が聞いた文にする（送る前に話しかけ直した・止めたなら、頭が捨てている）
            talker.delivered(admitted)
        await speak_to([client], reply, cancel, **({"plain": True} if plain else {}))  # 本体の答えのときだけ渡す（これまでの呼び方は変えない）

    async def run_turn(text: str, pane: str, cancel: asyncio.Event, key: str | None, extra: str = "", direct: dict | None = None):
        # extra: 本体が足す添付の行（セッションに渡す文の末尾に付ける）
        # direct: この指示を送ったかの印（ワーカーのスレッドの中で、送る直前に立てる。立つ前に止める・次の発言で取り消したら、送らずに
        #         画面に送っていないと知らせる。立てるのと取り消すのは、印の鍵で 1 つずつ）
        agent = next((a for a in list_agents() if a["pane"] == pane), None)
        if not agent:
            await tell_window(win_of(client), type="error", gone=True, text="話し相手のセッションが見つかりません。選び直してください。")  # 切れてつなぎ直した画面にも（a2a 6604a50a の 5 回目）
            return
        if key and agent_key(agent) != key:  # 開きっぱなしの画面の一覧が古く、同じ場所に別のセッションが居る
            await tell_window(win_of(client), type="error", gone=True, text="話し相手が入れ替わっています。送らずに止めました。選び直してください。")
            return
        if direct is not None and direct["dropped"]:  # 取り消し済み（「送っていません」と知らせ済み）。送ったとは出さない
            return
        with contextlib.suppress(Exception):  # 画面が切れていても送りは止めない（「送りました」と出た指示を、切れただけで黙って消さない。a2a 6604a50a）
            await send(type="status", text="エージェントに送りました。考え中…")
        SENT_GEN[pane] = SENT_GEN.get(pane, 0) + 1  # 画面・声からの直接の指示も、前の件の Slack への報告に新しい結果を載せない（送る前に進める）
        async with pane_lock(pane):  # ハブが自動通知に応答している最中なら、それが終わってから送る
            SENT_GEN[pane] = SENT_GEN.get(pane, 0) + 1  # 順番を待つ間に、ハブ経由の件が送られていることがある。実際に送る直前にも進める
            # 返事が来るまで（止められても、送る処理が裏で終わるまで）に送られたハブ経由の件は、重なった件として扱う
            session, loop = agent.get("session") or "", asyncio.get_running_loop()

            def commit():
                if direct is not None:
                    with direct["lock"]:
                        if direct["dropped"]:
                            return False
                        direct["sent"] = True
                # 直接送った話の問いを、前の件のリンクで見せない。印は送ると決めたときだけ付ける（送らずに取り消したら、前の件のリンクを
                # 外さない。codex-review 3b75d56）。ワーカーのスレッドからなので、イベントループに頼む
                loop.call_soon_threadsafe(topics.mark_direct, session)
                return True

            if direct is not None:
                direct["handed"] = True  # ここからは、送る仕事が終わるまで一覧に残す（切れても、ほかの画面の停止で取り消せる）
            tok = ASK_COMMIT.set(commit)
            try:
                fut = track_send(pane, ask(agent, text + extra))  # 送る仕事は、作るときの ASK_COMMIT を持つ
            finally:
                ASK_COMMIT.reset(tok)

            def report_failure(f):
                # 送れなかったことは、送る仕事の終わりのここ 1 か所で知らせる（このターンを止める・次の発言で取り消したのが、
                # 終わる前でもあとでも 1 回。ターンが待っていなくても黙って消さない。a2a 38ef4720）
                if f.cancelled():
                    return
                if direct is not None and direct["dropped"]:  # 取り消して「送っていません」と知らせ済み。同じ指示に「送信できませんでした」を重ねない（codex-review 27fc1d4）
                    return
                exc = f.exception()
                st, why = ("error", f"{type(exc).__name__}: {exc}") if exc is not None else f.result()
                if st == "error":
                    t = asyncio.ensure_future(tell_window(win_of(client), type="error", text=f"送信できませんでした: {why}"[:300]))  # 切れてつなぎ直した画面にも
                    _tasks.add(t)
                    t.add_done_callback(_tasks.discard)

            fut.add_done_callback(report_failure)
            if direct is not None:
                fut.add_done_callback(lambda _f, k=direct["key"]: DIRECT_PENDING.pop(k, None))
            try:
                state, reply = await asyncio.shield(fut)
            except asyncio.CancelledError:
                raise
            except Exception:
                return  # 知らせは report_failure が出す
            if cancel.is_set() or state in ("cancelled", "error"):  # error の知らせは report_failure が出す
                return
            if state == "blocked":
                await send(type="status", text="承認待ちです")
                await send(type="blocked", pane=pane)  # 画面に、許可・断るのボタンを出させる
                await speak_to([client], "【承認待ちの画面】\n" + reply, cancel)
            elif not reply.strip():
                await send(type="error", text="回答を読めませんでした")
            else:
                await send(type="reply", pane=pane, text=reply)
                await speak_to([client], reply, cancel)

    def start_turn(text: str, pane: str, speaker: int, key: str | None, reply_at: float | None = None, files: list[str] | None = None,
                   item: int | None = None, said: str | None = None, admitted: int | None = None, via: str | None = None, seen: int | None = None,
                   stopgen: int | None = None, crossed: bool = False, legacy: bool = False, legacy_drop: bool = False):
        # said: 辞書で直す前の聞き取り（直したときだけ。ハブの守りの台帳に載せる）
        # admitted: 発言を受けたその場（声なら発言の頭が届いたとき）に取った受け付けの番号。無ければここで取る
        # via: 発言を受けたその場の発言元（機械なら "device"）。無ければいまの名乗りから決める（声は頭のときに控える。頭と声の間に
        #      「画面に戻る」の名乗りが届いても、顔で録った発言を画面からの発言にしない。a2a 3752b7a5）
        nonlocal cancel, current
        client.speaker = speaker
        WIN_SPOKE[win_of(client)] = client.last_turn = time.time()  # この窓に最後に話しかけた（報告の声はここで鳴らす）
        cancel.set()  # 前のターンの読み上げが残っていれば止める
        drop_direct()
        if current and not current.done():
            current.cancel()
        cancel = asyncio.Event()
        job = None
        quick_kind = None
        # seen: 文字の発言を送るを押した時点で、画面が受け取っていた返事の番号（heard_seen）
        if admitted is None:  # 文字の発言（声は、発言の頭が届いたときに済ませている）
            admitted = admit_now(seen)  # 相手がハブでなくても進める（別のセッションに話しかけ直したら、前のハブの往復の許可は失効。codex-review bcd26ac）
            left_hub(pane)
        if crossed:  # 発言を始めた（送るを押した・録り終えた）あと、届くまでに、どこかの画面で止めた。発言は届けるが、承認に答える・閉じるには使わせない
            # この発言だけを、いまの発言にならない受け付けの番号にする（ハブ全体を止め直すと、ほかの画面のあとの発言まで失効させる。codex-review c72fbbe・bb7be2c）
            admitted = -1
        if isinstance(admitted, int) and admitted >= 0:
            SETTLE_LAST["n"] = max(SETTLE_LAST["n"], admitted)
            if client.sync and admitted not in SETTLING:  # 同期の合図を送り、返事を読むまで、この発言で承認に答えない・閉じない
                for j in [j for j, v in SETTLING.items() if j < admitted and v[0].is_set()]:  # 切れて残った前の発言の印は、もう使わない
                    SETTLING.pop(j, None)
                SETTLING[admitted] = [threading.Event(), None]
                my_settle.add(admitted)
                task = asyncio.create_task(send(type="sync", n=admitted))  # 送るのは別の仕事で（受信を待たせない）
                _tasks.add(task)
                task.add_done_callback(_tasks.discard)
        if via is None:
            via = "device" if client.kind == "device" else ""
        talker = TALKERS.get(pane)
        if talker and talker.on():
            # ハブが受け取った発言は、受け取ったその場で（待つものをはさまずに）1 つの仕事にする（hub_accepted: 添付を置く・断る・往復する）。
            # 画面のターン（current）は次の発言で止めるが、この仕事は止めない。受け取った順に作るので、受け取った順に進む
            # （画面のターンの中で作ると、状態の知らせを送る間や、ターンが動き出す前に次の発言が来たとき、前の発言が消える）
            # 添付が無い（空の一覧）ときだけ。形の壊れた添付（None）は、ふつうの道で断る（a2a 8f05ef33）
            kind = quick_kind = quick.match(text, AGENT) if QUICK and isinstance(files, list) and not files and reply_at is None and item is None else None
            # 決まった問いは本体が答える（返信・添付つき・受け箱の件への返信は、いつも頭へ）
            job = asyncio.create_task(hub_quick(talker, kind, text, pane, said, via, admitted) if kind else hub_accepted(talker, text, pane, reply_at, files, item, said, via=via, admitted=admitted))
            _tasks.add(job)
            job.add_done_callback(_tasks.discard)
        direct = None
        if job is None and not (talker and talker.on()):  # ハブ以外への指示。送るのは画面のターンの中なので、送ったかを本体の一覧で控える
            DIRECT_SEQ["n"] += 1
            direct = direct_turn["d"] = DIRECT_PENDING[DIRECT_SEQ["n"]] = {
                "key": DIRECT_SEQ["n"], "sent": False, "dropped": False, "handed": False, "lock": threading.Lock(), "notify": notify_unsent}
            if crossed or legacy_drop or (stopgen is not None and stopgen != STOP_GEN["n"]):  # 発言を始めたあと・声の頭を受けたあと（聞き取りの間）に、どこかの画面で止めた
                if legacy and (crossed or legacy_drop):  # 合図の無い更新前の画面の添付つき・声の指示。確かめられないので送らず、読み込み直しを頼む
                    direct["notify"] = lambda: notify_unsent("この画面は古い版なので、この指示はセッションに送っていません。画面を読み込み直してから送り直してください")
                drop_pending(direct)  # 送っていないと知らせ、画面のターンは動かさない（「送りました・考え中」と出さない。codex-review dad00a5）
                return
        current = asyncio.create_task(turn(text, pane, cancel, key, reply_at, files, job, plain=bool(quick_kind), admitted=admitted, direct=direct))

    direct_turn: dict = {"d": None}  # いまの画面のターンが、ハブ以外への指示ならその印 {"sent": 送り始めたか}
    my_settle: set[int] = set()  # この接続が同期の返事を返していない発言の受け付けの番号

    def drop_direct():
        # この接続の画面のターンを次の発言で取り消す前に呼ぶ。ハブ以外への指示をまだ送っていなければ、送らせず、送っていないと画面に知らせる
        # （「送りました」と出たまま、黙って消さない。codex-review e460a4f）
        d, direct_turn["d"] = direct_turn["d"], None
        if d is not None:
            drop_pending(d)

    def notify_unsent(text: str = "送る前に取り消したので、セッションへの前の指示は送っていません"):
        async def tell():
            await tell_window(win_of(client), type="error", text=text)  # 窓の印で届ける（切れてつなぎ直した画面にも。つながっていなければ控える）
        task = asyncio.create_task(tell())
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)

    begins: dict[int, dict] = {}  # 画面が発言を始めた合図の番号 → {"stop": そのときの停止の回数, "adm": 受け付けの番号}（新しいものから 32 件）
    # 声の聞き取りで受信が止まっていた間の、止まり始めたとき・止まり終えたときの停止の回数と、窓の終わり（止まっていた間に届いていた合図に使う）
    blocked = {"stop": 0, "end": 0, "until": 0.0}

    def stopped_in_block() -> bool:
        # 受信に戻ってからの窓の中で、止まっていた間にどこかの画面で止めていたか（窓の中で読んだ合図・文字は、止まっていた間に届いていたかもしれない）
        return time.monotonic() <= blocked["until"] and blocked["end"] != blocked["stop"]

    def take_begin(d: dict) -> tuple[bool, int | None, bool]:
        # 発言の本文・声の頭に付いた合図（begin）を引く。(合図のあと届くまでに、どこかの画面で止めたか, 合図を受けたときの受け付けの番号, 合図が無いか)。
        # 合図の番号が分からない（つなぎ直した）なら、止めたものとして扱う（断る側）
        b = d.get("begin")
        if b is None:
            # 合図の無い更新前の画面（機械も）。「↩ 返信」と添付つきの発言は、届くまでに止めたか分からないので、止めたものとして扱う
            # （承認に使わせない・ハブ以外へは送らずに読み込み直しを頼む。UPGRADE.md の案内どおり。a2a 4135f4c2）
            # 声の聞き取りで受信が止まっていた間に届いていた文字も、その間にどこかで止めていたら、止めたものとして扱う（a2a 4135f4c2 の 4 回目）
            return (d.get("reply_at") is not None or bool(d.get("attachments")) or stopped_in_block()), None, True
        if not isinstance(b, int) or isinstance(b, bool) or b not in begins:
            return True, None, False
        info = begins.pop(b)
        return info["stop"] != STOP_GEN["n"], info["adm"], False

    def left_hub(pane: str):
        # ハブ以外に話した（声は発言の頭が届いたとき。聞き取りが空・失敗でも）。ハブの直前の問いは、次にハブへ言う発言の直前に聞いた文
        # ではない（止めたのと同じに、聞いた文と台帳の最新の返事を空にする。別のセッションへの発言をはさんだ「はい」で、前の問いの
        # 承認・閉じるを通さない。a2a 3752b7a5）
        talker = TALKERS.get(pane)
        if not (talker and talker.on()):
            for t in TALKERS.values():
                t.stopped()

    pending: dict = {}
    last_stop: dict = {}  # この接続の最後の停止への返事（番号・確かめられないか）。止めたあとに届いた声を捨てたときも同じ返事をする
    try:
        while True:
            msg = await sock.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("text") is not None:
                d = json.loads(msg["text"])
                if d["type"] == "hello":  # 画面が選んでいる声と、読み上げるか（ハブからの報告を、その声で届けるため）。🔇 を押すたび・画面が隠れる／見えるたびにも届く
                    client.speaker = None if d.get("speaker") in (None, "") else int(d["speaker"])
                    client.voice = d.get("voice") is not False  # voice を送らない古い画面は、これまでどおり読み上げる
                    if not client.bound:  # 口で機械と決まった接続は、画面と名乗っても機械のまま。置き場所も口で決まる
                        client.kind = "device" if d.get("kind") in ("stackchan", "device") else "screen"
                        client.place = d.get("place") if d.get("place") in ("home", "car", "desk") else ""
                    client.audio = "opus" if d.get("audio") == "opus" else "wav"
                    w = d.get("win")
                    if isinstance(w, str) and WIN_RE.fullmatch(w):
                        client.win = w
                    # visible を送らない古い画面は、見えていないものとして扱う（通知を止めない）。機械は見えている画面に数えない
                    client.visible = d.get("visible") is True and client.kind == "screen"
                    client.sync = d.get("sync") is True  # 同期の合図を返す画面か（更新前の画面・機械は返さない）
                    client.seen = time.time()
                    notes = UNSENT_NOTICES.pop(win_of(client), [])  # この窓に届けられずに控えていた、指示の行方の知らせ

                    async def greet(notes=notes, win=win_of(client)):
                        with contextlib.suppress(Exception):  # いまの停止の回数（画面は発言を始めた合図に付ける）
                            await send(type="stopgen", n=STOP_GEN["n"])
                        for i, note in enumerate(notes):
                            try:
                                await send(**note)
                            except Exception:  # 名乗った直後にまた切れた。送れなかった分は控えに戻す（次に名乗ったときに届ける。a2a 6604a50a の 4 回目）
                                UNSENT_NOTICES.setdefault(win, [])[:0] = notes[i:]
                                break
                    # 名乗りへの返事は別の仕事で送る（送りが詰まっても、受信を待たせない。その間に届いた停止を読む前に、承認に答える・
                    # 閉じるが進まないように。codex-review f25d488 の P1）
                    task = asyncio.create_task(greet())
                    _tasks.add(task)
                    task.add_done_callback(_tasks.discard)
                    continue
                if d["type"] == "stop":
                    # scope=talk: 話し始めるときに、画面が再生だけを止めた（この画面の分だけ。ほかの画面の送る前の指示は取り消さない。codex-review dad00a5）
                    talk, unsure = d.get("scope") == "talk", False
                    if pending.get("type") == "turn":  # 声の発言の頭のあと、声の本体の前に止めた。あとから届く声は聞き取らずに捨てる（「止めました」のあとに送らない。codex-review 7793a44）
                        pending = {"type": "dropped"}
                    cancel.set()
                    if talk:  # 話し始めるときの再生だけの停止。この画面の送る前の指示だけ取り消し、届いていた返事（聞いた文）は消さない（codex-review 6b2ddfe）
                        drop_direct()
                    else:
                        # 停止の確定は、操作の送り始めと同じ鍵の中で行う（送り始めた操作は止めた前のもの。ここから先は、止めた発言での操作を始めない）。
                        # 鍵の空きは、イベントループを止めずに待つ（codex-review 711ce71）
                        async with invalidating("stop"):  # 読んだその場で確定待ちに数える（鍵を待つ間に、古い操作を始めさせない）
                            STOP_GEN["n"] += 1
                            direct_turn["d"] = None
                            for pend in list(DIRECT_PENDING.values()):  # この画面とほかの画面（切れた画面も）の、送る前のハブ以外への指示を取り消し、それぞれに知らせる
                                drop_pending(pend)
                            for t in TALKERS.values():  # 利用者は、読まれていた返事・まだ届いていない返事を聞いていない（承認に答える・閉じるの守り）
                                t.stopped()
                        # 停止の回数を全部の画面に知らせる（画面は発言を始めた合図に付けて送る。最新の停止を知らずに始めた発言は、通信が遅れて停止の
                        # あとに届いても、停止をまたいだものとして見分ける。codex-review 6b2ddfe の P1）。送るのは別の仕事で（遅い画面に待たされない）
                        task = asyncio.create_task(tell_stop_gen(STOP_GEN["n"]))
                        _tasks.add(task)
                        task.add_done_callback(_tasks.discard)
                        inflight = list(HELD_OPS)
                        # 止める前に起動した操作が返ってから「止めました」と返す（本体は止めずに待つ）。返らなければ、止めたか確かめられないと返す
                        if inflight and not await asyncio.get_running_loop().run_in_executor(COMMIT_POOL, wait_inflight, inflight):
                            unsure = True
                        if time.monotonic() < UNSURE_UNTIL["t"]:  # 少し前に、届いたか分からない操作があった
                            unsure = True
                    if current:
                        current.cancel()
                    for ev in list(REPORT_SPEECH):  # 報告の読み上げも止める
                        ev.set()
                    n = d.get("n")  # 画面の停止の番号。返事に付けて返す（画面は、次の発言を始めたあとに届いた古い返事で表示を変えない）
                    reply = {**({"scope": "talk"} if talk else {}), **({"n": n} if isinstance(n, int) and not isinstance(n, bool) else {}),
                             **({"unsure": True} if unsure else {})}
                    if not talk:
                        last_stop.clear()
                        last_stop.update(reply)
                    await send(type="stopped", **reply)
                    continue
                if d["type"] == "begin":
                    # 画面が発言を始めた（送るを押した・録音を始めた）。発言はここで受け付ける（受け付けの番号を進め、直前に聞いた文をこの発言に
                    # 引き渡し、ハブ以外宛てなら前の問いを消す。この画面の前の送る前の指示は取り消す）。添付を送る間・録音の間に、ほかの画面から
                    # 言った「はい」が前の問いを使わないように（a2a 4135f4c2 の 2 回目）。そのときの停止の回数も控える（届くまでに止めたかを見る）
                    b = d.get("b")
                    if isinstance(b, int) and not isinstance(b, bool):
                        stop_now = STOP_GEN["n"]
                        sg = d.get("sg")  # 画面が発言を始めたときに知っていた停止の回数。最新でなければ、その回数を開始値にする（停止をまたいだと見分ける）
                        if isinstance(sg, int) and not isinstance(sg, bool):
                            stop_now = min(stop_now, sg)
                        if stopped_in_block():  # 聞き取りで受信が止まっていた間に届いていたかもしれない合図。その間に止めていたら、止まり始めた時点のものにする
                            stop_now = min(stop_now, blocked["stop"])
                        drop_direct()
                        adm = await admit_async(heard_seen(d))
                        if d.get("pane"):  # 宛先が分かるときだけ（ハブ以外宛てなら前の問いを消す）
                            left_hub(str(d["pane"]))
                        begins[b] = {"stop": stop_now, "adm": adm}
                        while len(begins) > 32:
                            begins.pop(next(iter(begins)))
                    continue
                if d["type"] == "sync":  # 画面が同期の合図を返した。それより前にこの画面が送ったもの（停止）は読み終えている
                    k = d.get("n")
                    if isinstance(k, int) and not isinstance(k, bool):
                        for j in [j for j in my_settle if j <= k]:
                            my_settle.discard(j)
                            v = SETTLING.pop(j, None)
                            if v is not None:
                                v[1] = True
                                v[0].set()
                    continue
                if d["type"] == "clientlog":  # 端末側（特にスマホ）の再生まわりの出来事を残す
                    print(f"[client] {str(d.get('msg'))[:300]}", flush=True)
                    continue
                pending = d  # {"type":"turn", pane, speaker, filename} → 次のバイナリが音声
                if d["type"] == "turn":  # 声の発言の頭が届いた。受け付けの番号はここで取る（聞き取りの間に届いた前の往復の返事を、聞いた文にしない）
                    crossed, adm, legacy = take_begin(d)
                    sg0 = STOP_GEN["n"]  # 受け付けの鍵を待つ前に、停止の回数を控える（待つ間に止めたら、この発言のハブ以外への指示は送らない。codex-review 31c9e1c）
                    drop_direct()  # 次の発言を読んだ。この画面の前の、まだ送っていないハブ以外への指示は、鍵を待つ前に送らずに知らせる（codex-review c72fbbe・31c9e1c）
                    pending = {**d, "_admitted": adm if adm is not None else await admit_async(heard_seen(d)), "_via": "device" if client.kind == "device" else "",
                               # 受け付けの鍵を待つ間に止めたら、停止をまたいだ発言にする（ハブへの「はい」でも、承認に答える・閉じるに使わない）
                               "_stopgen": sg0, "_crossed": crossed or STOP_GEN["n"] != sg0, "_legacy": legacy,
                               # 合図の無い更新前の画面の声（機械の口の接続は除く）は、録音を始めた時点が分からない。ハブ以外への直接の指示なら送らない（a2a 4135f4c2 の 3 回目）
                               "_legacy_drop": legacy and not client.bound}
                    if legacy:  # 合図のある発言は、合図を受けたときにハブの前の問いを消し済み。遅れて届いた頭で、あとの新しい問いを消さない（codex-review 52a66a0）
                        left_hub(str(d.get("pane", "")))
                if d["type"] == "text":
                    crossed, adm, legacy = take_begin(d)  # 合図があれば、受け付けは合図を受けたときのもの（前の問いの取り消しも済んでいる）
                    sg0 = STOP_GEN["n"]  # 受け付けの鍵を待つ前に、停止の回数を控える（待つ間に止めたら、この発言のハブ以外への指示は送らない。codex-review 31c9e1c）
                    if adm is None and HUB.on():  # 合図の無い文字の発言。受け付けはここで（鍵の空きを、イベントループを止めずに待つ）
                        drop_direct()  # 前の、まだ送っていないハブ以外への指示は、鍵を待つ前に送らずに知らせる（codex-review 31c9e1c）
                        adm = await admit_async(heard_seen(d))
                        left_hub(str(d.get("pane", "")))
                        crossed = crossed or STOP_GEN["n"] != sg0  # 鍵を待つ間に止めたら、停止をまたいだ発言にする（ハブへの「はい」でも、承認に答える・閉じるに使わない）
                    start_turn(d["text"], d["pane"], int(d["speaker"]), d.get("key"), reply_at_of(d), attachments_of(d), reply_item_of(d), seen=heard_seen(d),
                               crossed=crossed, admitted=adm, legacy=legacy, stopgen=sg0)
                continue
            if msg.get("bytes") is not None and pending.get("type") == "dropped":
                pending = {}
                # 画面は声を送ったあと「聞き取り中…」で待っているので、止めたと返して戻す。返事は最後の停止と同じ（番号・確かめられない）にする
                # （番号の無い「止めました」で、先に出した確かめの頼みを上書きしない。codex-review e7ab276）
                await send(type="stopped", **last_stop)
                continue
            if msg.get("bytes") is not None and pending.get("type") == "turn":
                # 聞き取りは受信の中で待つ。その間に押した「停止」は、聞き取った発言を送ったあとに読む（発言は相手に届くが、止めたことで
                # 受け付けの番号が進むので、その発言で承認に答える・閉じるは通らない。.github/review-exceptions.md の D-007）
                head, pending = pending, {}
                stopped_since = head.get("_stopgen") is not None and head["_stopgen"] != STOP_GEN["n"]
                if head.get("_crossed") or stopped_since:
                    # 録音を始めたあと（合図のあと）・声の頭を受けたあと、声の本体が届くまでに、どこかの画面で止めた。聞き取らずに捨て、録音した画面に
                    # 知らせる（ハブにも送らない。codex-review fdc6b04・52a66a0）。合図の無い更新前の画面の返信・添付つきの声は、読み込み直しを頼む
                    await send(type="error", text="この画面は古い版なので、話した内容は送っていません。画面を読み込み直してください"
                               if head.get("_legacy") and not stopped_since and STOP_GEN["n"] == head.get("_stopgen") else "止めたので、話した内容は聞き取らずに捨てました")
                    continue
                # 聞き取り・辞書の補正・知らせの送り出しの間は受信が止まる。その間に届いていた合図は、止まり始めた時点のものとして扱う
                # （受信に戻ってから 0.5 秒の間に読んだ合図。溜まっていた合図はすぐに読まれる。a2a 4135f4c2 の 2・3 回目）
                # 前の声の窓がまだ開いている（声が続いて届いていた）なら、早いほうの値を引き継ぐ（あとの声の処理で上書きしない。a2a 4135f4c2 の 4 回目）
                stop_at_block = blocked["stop"] if stopped_in_block() else STOP_GEN["n"]
                try:
                    await send(type="status", text="聞き取り中…")
                    try:
                        said = await asyncio.to_thread(transcribe, msg["bytes"], head.get("filename", "audio.webm"))
                    except (OSError, ValueError) as e:  # whisper が止まっている・答えの形が違う。接続は切らずに知らせる
                        print(f"[whisper] 聞き取りに失敗: {str(e)[:200]}", flush=True)
                        await send(type="error", text="聞き取りに失敗しました（聞き取りのサーバーを確かめてください）")
                        continue
                    if not said:
                        await send(type="error", text="聞き取れませんでした")
                        continue
                    text = await asyncio.to_thread(fix_heard, said)  # 画面とハブ・セッションに渡す文。守りの台帳には直す前の said を載せる
                    await send(type="transcript", pane=head["pane"], text=text)
                    start_turn(text, head["pane"], int(head["speaker"]), head.get("key"), reply_at_of(head), attachments_of(head),
                               reply_item_of(head), said if text != said else None, admitted=head["_admitted"], via=head["_via"], stopgen=head["_stopgen"],
                               crossed=head["_crossed"], legacy=head["_legacy"], legacy_drop=head["_legacy_drop"])
                finally:
                    blocked.update(stop=stop_at_block, end=STOP_GEN["n"], until=time.monotonic() + 0.5)  # 受信に戻る時点から数える
    except WebSocketDisconnect:
        pass
    finally:
        CLIENTS.discard(client)
        for j in list(my_settle):  # 返事の前に切れた（送られた停止が届かずに消えたかもしれない）。待っている答え・閉じるを放し、その発言では断らせる
            v = SETTLING.get(j)
            if v is not None:
                v[1] = False
                v[0].set()
        if win_of(client) == client.cid:  # 印を名乗らなかった接続の「話しかけた時刻」は、切れたら消す（ほかの接続に引き継がない）
            WIN_SPOKE.pop(client.cid, None)
        cancel.set()
        d = direct_turn["d"]
        if d is not None and not d["handed"] and DIRECT_PENDING.get(d["key"]) is d:
            # まだ送る仕事に渡していないハブ以外への指示（鍵・添付を待っている）。切れただけでは取り消さず、そのまま送る（画面には
            # 「送りました」と出ている。cancel が立っているので、返事は画面に送らない。ほかの画面の停止では取り消す。a2a 6604a50a の 2 回目）
            pass
        elif current:
            current.cancel()


hermes.setup(sys.modules[__name__])  # 頭の部品に本体を渡す（このモジュールの名前は、起動のしかたで server か __main__ になる）


def quiet_server(uvicorn, config):
    """止める合図（SIGTERM・SIGINT）を自分では受けない uvicorn の受け口。合図は serve_all がまとめて受け、全部の受け口を止めてから終える
    （uvicorn 0.54 の受け口は、止まったあとに合図を出し直してプロセスを終えるので、ほかの受け口の止める手順が飛ぶ。a2a ca4d694e）。"""
    class Quiet(uvicorn.Server):
        def capture_signals(self):
            return contextlib.nullcontext()
    return Quiet(config)


def device_servers(uvicorn) -> list:
    """機械の口（VL_DEVICE_PORTS）の受け口。この Mac の中（127.0.0.1）だけで待つ（tailnet へは Tailscale Serve が出す）。同じ本体（app）を使い、
    起動と終わりの処理（lifespan）は画面の口の受け口だけが受け持つ（見張りなどを二重に動かさない）。"""
    return [quiet_server(uvicorn, uvicorn.Config(app, host="127.0.0.1", port=p, proxy_headers=False, lifespan="off")) for p in sorted(device_ports())]


async def serve_all(main, extras: list):
    """画面の口の受け口と、機械の口の受け口を一緒に動かす。止める合図はここで受けて全部に伝え（2 回目は待たずに止める）、全部が止まってから戻る。
    画面の口の受け口が終われば（起動に失敗したときも）、機械の口の受け口も止める。"""
    loop = asyncio.get_running_loop()
    servers = [main, *extras]

    def stop():
        hard = any(x.should_exit for x in servers)
        for x in servers:
            x.should_exit = True
            x.force_exit = x.force_exit or hard

    sigs = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop)
            sigs.append(sig)
        except (NotImplementedError, RuntimeError):
            pass
    tasks = [asyncio.create_task(x.serve()) for x in extras]
    try:
        await main.serve()
    finally:
        for x in extras:
            x.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
        for sig in sigs:
            loop.remove_signal_handler(sig)


if __name__ == "__main__":
    import uvicorn

    # proxy_headers を切る: 接続元を X-Forwarded-For で書き換えさせない（コンテナが偽った値で、接続のもとの確かめを外せないように。origin_guard）
    main_server = quiet_server(uvicorn, uvicorn.Config(app, host=os.environ.get("VL_HOST", "127.0.0.1"), port=int(os.environ.get("VL_PORT", "8790")),
                                                       proxy_headers=False))
    asyncio.run(serve_all(main_server, device_servers(uvicorn)))
