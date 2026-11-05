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
import base64
import hashlib
import io
import json
import os
import re
import shutil
import stat
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
from origin_guard import SameOriginOnly  # noqa: E402  接続元の確かめ
import consent  # noqa: E402  利用者の発言が、許す・選ぶの答えになっているか（承認に答える・閉じるの守り）
from hub_brain import Brain  # noqa: E402  ハブの頭との約束
from places import CONFIG_DIR, STATE_DIR  # noqa: E402  端末ごとの設定と状態の置き場


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
WHISPER = os.environ.get("VL_WHISPER", "http://127.0.0.1:8080/v1/audio/transcriptions")
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
HUB_DIR = CONFIG_DIR / "hub"  # ここを作業ディレクトリにしている Claude Code セッションが「ハブ」。中の指示文は利用者が書き換えてよい
HUB_MODEL = os.environ.get("VL_HUB_MODEL", "sonnet")
HUB_WORKSPACE = os.environ.get("VL_HUB_WORKSPACE", "")  # ハブのタブを作る herdr ワークスペースの名前。空なら先頭のワークスペース
HUB_SETTLE = float(os.environ.get("VL_HUB_SETTLE", "30"))  # 起動時、herdr 自身のセッション復元が落ち着くのを待つ秒数
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
    out = p.stdout.strip() or p.stderr.strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"raw": out, "code": p.returncode}


def read_screen(pane: str, lines: int) -> str:
    """`herdr agent read` はプレーンテキストを返す。"""
    p = subprocess.run(["herdr", "agent", "read", pane, "--source", "recent-unwrapped", "--lines", str(lines)], capture_output=True, text=True, timeout=20)
    return "\n".join(l for l in p.stdout.splitlines() if l.strip())


STATUS_LINE = re.compile(r"^\s*(📂|⏵⏵|⏸|✻)")  # 状態表示の行（費用や経過時間で変わる）
OPTION = re.compile(r"^\s*(?:❯\s*)?(\d+)\.\s+(\S.*)$")


def read_prompt(pane: str) -> str:
    """承認・入力待ちで止まっている画面。対話の枠は visible でしか取れない（recent には出ない）。
    罫線と、内容に関係なく変わる状態表示の行は除く。"""
    p = subprocess.run(["herdr", "agent", "read", pane, "--source", "visible", "--lines", "45"], capture_output=True, text=True, timeout=20)
    keep = [l.rstrip() for l in p.stdout.splitlines() if l.strip() and not STATUS_LINE.match(l) and not set(l.strip()) <= set("─╌")]
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


def _agents() -> list[dict]:
    r = herdr("agent", "list")
    agents = r.get("result", {}).get("agents", [])
    return [
        {
            "pane": a["pane_id"],
            "title": a.get("terminal_title_stripped") or a.get("terminal_title", ""),
            "cwd": a.get("cwd", ""),
            "kind": a.get("agent"),
            "session": (a.get("agent_session") or {}).get("value"),
            "status": a.get("agent_status"),
            "name": a.get("name") or "",
            "hub": a.get("cwd", "") == str(HUB_DIR) and HUB.in_herdr,  # 頭が herdr の外のときは、hub フォルダの Claude Code はただのセッション
        }
        for a in agents
    ]


def _hub_unstarted(a: dict) -> bool:
    """会話を始める前の画面（フォルダの信頼、ログイン、初回の設定など）で止まっているハブか。
    herdr がセッション ID を持つのは Claude Code が会話を受け付けてからなので、「止まっていて ID が無い」で見分ける。"""
    return a["hub"] and a["status"] == "blocked" and not a["session"]


def list_agents() -> list[dict]:
    # 始まる前の画面で止まっているハブは、まだ話し相手にしない（文字を送ると、その画面に答えてしまう）
    return [a for a in _agents() if not _hub_unstarted(a)]


def agent_key(a: dict) -> str:
    """画面が「同じ相手か」を見分ける鍵。ハブは立て直すとセッション ID が変わるので "hub" で指す。"""
    return "hub" if a["hub"] else (a.get("session") or a["pane"])


_hub_lock = threading.Lock()
# 初期の選択は「No, exit」なので、Enter だけでは進まない
TRUST_TODO = ("herdr の hub タブを開くと、Claude Code がこのフォルダを信頼するか聞いています。↓ で「Yes, I trust this folder」を選び、Enter を押してください"
              "（初回だけ。ハブが ./hubctl を確認なしで実行できる設定を持っているために出る確認です）")
STUCK_TODO = ("herdr の hub タブを開くと、Claude Code が会話を始める前の画面で止まっています。画面の問いに答えてください"
              "（初回は、ログインや設定の確認が出ることがあります）。答えたあと、もう一度 start を実行するか「ハブを起動」を押します")


def hub_stuck() -> dict | None:
    """ハブが会話を始める前の画面で止まっていれば、その pane と、利用者に頼むこと。答えるのは利用者（代わりに答えない）。"""
    for p in herdr("pane", "list").get("result", {}).get("panes") or []:
        if p.get("cwd") == str(HUB_DIR) and pane_shows_trust(p["pane_id"]):
            return {"reason": "trust", "pane": p["pane_id"], "detail": TRUST_TODO}
    hub = next((a for a in _agents() if _hub_unstarted(a)), None)
    return {"reason": "stuck", "pane": hub["pane"], "detail": STUCK_TODO} if hub else None


class ClaudeBrain(Brain):
    """herdr の中の Claude Code がハブ。話す・記録は、ほかのセッションと同じ。"""

    in_herdr = True

    def ensure(self) -> dict:
        """ハブが居なければ立てる。Mac の再起動後に残る hub タブ（中身はシェル）があればその中で、無ければタブを作る。"""
        with _hub_lock:
            if not (HUB_DIR / "speaking.md").is_file():
                return {"ok": False, "reason": "hub_dir", "detail": f"ハブの作業フォルダがまだありません（{HUB_DIR}）。プラグインの start を実行してください"}
            stuck = hub_stuck()
            if stuck:
                return {"ok": False, **stuck}
            hub = next((a for a in list_agents() if a["hub"]), None)
            if hub:
                return {"ok": True, "pane": hub["pane"], "started": False}
            panes = herdr("pane", "list").get("result", {}).get("panes")
            if panes is None:
                return {"ok": False, "reason": "herdr が応答しません"}
            pane = next((p["pane_id"] for p in panes if p.get("cwd") == str(HUB_DIR) and not p.get("agent")), None)
            if not pane:
                wss = herdr("workspace", "list").get("result", {}).get("workspaces") or []
                if not wss:
                    return {"ok": False, "reason": "herdr にワークスペースがありません"}
                ws = next((w for w in wss if w.get("label") == HUB_WORKSPACE), wss[0])["workspace_id"]
                r = herdr("tab", "create", "--workspace", ws, "--cwd", str(HUB_DIR), "--label", "hub", "--no-focus")
                pane = ((r.get("result") or {}).get("root_pane") or {}).get("pane_id")
                if not pane:
                    return {"ok": False, "reason": f"タブを作れませんでした: {json.dumps(r, ensure_ascii=False)[:200]}"}
                time.sleep(2)  # シェルが立ち上がるのを待つ
            # 話し方の決まりはシステムプロンプトにも載せる（CLAUDE.md だけだと、列挙のときに箇条書きへ戻る）
            speaking = " ".join((HUB_DIR / "speaking.md").read_text().split())
            r = herdr("agent", "start", "hub", "--kind", "claude", "--pane", pane, "--timeout", "90000", "--",
                      "--model", HUB_MODEL, "--append-system-prompt", speaking, timeout=120)
            stuck = hub_stuck()
            if stuck:
                return {"ok": False, **stuck}
            if (r.get("error") or {}).get("code") == "agent_name_taken":
                return {"ok": False, "reason": "name_taken", "detail": "herdr に「hub」という名前の別のセッションがあります。そのタブを閉じてから、もう一度 start してください"}
            if "result" not in r:
                return {"ok": False, "reason": f"ハブを起動できませんでした: {json.dumps(r, ensure_ascii=False)[:200]}"}
            return {"ok": True, "pane": pane, "started": True}

    async def boot(self):
        """サーバー起動時（= Mac の再起動後）にハブを確かめる。herdr が開かれるまで待ち、復元が落ち着いてから1回だけ行う。"""
        for _ in range(360):  # 最長1時間
            if "result" in await asyncio.to_thread(herdr, "agent", "list"):
                break
            await asyncio.sleep(10)
        else:
            print("[hub] herdr が応答しないため、起動時のハブ確認をあきらめました", flush=True)
            return
        await asyncio.sleep(HUB_SETTLE)
        hlog(f"起動時の確認: {json.dumps(await asyncio.to_thread(ensure_hub), ensure_ascii=False)}", flush=True)

    def doctor(self, add):
        files = (HUB_DIR / "CLAUDE.md").is_file() and (HUB_DIR / "speaking.md").is_file()
        add("ハブの作業フォルダ", files, "プラグインの start を実行してください（指示文のひな形が置かれます）")
        if files:
            stuck = hub_stuck()
            if stuck:
                add("ハブ", False, stuck["detail"])
            else:
                add("ハブ", any(a["hub"] for a in list_agents()), "プラグインの start を実行するか、画面の「ハブを起動」を押してください")
        for talker in TALKERS.values():
            if talker.on():
                talker.doctor_as_talker(add)

    def turns(self, n: int = 1) -> list[tuple[str, str]]:
        return human_turns(hub_log(), n)

    def heard(self, turn: str) -> str:
        return heard_before(hub_log(), turn)

    def reply_to(self, turn: str) -> str:
        return reply_to(hub_log(), turn)

    async def report_target(self) -> dict | None:
        return next((a for a in await asyncio.to_thread(list_agents) if a["hub"]), None)


TALKERS = {hermes.TALKER.pane: hermes.TALKER}  # herdr の外の話し相手
HUB = hermes.TALKER if hermes.HERMES_HUB else ClaudeBrain()  # ハブの頭（config.env の VL_HUB）。約束は scripts/hub_brain.py


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


def human_turns(path: Path | None, n: int = 1) -> list[tuple[str, str]]:
    """ユーザー本人の発言の (ID, 文) を、新しいものから n 件（自動通知は数えない）。「ユーザーが新しく何か言ったか」を見分けるのに使う。"""
    if not (path and path.exists()):
        return []
    lines, out = read_lines(path, tail_bytes=600_000), []
    for i in range(len(lines) - 1, -1, -1):
        if len(out) >= n:
            break
        said = [text for kind, text in iter_events([lines[i]]) if kind == "user" and not text.startswith(NOTE_PREFIX)]
        if said:
            out.append((json.loads(lines[i]).get("uuid") or f"line-{i}", said[0]))
    return out


def reply_to(path: Path | None, turn: str) -> str:
    """記録の中の発言 turn への返答（最後のツール呼び出しより後の文 = ユーザーが聞いたもの）。見つからなければ空。"""
    if not (path and path.exists()):
        return ""
    lines = read_lines(path, tail_bytes=600_000)
    for i, line in enumerate(lines):
        if f'"{turn}"' in line and json.loads(line).get("uuid") == turn:
            end = next((j for j in range(i + 1, len(lines)) if any(k == "user" for k, _ in iter_events([lines[j]]))), len(lines))
            return last_exchange(lines[i:end])[1]
    return ""


def reply_since(path: Path, offset: int) -> str:
    """読み上げる本文。offset 以降の最後のターンの返答。"""
    return last_exchange(read_lines(path, offset=offset))[1]


# ---------- 音声 ----------
def _whisper(audio: bytes, filename: str, timeout: float = 120) -> dict:
    boundary = "----vl" + os.urandom(8).hex()
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + audio + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(WHISPER, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode(), strict=False)


def transcribe(audio: bytes, filename: str) -> str:
    text = _whisper(audio, filename).get("text", "")
    text = re.sub(r"\s+", " ", text).strip()
    if text.replace("。", "").replace(" ", "") in HALLUCINATIONS:
        return ""
    return text


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


def heard_before(path: Path | None, turn: str) -> str:
    """Claude Code のハブの記録で、発言 turn の行より前の、ハブの最後の返答（発言か自動通知への返答。最後のツール呼び出しより後の文をまとめたもの）。
    turn の行より後（いまの発言の中でハブが書く文）は使わない。見つからなければ空。"""
    if not (path and path.exists()):
        return ""
    lines = read_lines(path, tail_bytes=600_000)
    for i in range(len(lines) - 1, -1, -1):
        try:
            uid = json.loads(lines[i]).get("uuid")
        except json.JSONDecodeError:
            continue
        if uid == turn:
            return last_exchange(lines[:i])[1]
    return ""


def hub_heard(turn: str | None) -> str:
    """利用者が発言 turn の直前に聞いたハブの返答（本体が受け取った原文）。分からなければ空（守りは断る側に倒れる）。"""
    return HUB.heard(turn) if turn else ""


def hub_reply_to(turn: str | None) -> str:
    """発言 turn へのハブの返答（ユーザーが聞いた文）。"""
    return HUB.reply_to(turn) if turn else ""


@app.get("/api/agents")
def api_agents():
    out = [{**a, "key": agent_key(a)} for a in list_agents()]
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
    hub = pane in TALKERS or bool(me and me["hub"])
    if me is None and not hub:
        return None, "そのセッションがありません"
    home, roots, named = Path.home().resolve(), [], []  # named: (ハブが見ているフォルダ名＝cwd の末尾。hub_sessions の project と同じ, たどった先)
    for a in agents if hub else [me]:
        if a["hub"] or not a.get("cwd"):
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
                return None, "ハブの会話では、絶対パスか「フォルダ名/場所」だけを見ます（そのフォルダ名のセッションが無いか、二つ以上あります）"
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
    return next((a for a in list_agents() if a["pane"] == pane and not a["hub"]), None)


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
        if a["hub"]:
            continue
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


@app.get("/api/hub/screen")
def hub_screen(pane: str, lines: int = 30):
    a = _worker(pane)
    if not a:
        return JSONResponse({"error": f"セッションが見つかりません: {pane}"}, status_code=404)
    if a["status"] != "blocked":
        return JSONResponse({"pane": pane, "status": a["status"], "screen": read_screen(pane, lines)})
    text = read_prompt(pane)
    SEEN_PROMPT[pane] = (prompt_fp(text), hub_human_turn())  # ハブがこの問いを見た。答えられるのは、ユーザーの次の発言から
    return JSONResponse({"pane": pane, "status": "blocked", "screen": text, "prompt": parse_prompt(text)})


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


def _whisper_problem() -> str:
    """短い無音を実際に聞き取らせる（同じポートで別のサービスが動いているだけ、を「使える」と数えないため）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"\0\0" * 4800)  # 0.3秒
    try:
        ok = "text" in _whisper(buf.getvalue(), "probe.wav", timeout=30)
    except (urllib.error.HTTPError, ValueError):  # 何かは応答したが、whisper の返事の形ではない
        ok = False
    except OSError as e:
        return f"whisper のサーバーが {WHISPER} で動いていません（{str(e)[:80]}）。起動してください。入れ方は README の「準備」にあります"
    return "" if ok else f"{WHISPER} は応答しますが、whisper の返事ではありません。別のサービスが同じポートを使っていないか確かめ、config.env の VL_WHISPER を合わせてください"


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
        "ハブに新しいセッションを立てさせるなら、config.env の VL_PROJECTS_DIR にプロジェクトの置き場所を書いてください", optional=True)
    return JSONResponse({"ok": all(c["ok"] or c["optional"] for c in checks), "checks": checks, "config_dir": str(CONFIG_DIR), "narrator": model})


@app.post("/api/hub/start")
def hub_start():
    """ハブが居なければ立てる（画面の「ハブを起動」と hub-start から呼ばれる）。"""
    r = ensure_hub()
    hlog(f"起動の依頼: {json.dumps(r, ensure_ascii=False)}", flush=True)
    return JSONResponse(r, status_code=200 if r["ok"] else 503)


# ---------- 画面との接続・セッションとの一往復・読み上げ（1対1でも、ハブからの報告でも使う） ----------
class Client:
    """接続中の画面ひとつ。"""

    def __init__(self, sock: WebSocket):
        self.sock = sock
        self.speaker: int | None = None  # この画面で選ばれている声
        self.voice = True  # この画面で読み上げるか（画面の 🔇 で切ると、返答は文字だけになる）
        self.visible = False  # この画面がいま見えているか。見えている画面が1つでもあれば、プッシュ通知は送らない（画面に声と文字で届くため）
        self.seen = 0.0  # 画面が最後に様子（hello）を知らせてきた時刻

    async def send(self, **msg):
        await self.sock.send_text(json.dumps(msg, ensure_ascii=False))


CLIENTS: set[Client] = set()
PANE_LOCKS: dict[str, asyncio.Lock] = {}  # ひとつのセッションへの「送って返信を読む」は同時に1件だけ
REPORTING = False  # ハブが自動通知に応答している最中か
REPORT_IDLE: asyncio.Event | None = None  # 報告が終わったときに立つ（Slack から来た送信が、報告の終わりを待つのに使う）。イベントループの中で作る
SLACK_QUEUE_WAIT = 45.0  # Slack から来た送信が、報告の終わりを待つ上限（秒）。送り（herdr の上限 30 秒）と合わせて hubctl send の待ち（100 秒）に収める
REPORT_SEQ = 0  # 報告の通し番号（報告を始めるたびに進む）
REPORT_FAILED: dict[int, bool] = {}  # 番号ごとに、報告が失敗・時間切れで終わったか（直近の分だけ持つ）
LAST_DONE: dict[str, tuple] = {}  # セッションごとの、最後に報告した (ターンの目印, 返答)。同じ返答を二度知らせないため
_tasks: set[asyncio.Task] = set()


def pane_lock(pane: str) -> asyncio.Lock:
    return PANE_LOCKS.setdefault(pane, asyncio.Lock())


async def ask(agent: dict, text: str) -> tuple[str, str]:
    """セッションに送り、返信が出そろうまで待つ。("done", 返信) / ("blocked", 画面の末尾) / ("error", 理由) を返す。"""
    talker = TALKERS.get(agent["pane"])
    if talker:  # herdr の外の話し相手。止まる（blocked）ことはない
        try:
            return "done", await asyncio.to_thread(talker.ask, text)
        except Exception as e:  # 待ちきれなかった（TimeoutError）なども、失敗として返す（報告の記録に残すため）
            return "error", str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}"
    pane = agent["pane"]
    path = jsonl_path(agent)
    offset = path.stat().st_size if path and path.exists() else 0  # 新しいセッションは最初の発言まで記録が無い
    # --wait: 送信後に working を観測してから idle/done/blocked を待つ（送信直後の idle に反応しない）
    r = await asyncio.to_thread(herdr, "agent", "prompt", pane, text, "--wait", "--timeout", "600000", timeout=610)
    err = (r.get("error") or {}).get("code")
    if err == "agent_prompt_stalled":  # 5秒以内に working にならなかった。短い応答で見逃した可能性があるので通常待ちへ
        r = await asyncio.to_thread(herdr, "agent", "wait", pane, "--timeout", "600000", timeout=610)
    elif err or "result" not in r:
        return "error", json.dumps(r, ensure_ascii=False)[:200]
    state = str((r.get("result") or {}).get("agent", {}).get("agent_status") or (r.get("result") or {}).get("agent_status") or "")
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


async def speak_to(clients: list[Client], text: str, cancel: asyncio.Event):
    """話し言葉に直し、文ごとに合成して画面へ送る。声は画面ごとの選択に従う。切れた画面は飛ばす。
    読み上げを切った画面には音声を送らない（文字は先に reply で届いている）。聞く画面が無ければ、語り手も合成も呼ばない。"""
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
        async with aclosing(narrate(text, cancel)) as sentences:  # 途中で抜けたら、語り手の生成もすぐ止める
            async for sentence in sentences:
                # 読み上げの途中で切った画面は、次の文から外す。聞く画面が無くなれば、そこで止める
                voice = {c: DEFAULT_SPEAKER if c.speaker is None else c.speaker for c in alive if c.voice}  # VOICEVOX では 0 も声の ID
                if not voice:
                    break
                wavs = {sp: await asyncio.to_thread(synth, sentence, sp) for sp in set(voice.values())}
                if cancel.is_set() or not alive:
                    break
                await each(lambda c: c.sock.send_bytes(wavs[voice[c]]) if c in voice and c.voice else asyncio.sleep(0))
                n += 1
    await each(lambda c: c.send(type="done", sentences=n))


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


async def push_all(title: str, body: str, tag: str, urgency: str = "normal", only: list[dict] | None = None) -> str:
    """どの画面も見えていなければ、購読している端末すべてに知らせる。結果は sent / visible / none / failed / error。
    only を渡すと、見えている画面の有無に関係なく、その購読だけに送る（🔔 を押したときの試しの通知）。"""
    if only is None and any(c.visible and time.time() - c.seen < VISIBLE_TTL for c in CLIENTS):
        return "visible"
    subs = only if only is not None else await asyncio.to_thread(push_subs)
    if not subs:
        return "none"
    payload = {"title": _clip(title, 80), "body": _clip(body, 120), "tag": tag, "url": "/"}
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
                    name = "ハブ" if a["hub"] else f"{a['cwd'].rsplit('/', 1)[-1]}｜{a['title']}"
                    await push_all(name, "承認・入力待ちで止まっています", f"blocked-{p}", urgency="high")
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
                task_set(d.get("task"), "closed", "ハブが閉じました")
                relay_slack(d, label, "のセッションは、ハブが閉じました", "", slack_report.SCREEN)
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
        tail = {"no_hub": "（ハブが居ないので、声の報告はありません）", "failed": "（ハブから報告を受け取れませんでした）",
                "waiting": "（ハブの報告は、このあと届きます）"}.get(outcome, "")
        while True:
            r = await push_all(f"{d['project']}｜{d['title']}", PUSH_BY_REPORT[kind] + tail, f"report-{d['pane']}")
            if r != "visible":
                st["settled"] = True
                return
            if not st["next"]:  # 画面が見えていて送らなかった。あとでハブの報告ができたとき、もう一度確かめる
                return
            outcome, st["next"] = st["next"], None
            tail = {"no_hub": "（ハブが居ないので、声の報告はありません）", "failed": "（ハブから報告を受け取れませんでした）"}.get(outcome, "")

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
        relay_slack(d, label, headline, "")
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
        try:
            state, reply = await ask(hub, note)
        finally:
            REPORT_FAILED[seq] = state != "done" or not reply.strip()  # 失敗・時間切れ・空の返事で終わった報告を待っていた送信は、通さない（下の「failed」と同じ判定）
            for old in [k for k in REPORT_FAILED if k < seq - 50]:
                REPORT_FAILED.pop(old, None)
            REPORTING = False
            report_idle().set()
        hlog(f"報告 {kind}: {label} → {state}（画面 {len(CLIENTS)}）", flush=True)
        saved = await HUB.report_end(state, reply, label, saved)
        if state != "done" or not reply.strip():
            relay_slack(d, label, headline, "")
            if kind in ("done", "more", "gone"):
                d["_slack_done"] = True
            return "failed"
        notify("ok")
        relay_slack(d, label, headline, reply)  # 報告は REPORTING の中で作られたもの（その間、ハブは次の指示を送れない）
        if kind in ("done", "more", "gone"):
            d["_slack_done"] = True  # 完了の報告を送った。このあとの見張りの失敗は重ねて知らせない（途中経過のあとの失敗は知らせる）
        clients = list(CLIENTS)
        for c in clients:
            try:
                await c.send(type="reply", pane=hub["pane"], text=reply, report=True, about=label, saved=saved)
            except Exception:
                clients.remove(c)
        if clients:
            await speak_to(clients, reply, asyncio.Event())
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
    d["chain"] = [*prev.get("chain", []), {k: prev.get(k) for k in ("text", "offset", "slack", "overlap", "session", "sent_gen")}]
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


def relay_slack(d: dict, label: str, headline: str, reply: str, fallback: str = slack_report.NO_REPORT):
    """Slack から頼まれた件なら、報告を同じスレッドにも送る（裏で。声と画面の報告を遅らせない）。送り先は slack_report.origin が決めた DM だけ。
    同じセッションに続けて頼んでまとめた件（chain）は、一番新しい Slack のスレッドに 1 回だけ送り、まとめた依頼を並べる。
    ハブの報告（reply）は、ハブが hubctl read --task でこの件の範囲を読み分けて書いたもの。本文を載せないのは、この件の記録を読めないときだけ
    （送ったときと別のセッションになった・記録が無い・読めない・送った指示が記録に見つからない）。そのときは定型文にする。
    この件の範囲に、ほかのやり取りが混ざったら、本文の頭に注意の 1 行を付ける（ハブが別の件の答えと取り違えていたら、あなたが気づけるように）:
    - セッションの記録で、この件の指示のほかに利用者の側の行がある（端末・画面・声から直接の発言・ほかの件の指示・ほかのセッションやエージェントからの知らせ・読めない行）か、
      最初の指示が作業中に届いた（別の作業の返答と同じターンに続く）
    - 報告を待つ間に、同じセッションへ次の指示が送られようとした（SENT_GEN。送る前に進む・増えるだけ）か、送る途中でほかの指示と重なった（overlap）か、
      まとめた依頼の間に、画面・声から直接の発言があった（SENT_GEN が続いていない）"""
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
        text = slack_report.text_for(label, headline, "", fallback, asks)
        if reply.strip():
            text = slack_report.text_for(label, headline, "", slack_report.NOT_FOUND, asks)
            path = d.get("path")
            try:
                first = chain[0]["offset"] if chain else d["offset"]
                texts = [e["text"] for e in chain] + [d["text"]]
                if path and one_session and await asyncio.to_thread(same_session, d):
                    _msgs, found, mixed = await asyncio.to_thread(task_range, Path(path), first, texts)
                    if found:
                        text = slack_report.text_for(label, headline, reply, asks=asks, note=slack_report.MIXED if mixed or not quiet else "")
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
    OPEN_WATCH[pane] = d
    start_watch(d, kind)
    if slack_chain(d):
        task_set(d.get("task"), "watching", f"同じセッションへの前の {len(slack_chain(d))} 件の指示と、まとめて報告します")
    return {"ok": True, "project": project, "title": a["title"], "was_working": a["status"] == "working", "task": d["task"], "slack": bool(slack)}


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
SEEN_PROMPT: dict[str, tuple[str, str | None]] = {}  # ハブが見た、止まっている画面: pane → (画面の指紋, そのときのユーザーの最後の発言)
CONFIRMING: dict[str, tuple[str, str | None]] = {}  # 取り消しにくい操作の聞き返し中: pane → (画面の指紋, そのときのユーザーの最後の発言)


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
    if is_permission(text):
        text = TOOL_MARK.sub("", text)
    return hashlib.sha1(" ".join(text.split()).encode()).hexdigest()


def hub_log() -> Path | None:
    hub = next((a for a in list_agents() if a["hub"]), None)
    return jsonl_path(hub) if hub else None


def hub_human_turn() -> str | None:
    return next((turn for turn, _ in hub_turns()), None)


@app.post("/api/hub/answer")
async def hub_answer(body: dict):
    """止まっているセッションの問いに、ユーザーの答えを伝える。
    守ること: ハブが見てユーザーに伝えた問いにだけ答える（見たのと同じ画面で、かつ見たあとにユーザーが発言している）。
    「はい」と番号は、ユーザーがその発言の直前に聞いたハブの返答が、プロジェクト名を挙げた問いで（not_asked）、
    発言全体が、その答えの文法に収まるときだけ（not_said。scripts/consent.py）。断る（いいえ）はいつでも送れる。
    取り消しにくい操作の「はい」は、聞き返してユーザーがもう一度発言してから（どちらの発言も上の確かめを通ること）。「今後は聞かずに許可」は選ばない。"""
    if REPORTING:
        return JSONResponse({"ok": False, "reason": "reporting"})
    pane, answer, confirmed = str(body.get("pane", "")), str(body.get("answer", "")).strip().lower(), bool(body.get("confirmed"))
    a = await asyncio.to_thread(_worker, pane)
    if not a:
        return JSONResponse({"ok": False, "reason": f"セッションが見つかりません: {pane}"}, status_code=404)
    if a["status"] != "blocked":
        return JSONResponse({"ok": False, "reason": "not_blocked", "status": a["status"]})
    trust = await asyncio.to_thread(pane_shows_trust, pane)
    if trust is None:  # 何を聞かれているか読めないまま、答えを送らない
        return JSONResponse({"ok": False, "reason": "unreadable"})
    if trust:  # フォルダを信頼するかは、利用者が端末で答える
        return JSONResponse({"ok": False, "reason": "trust"})
    text = await asyncio.to_thread(read_prompt, pane)
    (turn, utter), = (await asyncio.to_thread(hub_turns, 1) or [(None, "")])[:1]
    fp, prompt, project = prompt_fp(text), parse_prompt(text), a["cwd"].rsplit("/", 1)[-1]
    if answer in ("no", "n"):
        key, said = "esc", "いいえ（取り消し）"
    else:
        seen = SEEN_PROMPT.get(pane)
        if not seen or seen[0] != fp:  # 見ていない、または見たあとで問いが変わった
            SEEN_PROMPT[pane] = (fp, turn)
            return JSONResponse({"ok": False, "reason": "unseen", "screen": text})
        if seen[1] == turn:  # 見たのと同じ発言の中で答えようとしている = ユーザーはまだ中身を聞いていない
            return JSONResponse({"ok": False, "reason": "same_turn", "screen": text})
        if answer not in ("yes", "y") and not answer.isdigit():
            return JSONResponse({"ok": False, "reason": "bad_answer"})
        if answer in ("yes", "y") and prompt["kind"] != "permission":
            return JSONResponse({"ok": False, "reason": "not_permission", "options": prompt["options"]})
        if answer.isdigit() and (prompt["kind"] != "question" or int(answer) not in prompt["options"]):
            return JSONResponse({"ok": False, "reason": "bad_choice", "kind": prompt["kind"], "options": prompt["options"]})
        heard = await asyncio.to_thread(hub_heard, turn)
        if not consent.asks(heard, project):  # ユーザーは、この相手の問いだと分かる文を聞いていない
            return JSONResponse({"ok": False, "reason": "not_asked", "project": project, "screen": text})
        if not (consent.affirms(utter, "answer", project) if answer in ("yes", "y") else consent.chooses(utter, int(answer), prompt["options"], project)):
            return JSONResponse({"ok": False, "reason": "not_said", "said": _clip(utter, 80), "project": project})
        if answer in ("yes", "y"):
            if RISKY.search(asked_part(text)):
                held = CONFIRMING.get(pane)
                if not (confirmed and held and held[0] == fp and held[1] != turn):
                    CONFIRMING[pane] = (fp, turn) if not (held and held[0] == fp) else held
                    return JSONResponse({"ok": False, "reason": "confirm", "screen": text})
            key, said = "1", "はい（今回だけ許可）"
        else:
            n = int(answer)
            key, said = str(n), f"{n}番「{prompt['options'][n]}」"
    r = await asyncio.to_thread(herdr, "agent", "send-keys", pane, key)
    hlog(f"answer → {pane} ({project}): {said} {'ok' if 'result' in r else 'ERR'}", flush=True)
    if "result" not in r:
        return JSONResponse({"ok": False, "reason": "herdr が応答しません"})
    SEEN_PROMPT.pop(pane, None)
    CONFIRMING.pop(pane, None)
    if not WATCHING.get(pane):  # ハブ経由で頼んだ件でなければ見張りが無いので、答えたあとの結果を知らせるために付ける
        path = jsonl_path(a)
        start_watch({"pane": pane, "cwd": a["cwd"], "project": project, "title": a["title"], "text": "", "generic": True,
                     "offset": path.stat().st_size if path and path.exists() else 0, "was_working": True,
                     "gen": WATCH_GEN.setdefault(pane, 0)}, "answer")
    return JSONResponse({"ok": True, "project": project, "title": a["title"], "said": said})


# ---------- 画面のボタンからの返答（利用者がタップで答える） ----------
# ボタンを出して答えられるのは、Claude Code の承認の画面と言い切れるときと、問いが 1 つで 1 つを選ぶ質問と言い切れるときだけ。
# 画面の文字の読み取りは揺れるので、それ以外（複数選択・問いが複数・preview 付きの質問・フォルダの信頼・読めない・わからない・
# セッション ID が無い）は中身を見せるだけで、キーは送らない
QUESTION_ONLY = ("Type something.", "Chat about this")  # 選択式の質問（AskUserQuestion）にだけある選択肢
QUESTION_CHIP = re.compile(r"^\s*(?:←\s+)?☐ ")  # 質問の見出しの印（複数選択・問いが複数のときは「←  ☐ 朝  ☐ 夜  ✔ Submit  →」）
QUESTION_OPTION = re.compile(r"^(?:❯ | {2})(\d+)\. (\S.*)$")  # 質問の選択肢の行。説明の行は 5 文字下げて出る
NOT_PLAIN = re.compile(r"[│┌┐└┘├┤]|^\[[ x✔✓]\]")  # preview の枠・複数選択の印


def question_head(lines: list[str]) -> int | None:
    """末尾の操作案内から上へたどって、いちばん近い質問の見出しの行。"""
    return next((i for i in range(len(lines) - 2, -1, -1) if QUESTION_CHIP.match(lines[i])), None)


def parse_question(text: str) -> dict | None:
    """問いが 1 つで、選択肢から 1 つを選ぶ質問（AskUserQuestion）の画面なら、見出し・問い・選択肢。言い切れなければ None。
    数字キーはその番号を選んで確定する（2026-11-05 実画面で確認）。言い切る形は、実画面で取ったものと同じ並び:
    いちばん下の行が操作案内（Enter to select · ↑/↓ to navigate · Esc to cancel）、その上に見出しの印・問い・選択肢（1 から順に。
    説明の行は 5 文字下げ）・「Type something.」・「Chat about this」。複数選択（[ ] と Submit）・問いが複数（← → と Tab/Arrow keys）・
    preview 付き（枠と n to add notes）・番号が飛ぶか重なる・ほかの行が混じる画面は None（ボタンを出さない）。"""
    lines = text.splitlines()
    if not lines:
        return None
    foot = lines[-1]
    if not all(w in foot for w in ("Enter to select", "↑/↓ to navigate", "Esc to cancel")) or "Tab/Arrow" in foot or "to add notes" in foot:
        return None
    head = question_head(lines)
    if head is None or any(w in lines[head] for w in ("Submit", "←", "→")):
        return None
    body = lines[head + 1:-1]
    first = next((i for i, l in enumerate(body) if QUESTION_OPTION.match(l)), None)
    if not first:  # 選択肢が無い、または問いの行が無い
        return None
    opts: list[list] = []  # [番号, 見出し, 説明の行]
    for l in body[first:]:
        m = QUESTION_OPTION.match(l)
        if m:
            opts.append([int(m.group(1)), m.group(2).strip(), []])
        elif l.startswith(" " * 5):
            opts[-1][2].append(l.strip())
        else:
            return None
    if [o[0] for o in opts] != list(range(1, len(opts) + 1)) or len(opts) < 3:
        return None
    if [o[1] for o in opts[-2:]] != list(QUESTION_ONLY) or opts[-1][2] or opts[-2][2]:
        return None
    real = opts[:-2]
    if any(NOT_PLAIN.search(label) or label in QUESTION_ONLY for _, label, _ in real):
        return None
    return {"header": lines[head].strip()[1:].strip(), "question": "\n".join(l.strip() for l in body[:first]),
            "choices": [{"n": n, "label": label, "desc": " ".join(desc)} for n, label, desc in real]}


def dialog_part(text: str) -> str | None:
    """質問の画面のうち、見出しの印から下。印が無ければ None。"""
    lines = text.splitlines()
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


def _ask_of(a: dict) -> dict:
    """止まっているセッション a の問い。kind は permission（ボタンを出す）/ question / trust / unknown / none（止まっていない）/ unreadable。
    question は、1 つを選ぶ質問と言い切れて、セッション ID もあるときだけ choices（選択肢のボタン）を付ける。"""
    if a["status"] != "blocked":
        return {"kind": "none"}
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
    if kind == "question":
        dialog = dialog_part(text)
        if dialog:  # 質問は見出しの印から見せる（会話の上の部分は出さない）
            out["text"] = _clip(dialog, 1200)
        q = parse_question(text) if a.get("session") else None
        if q:
            out.update(header=_clip(q["header"], 40), question=_clip(q["question"], 600),
                       choices=[{"n": c["n"], "label": _clip(c["label"], 120), "desc": _clip(c["desc"], 200)} for c in q["choices"]])
    return out


def _asked(pane: str, key: str) -> tuple[dict | None, dict]:
    """pane と key が指すセッションと、その問い。key はセッション ID で必須（同じ場所でセッションが立て直されていたら別物として断る。
    ハブも、話し相手の選択に使う "hub" ではなく、いまのセッション ID で見分ける）。"""
    a = next((a for a in list_agents() if a["pane"] == pane), None)
    if not a or not key or a.get("session") != key:
        return None, {"kind": "gone"}
    return a, _ask_of(a)


@app.get("/api/prompts")
def api_prompts():
    """止まっているセッションすべての問い。画面はこれを上に並べ、承認の画面にだけボタンを出す。key はセッション ID。"""
    out = []
    for a in list_agents():
        if a["status"] == "blocked":
            asked = _ask_of(a)
            if asked["kind"] != "none":
                out.append({"pane": a["pane"], "key": a.get("session") or "", "name": "ハブ" if a["hub"] else f"{a['cwd'].rsplit('/', 1)[-1]}｜{a['title']}", **asked})
    return JSONResponse(out)


_answer_lock = asyncio.Lock()  # 確かめてからキーを送るまでを、一度にひとつだけにする
ANSWERED: dict[str, tuple[str, float]] = {}  # 画面のボタンで答えた問い: pane → (指紋, 時刻)
ANSWERED_HOLD = 15  # 答えたあと、相手の状態が変わるまでのあいだ、同じ問いへの重ねた答えを断る秒数


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
    done = ANSWERED.get(pane)
    if done and done[0] == fp and time.time() - done[1] < ANSWERED_HOLD:  # 二度押し・重ねた送信
        return JSONResponse({"ok": False, "reason": "answered"})
    if choice not in ("allow", "deny", "choose"):
        return JSONResponse({"ok": False, "reason": "bad_choice"})
    a, asked = await asyncio.to_thread(_asked, pane, key)
    if asked["kind"] in ("gone", "none", "unreadable", "trust"):
        return JSONResponse({"ok": False, "reason": asked["kind"]})
    if choice == "choose" and not asked.get("choices"):  # 1 つを選ぶ質問と言い切れないときは、何も送らない
        return JSONResponse({"ok": False, "reason": "not_choice"})
    if choice != "choose" and asked["kind"] != "permission":  # 承認の画面と見分けられないときは、何も送らない
        return JSONResponse({"ok": False, "reason": "not_permission"})
    if asked["fp"] != fp:  # 画面に出したあとで問いが変わった
        return JSONResponse({"ok": False, "reason": "changed"})
    if choice == "allow":
        if asked["risky"] and not confirmed:
            return JSONResponse({"ok": False, "reason": "confirm"})
        keys, said = "1", "許可（今回だけ）"
    elif choice == "deny":
        keys, said = "esc", "断る"
    else:  # 画面から読んだ選択肢の番号だけを送る（Type something. と Chat about this は選ばない）
        n = body.get("n")
        picked = next((c for c in asked["choices"] if type(n) is int and c["n"] == n), None)
        if not picked:
            return JSONResponse({"ok": False, "reason": "bad_choice"})
        if asked["risky"] and not confirmed:
            return JSONResponse({"ok": False, "reason": "confirm"})
        keys, said = str(n), f"{n}番「{_clip(picked['label'], 40)}」"
    r = await asyncio.to_thread(herdr, "agent", "send-keys", pane, keys)
    hlog(f"answer(画面) → {pane} ({a['cwd'].rsplit('/', 1)[-1]}): {said} {'ok' if 'result' in r else 'ERR'}", flush=True)
    if "result" not in r:
        return JSONResponse({"ok": False, "reason": "herdr"})
    ANSWERED[pane] = (fp, time.time())
    return JSONResponse({"ok": True, "said": said})


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
    held = CLOSING.get(pane)
    (now, said), (before, _) = (await asyncio.to_thread(hub_turns, 2) + [(None, "")] * 2)[:2]
    heard = await asyncio.to_thread(hub_reply_to, held[1]) if confirmed and held and held[1] else ""
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
            if not asks_to_close(heard, project):
                why = "not_asked"
            elif not consent.affirms(said, "close", project):
                why = "not_yes"
            else:
                del CLOSING[pane]  # 閉じる前に外す（重ねて届いた同じ許可で、二度閉じない）
                CLOSED.add(pane)
                r = await asyncio.to_thread(herdr, "pane", "close", pane)
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
# 代理（Hermes）が、見張りの回（cron）で見つけた判断待ちなどを知らせる口。本体は、題と一言を文字として画面の一覧に出し、
# プッシュ通知には題だけを載せる。知らせには押して動くものが無い（送る・答える・立てる・閉じるの口とはつながらない）。
# 中身は代理が外から読んだものの要約なので、引用データとして文字で出すだけにする（HTML にしない）。
NOTICES_FILE = STATE_DIR / "notices.json"
NOTICE_TITLE, NOTICE_TEXT = 60, 400  # 字数の上限（超えた分は切る）
NOTICE_KEEP, NOTICE_DAYS = 50, 7  # 残す数と日数
NOTICE_PER_HOUR = 20  # 1 時間に受ける数（消したものも数える）。見張りが暴れても、通知が鳴り続けないように
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
    """{"next": 次の番号, "rows": 残っている知らせ（古い順）, "sent": 直近 1 時間に受けた時刻}。
    記録は本体の状態フォルダにあるが、壊れていても・手で書き換えられていても、受けるときと同じ形にそろえ直してから使う。"""
    try:
        d = _read_json(NOTICES_FILE, {}) if not NOTICES_FILE.exists() or NOTICES_FILE.stat().st_size <= NOTICE_FILE_MAX else {}
    except OSError:
        d = {}
    now = time.time()
    rows = []
    for r in d.get("rows") if isinstance(d.get("rows"), list) else []:
        if not isinstance(r, dict) or not _notice_time(r.get("at"), now) or now - r["at"] >= NOTICE_DAYS * 86400:
            continue
        n, title, text = r.get("n"), _notice_clean(r.get("title"), NOTICE_TITLE), _notice_clean(r.get("text"), NOTICE_TEXT)
        if _notice_n(n) and title and text is not None:
            rows.append({"n": n, "title": title, "text": text, "at": float(r["at"])})
    # 時計が戻っても上限が外れないよう、未来の時刻（1 日まで）も数え続ける
    sent = [float(t) for t in (d.get("sent") if isinstance(d.get("sent"), list) else []) if _notice_time(t, now) and now - t < 3600]
    top = max([0] + [r["n"] for r in rows])
    nxt = d.get("next")
    nxt = nxt if _notice_n(nxt) and nxt > top else top + 1
    return {"next": nxt, "rows": rows[-NOTICE_KEEP:], "sent": sent}


@app.post("/api/hub/notice")
async def hub_notice(body: dict):
    """hubctl notice。代理が知らせる題と一言を受け、画面の一覧に足し、画面が開いていればそこへ、閉じていればプッシュ通知（題だけ）で知らせる。"""
    title, text = _notice_clean(body.get("title"), NOTICE_TITLE), _notice_clean(body.get("text", ""), NOTICE_TEXT)
    if not title or text is None:
        return JSONResponse({"ok": False, "reason": "bad_request"}, status_code=400)
    with _notice_lock:
        d = notices()
        if len(d["sent"]) >= NOTICE_PER_HOUR or d["next"] >= NOTICE_N_MAX:
            return JSONResponse({"ok": False, "reason": "too_many", "max": NOTICE_PER_HOUR})
        row = {"n": d["next"], "title": title, "text": text, "at": time.time()}
        write_json(NOTICES_FILE, {"next": row["n"] + 1, "rows": (d["rows"] + [row])[-NOTICE_KEEP:], "sent": d["sent"] + [row["at"]]})
    hlog(f"知らせ [{row['n']}]（題 {len(title)}字・一言 {len(text)}字）", flush=True)
    for c in list(CLIENTS):
        try:
            await c.send(type="agent_notice", n=row["n"], title=title, sender=HUB.label)
        except Exception:
            pass
    push = await push_all(f"{HUB.label} から", title, f"notice-{row['n']}")
    return JSONResponse({"ok": True, "n": row["n"], "push": push})


@app.get("/api/notices")
def api_notices():
    """画面の 💡 の「知らせ」。新しい順。コンテナからは使えない口。"""
    with _notice_lock:
        d = notices()
    return JSONResponse({"sender": HUB.label, "rows": list(reversed(d["rows"]))})


@app.post("/api/notices/dismiss")
def api_notices_dismiss(body: dict):
    """画面の ×（1 件）・すべて消す。コンテナからは使えない口。"""
    n, every = body.get("n"), body.get("all") is True
    if not every and (not isinstance(n, int) or isinstance(n, bool)):
        return JSONResponse({"ok": False, "reason": "bad_request"}, status_code=400)
    with _notice_lock:
        d = notices()
        keep = [] if every else [r for r in d["rows"] if r["n"] != n]
        write_json(NOTICES_FILE, {**d, "rows": keep})
    return JSONResponse({"ok": True, "left": len(keep)})


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    client = Client(sock)
    CLIENTS.add(client)
    cancel = asyncio.Event()  # 進行中のターンを止める合図。ターンごとに作り直す
    current: asyncio.Task | None = None
    send = client.send

    async def turn(text: str, pane: str, cancel: asyncio.Event, key: str | None):
        try:
            await run_turn(text, pane, cancel, key)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # 語り手・音声合成・herdr の失敗を黙って止めず、画面とログに出す
            traceback.print_exc()
            await send(type="error", text=f"{type(e).__name__}: {e}"[:300])

    async def run_turn(text: str, pane: str, cancel: asyncio.Event, key: str | None):
        talker = TALKERS.get(pane)
        if talker:  # herdr のセッションではなく、本体の外の話し相手
            if not talker.on():
                await send(type="error", gone=True, text=talker.off_text)
                return
            await send(type="status", text=talker.sending_text())
            # 画面が閉じても、止めても、返事は最後まで待って記録する（相手は答えて覚えているので、記録が欠けると画面と食い違う）。
            # ハブが自動通知に応答している最中なら、それが終わってから送る
            job = asyncio.create_task(talker.exchange(text))
            _tasks.add(job)
            job.add_done_callback(_tasks.discard)
            reply, why, saved = await asyncio.shield(job)
            if why:
                await send(type="error", text=why[:300], saved=saved)
                return
            if cancel.is_set():
                return
            await send(type="reply", pane=pane, text=reply, saved=saved)  # saved=False なら、画面は記録から描き直さずに足す
            await speak_to([client], reply, cancel)
            return
        agent = next((a for a in list_agents() if a["pane"] == pane), None)
        if not agent:
            await send(type="error", gone=True, text="話し相手のセッションが見つかりません。選び直してください。")
            return
        if key and agent_key(agent) != key:  # 開きっぱなしの画面の一覧が古く、同じ場所に別のセッションが居る
            await send(type="error", gone=True, text="話し相手が入れ替わっています。送らずに止めました。選び直してください。")
            return
        await send(type="status", text="エージェントに送りました。考え中…")
        SENT_GEN[pane] = SENT_GEN.get(pane, 0) + 1  # 画面・声からの直接の指示も、前の件の Slack への報告に新しい結果を載せない（送る前に進める）
        async with pane_lock(pane):  # ハブが自動通知に応答している最中なら、それが終わってから送る
            SENT_GEN[pane] = SENT_GEN.get(pane, 0) + 1  # 順番を待つ間に、ハブ経由の件が送られていることがある。実際に送る直前にも進める
            # 返事が来るまで（止められても、送る処理が裏で終わるまで）に送られたハブ経由の件は、重なった件として扱う
            state, reply = await asyncio.shield(track_send(pane, ask(agent, text)))
            if cancel.is_set():
                return
            if state == "error":
                await send(type="error", text=f"送信できませんでした: {reply}")
            elif state == "blocked":
                await send(type="status", text="承認待ちです")
                await send(type="blocked", pane=pane)  # 画面に、許可・断るのボタンを出させる
                await speak_to([client], "【承認待ちの画面】\n" + reply, cancel)
            elif not reply.strip():
                await send(type="error", text="回答を読めませんでした")
            else:
                await send(type="reply", pane=pane, text=reply)
                await speak_to([client], reply, cancel)

    def start_turn(text: str, pane: str, speaker: int, key: str | None):
        nonlocal cancel, current
        client.speaker = speaker
        cancel.set()  # 前のターンの読み上げが残っていれば止める
        if current and not current.done():
            current.cancel()
        cancel = asyncio.Event()
        current = asyncio.create_task(turn(text, pane, cancel, key))

    pending: dict = {}
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
                    client.visible = d.get("visible") is True  # visible を送らない古い画面は、見えていないものとして扱う（通知を止めない）
                    client.seen = time.time()
                    continue
                if d["type"] == "stop":
                    cancel.set()
                    if current:
                        current.cancel()
                    await send(type="stopped")
                    continue
                if d["type"] == "clientlog":  # 端末側（特にスマホ）の再生まわりの出来事を残す
                    print(f"[client] {str(d.get('msg'))[:300]}", flush=True)
                    continue
                pending = d  # {"type":"turn", pane, speaker, filename} → 次のバイナリが音声
                if d["type"] == "text":
                    start_turn(d["text"], d["pane"], int(d["speaker"]), d.get("key"))
                continue
            if msg.get("bytes") is not None and pending.get("type") == "turn":
                await send(type="status", text="聞き取り中…")
                text = await asyncio.to_thread(transcribe, msg["bytes"], pending.get("filename", "audio.webm"))
                if not text:
                    await send(type="error", text="聞き取れませんでした")
                    continue
                await send(type="transcript", pane=pending["pane"], text=text)
                start_turn(text, pending["pane"], int(pending["speaker"]), pending.get("key"))
    except WebSocketDisconnect:
        pass
    finally:
        CLIENTS.discard(client)
        cancel.set()
        if current:
            current.cancel()


hermes.setup(sys.modules[__name__])  # 頭の部品に本体を渡す（このモジュールの名前は、起動のしかたで server か __main__ になる）


if __name__ == "__main__":
    import uvicorn

    # proxy_headers を切る: 接続元を X-Forwarded-For で書き換えさせない（コンテナが偽った値で、接続のもとの確かめを外せないように。origin_guard）
    uvicorn.run(app, host=os.environ.get("VL_HOST", "127.0.0.1"), port=int(os.environ.get("VL_PORT", "8790")), proxy_headers=False)
