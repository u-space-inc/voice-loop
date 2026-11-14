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

同じセッションに続けて頼んだ件（前の件の完了を報告する前に、次の件を送った）は、まとめて 1 つの報告にし、
一番新しい依頼のスレッドに送る（server.py の merge_open・chain_dest）。前のスレッドには、次の依頼のスレッドで報告すると一言送る。

報告は、ハブがこの件の範囲（指示を送ったところから今まで。hubctl read --task）を読み分けて書いたもの。その範囲にほかのやり取りが
混ざったときは、本文の頭に注意の 1 行（MIXED）を付ける。この件の記録を読めないときだけ、本文を載せずに定型文（NOT_FOUND）を送る。
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
NOT_FOUND = "（この件の指示を、セッションの記録で見つけられませんでした。結果は voice-loop の画面で確かめてください）"  # セッションが作り直された・記録が読めないなど
MIXED = "（この件の間に、同じセッションでほかのやり取りがありました。違っていたら、このスレッドで言ってください）"  # ハブが読み分けた報告の頭に付ける
SCREEN = "（詳しくは voice-loop の画面で確かめてください）"
# 同じセッションに続けて頼んでまとめた報告で、それぞれの依頼がどこから来たか
HERE, OTHER_THREAD, FROM_SCREEN = "このスレッドから", "前のスレッドから", "voice-loop から"
ASKS_SHOWN = 5  # まとめた報告に並べる依頼の数の上限（多いときは新しいものを残す）


def one_line(text: str, n: int) -> str:
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[: n - 1] + "…"


def text_for(label: str, headline: str, reply: str, fallback: str = NO_REPORT, asks=(), note: str = "") -> str:
    """Slack に送る文。見出し（案件｜件名と、何が起きたか）と、ハブの報告。報告が無ければ fallback（画面で確かめるよう書く）。
    asks は、同じセッションに続けて頼んでまとめた依頼の (文, どこから) の並び（古い順）。あれば見出しのあとに並べる。
    note は、報告の頭に付ける 1 行（この件の間にほかのやり取りが混ざったときの MIXED）。報告が無ければ付けない。"""
    body = (f"{note}\n{reply.strip()}" if note else reply.strip()) if reply.strip() else fallback
    if asks:
        shown = list(asks)[-ASKS_SHOWN:]
        lines = [f"【{label}{headline}】（{len(asks)} 件まとめて）"]
        if len(asks) > len(shown):
            lines.append(f"・ほか {len(asks) - len(shown)} 件")
        lines += [f"・{one_line(t, 50)}（{where}）" for t, where in shown]
        text = "\n".join(lines) + f"\n\n{body}"
    else:
        text = f"【{label}{headline}】\n{body}"
    text = _MEDIA.sub("MEDIA：", _CTRL.sub(" ", text)).replace("<", "＜").replace(">", "＞")
    return text if len(text) <= LIMIT else text[: LIMIT - 1] + "…"


def pointer_text(label: str, next_text: str) -> str:
    """前のスレッドへの一言。同じセッションへ別のスレッドから次の依頼が来たので、報告はそちらにまとめる。"""
    return text_for(label, " への依頼は、次の依頼とまとめて、そちらのスレッドで報告します", f"次の依頼：「{one_line(next_text, 50)}」")


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
