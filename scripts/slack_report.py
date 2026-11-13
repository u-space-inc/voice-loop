"""Slack から頼まれた件の報告を、同じ Slack のスレッドにも返す。

Hermes の Slack の DM で頼まれた指示（hubctl send・start）は、片づいたときの報告が voice-loop の画面と声にしか届かなかった。
本体は、その件の報告（自動通知への応答として、ハブが REPORTING の中で作ったもの。報告の途中は次の指示を送れない）を、
Hermes の送信コマンド（`hermes send`。Hermes のボットとして投稿する）で、頼まれたスレッドに送る。Hermes を起こさない。

守り:
- 送れる先は、config.env の VL_SLACK_REPORT_CHAT に書いた 1 つの DM だけ。hubctl が伝える会話（Hermes の端末の環境変数）は
  コンテナの中の Hermes が偽れるので、DM が違えば送らない。スレッドは数字の形だけを通す
- 本文の `MEDIA:` は、`hermes send` が手元のファイルを添付する印なので、全角のコロンに替えて添付にさせない。
  `<…|…>`（見た目と行き先の違うリンク）を作らせないよう、< > も全角に替える。制御文字・向きを変える文字は空白にする
- 送るのはコンテナの中の固定のコマンドで、本文は標準入力で渡す（シェルを通さない）
"""
from __future__ import annotations

import os
import re
import subprocess

from hermes_skills import USER, docker_bin

CHAT = os.environ.get("VL_SLACK_REPORT_CHAT", "").strip()  # 報告を送ってよい、ただ 1 つの DM（利用者と Hermes のボットの DM）
CHAT_RE = re.compile(r"D[A-Z0-9]{8,12}")  # Slack の DM のチャンネル ID
TS_RE = re.compile(r"\d{1,12}\.\d{1,12}")  # Slack のメッセージの ts（スレッドの親）
LIMIT = 3500  # 1 通の字数の上限（Slack の 1 通の上限より十分小さく）
HERMES = "/opt/hermes/.venv/bin/hermes"
CONTAINER = os.environ.get("VL_HERMES_CONTAINER", "hermes")  # Hermes のコンテナ（本体は頭の固有の設定を持たないので、ここで読む）
RUN_LIMIT, HOST_WAIT = 30, 45
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff]")  # 改行とタブは残す
_MEDIA = re.compile(r"MEDIA\s*:", re.I)


def origin(raw) -> tuple[str, str] | None:
    """hubctl が伝えた会話（{"platform", "chat", "thread"}）を、送ってよい届け先 (DM, スレッド) に直す。送れなければ None。"""
    if not (CHAT and CHAT_RE.fullmatch(CHAT)) or not isinstance(raw, dict):
        return None
    if raw.get("platform") != "slack" or raw.get("chat") != CHAT:
        return None
    thread = raw.get("thread") or ""
    if not isinstance(thread, str) or (thread and not TS_RE.fullmatch(thread)):
        return None
    return CHAT, thread


NO_REPORT = "（ハブの報告を受け取れませんでした。voice-loop の画面で確かめてください）"
REPLACED = "（報告の前に、同じセッションへ次の指示が送られました。この件の結果は voice-loop の画面で確かめてください）"
UNVERIFIED = "（この件の指示のあとに同じセッションへ別の発言があったか、記録で確かめられませんでした。結果は voice-loop の画面で確かめてください）"
SCREEN = "（詳しくは voice-loop の画面で確かめてください）"


def text_for(label: str, headline: str, reply: str, fallback: str = NO_REPORT) -> str:
    """Slack に送る文。見出し（案件｜件名と、何が起きたか）と、ハブの報告。報告が無ければ fallback（画面で確かめるよう書く）。"""
    body = reply.strip() or fallback
    text = f"【{label}{headline}】\n{body}"
    text = _MEDIA.sub("MEDIA：", _CTRL.sub(" ", text)).replace("<", "＜").replace(">", "＞")
    return text if len(text) <= LIMIT else text[: LIMIT - 1] + "…"


def command(container: str, dest: tuple[str, str]) -> list[str] | None:
    exe = docker_bin()
    if not exe:
        return None
    chat, thread = dest
    target = f"slack:{chat}:{thread}" if thread else f"slack:{chat}"
    return [exe, "exec", "-i", "-u", USER, container, "timeout", "--kill-after=5", str(RUN_LIMIT), HERMES, "send", "-q", "-t", target, "-f", "-"]


def post(dest: tuple[str, str], text: str, wait: float = HOST_WAIT, container: str | None = None) -> str:
    """送る。結果は ok か、送れなかった理由の短い語（中身は書かない）。"""
    cmd = command(container or CONTAINER, dest)
    if not cmd:
        return "docker が見つかりません"
    try:
        p = subprocess.run(cmd, input=text.encode("utf-8"), capture_output=True, timeout=wait)
    except subprocess.TimeoutExpired:
        return "時間切れ"
    except OSError as e:
        return f"動かせません（{type(e).__name__}）"
    return "ok" if p.returncode == 0 else f"終了コード {p.returncode}"
