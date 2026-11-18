"""件（話題）。頼んだ件・ハブの報告・Slack のスレッド・そのセッションの問いを、1 つの単位として結ぶ（0.29.0。件を中心にするビジョンの 1 段目）。

件の正本は本体の状態フォルダ（topics.json）に 1 つ。画面・プッシュ通知・Slack からは、件の URL（/#t=<件>）で開く。
件の URL で開くのは、選ぶ・出すだけ（その件の問いの欄を見せる・その件の報告を「↩ 返信」の状態にする）。答える・送るは、必ず利用者が押す。

件の分け方（2026-11-19 本人が A を選んだ）: 続きなら同じ件、それ以外は新しい件。続きとは、
- まだ報告の来ていない同じセッションへの依頼（#54 で報告をまとめる依頼）
- Slack の同じスレッドからの依頼
- 画面の「↩ 返信」で報告に返した発言の中の依頼
件の id は推測しにくい乱数（t と 16 進 10 桁）。件の URL を知っていても、開けるのは tailnet の中の画面だけで、開いても何も送らない。"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
from pathlib import Path

from places import STATE_DIR

TOPICS_FILE = STATE_DIR / "topics.json"
TOPIC_KEEP = 14 * 86400  # 最後に動いてから残す期間
TOPIC_MAX = 300
ID_RE = re.compile(r"t[0-9a-f]{10}")
FIELDS = {"id": str, "created": (int, float), "updated": (int, float), "pane": str, "session": str, "project": str, "title": str}
_lock = threading.Lock()
TOPICS: dict[str, dict] = {}


def _write(rows: list[dict]):
    """書きかけを残さないよう、一時ファイルに書いてから置き換える。中身は利用者だけが読める（0600）。"""
    TOPICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOPICS_FILE.with_name(TOPICS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    tmp.chmod(0o600)
    tmp.replace(TOPICS_FILE)


def _save():
    """残す範囲（最後に動いてから 14 日以内で、新しい順に 300 件まで）に絞って書く。書けなくても止めない（_lock の中で呼ぶ）。"""
    now = time.time()
    rows = sorted((t for t in TOPICS.values() if now - t["updated"] < TOPIC_KEEP), key=lambda t: t["updated"], reverse=True)[:TOPIC_MAX]
    TOPICS.clear()
    TOPICS.update({t["id"]: t for t in rows})
    try:
        _write(rows)
    except OSError:
        traceback.print_exc()


def _live(t: dict) -> bool:
    """最後に動いてから TOPIC_KEEP の内の件か（期限を過ぎた件は、保存し直す前でも引かない・続きにしない）。"""
    return time.time() - t["updated"] < TOPIC_KEEP


def _slack(v) -> list[str] | None:
    return [v[0], v[1]] if isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(x, str) for x in v) else None


def load():
    """起動のとき、件の台帳を戻す。壊れた行は飛ばす。"""
    try:
        rows = json.loads(TOPICS_FILE.read_text())
    except FileNotFoundError:
        return
    except (OSError, ValueError, RecursionError):
        traceback.print_exc()
        return
    with _lock:
        for t in rows if isinstance(rows, list) else []:
            if not (isinstance(t, dict) and all(isinstance(t.get(k), ty) for k, ty in FIELDS.items()) and ID_RE.fullmatch(t["id"])):
                continue
            direct = t.get("direct") if isinstance(t.get("direct"), (int, float)) and not isinstance(t.get("direct"), bool) else None
            TOPICS[t["id"]] = {**{k: t[k] for k in FIELDS}, "slack": _slack(t.get("slack")), **({"direct": direct} if direct else {})}
        _save()


def new(pane: str, session: str, project: str, title: str, slack=None) -> str:
    """新しい件を作り、id を返す。"""
    now = time.time()
    with _lock:
        tid = "t" + os.urandom(5).hex()
        while tid in TOPICS:
            tid = "t" + os.urandom(5).hex()
        TOPICS[tid] = {"id": tid, "created": now, "updated": now, "pane": str(pane), "session": str(session or ""),
                       "project": str(project), "title": " ".join(str(title).split())[:80], "slack": _slack(slack)}
        _save()
    return tid


def touch(tid: str, pane: str, session: str, slack=None) -> bool:
    """件が続いた（同じ件に次の依頼が来た）。いまの相手と Slack のスレッドを、新しいほうに置き換える。件が無ければ False。"""
    with _lock:
        t = TOPICS.get(tid)
        if not t or not _live(t):
            return False
        t.update(updated=time.time(), pane=str(pane), session=str(session or ""))
        t.pop("direct", None)  # ハブがまたこの件で頼んだ。いまの問いはこの件のものに戻る
        if _slack(slack):
            t["slack"] = _slack(slack)
        _save()
        return True


def get(tid) -> dict | None:
    """件（写し）。id の形が違う・無ければ None。"""
    if not isinstance(tid, str) or not ID_RE.fullmatch(tid):
        return None
    with _lock:
        t = TOPICS.get(tid)
        return dict(t) if t and _live(t) else None


def by_slack(dest, session: str = "") -> str | None:
    """Slack の同じスレッドから頼まれた件（一番新しいもの）。スレッドの無い（DM の一番上の）依頼は、続きと見なさない。
    session を渡すと、そのセッションの件だけから選ぶ（同じスレッドから別のセッションにも頼んだとき、あとの件に紛れない）。"""
    d = _slack(dest)
    if not d or not d[1]:
        return None
    with _lock:
        hit = [t for t in TOPICS.values() if t.get("slack") == d and _live(t) and (not session or t["session"] == session)]
        return max(hit, key=lambda t: t["updated"])["id"] if hit else None


def latest_for_session(session: str) -> str | None:
    """そのセッションの、一番新しい件（承認・入力待ちの知らせや、承認への答えを、どの件のものとするか）。"""
    if not session:
        return None
    with _lock:
        hit = [t for t in TOPICS.values() if t["session"] == session and _live(t)]
        return max(hit, key=lambda t: t["updated"])["id"] if hit else None


def current_for_session(session: str) -> str | None:
    """そのセッションの、いまの問いを結んでよい件（一番新しい件で、そのあと画面・声から直接送られていないもの）。無ければ None。"""
    tid = latest_for_session(session)
    t = get(tid) if tid else None
    return tid if t and not t.get("direct") else None


def mark_direct(session: str):
    """画面・声から（ハブを通さずに）そのセッションへ直接送った。そのセッションの一番新しい件に印を付け、いまの問いをその件のものとしない
    （直接送った別の話の問いを、前の件のリンクで見せない）。印は、ハブがまたその件で頼んだら外す。"""
    if not session:
        return
    with _lock:
        hit = [t for t in TOPICS.values() if t["session"] == session and _live(t)]
        if not hit:
            return
        max(hit, key=lambda t: t["updated"])["direct"] = time.time()
        _save()


# ---------- 件の URL（Slack に載せるリンク） ----------
# 画面の外向きの URL は、起動のときではなく使うときに tailscale serve から読む（10 分は覚える）。
# config.env の VL_PUBLIC_URL があれば、そちらを使う（https://ホスト[:ポート] の形だけ）。どちらも無ければリンクを付けない
URL_RE = re.compile(r"https://[A-Za-z0-9.-]+(?::\d{1,5})?")
_BASE = {"url": None, "at": 0.0}
BASE_TTL = 600
TAILSCALE_APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


def _tailscale_url() -> str | None:
    """tailscale serve でこのサーバー（VL_PORT）を出していれば、その URL（scripts/ctl.py の phone_url と同じ読み方）。"""
    ts = shutil.which("tailscale") or next((p for p in ["/opt/homebrew/bin/tailscale", "/usr/local/bin/tailscale", TAILSCALE_APP] if Path(p).is_file()), None)
    if not ts:
        return None
    port = os.environ.get("VL_PORT", "8790")
    try:
        web = json.loads(subprocess.run([ts, "serve", "status", "--json"], capture_output=True, text=True, timeout=10).stdout).get("Web") or {}
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
        return None
    for host, v in web.items() if isinstance(web, dict) else []:
        handlers = (v or {}).get("Handlers") if isinstance(v, dict) else None
        if isinstance(handlers, dict) and any(isinstance(h, dict) and str(h.get("Proxy", "")).rstrip("/").endswith(f":{port}") for h in handlers.values()):
            url = "https://" + str(host).removesuffix(":443")
            return url if URL_RE.fullmatch(url) else None
    return None


def base_url() -> str | None:
    env = os.environ.get("VL_PUBLIC_URL", "").strip().rstrip("/")
    if env:
        return env if URL_RE.fullmatch(env) else None
    if time.time() - _BASE["at"] < BASE_TTL:
        return _BASE["url"]
    url = _tailscale_url()
    _BASE.update(url=url, at=time.time())
    return url


def link(tid) -> str | None:
    """件を画面で開く URL。外向きの URL が分からない・件の id の形が違えば None（リンクを付けない）。裏の処理から呼ぶ（tailscale を動かすことがある）。"""
    if not isinstance(tid, str) or not ID_RE.fullmatch(tid):
        return None
    base = base_url()
    return f"{base}/#t={tid}" if base else None
