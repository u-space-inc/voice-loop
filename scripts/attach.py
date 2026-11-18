"""画面から添付したファイルを受け取り、話し相手（セッション・ハブ）に渡すための部品（2026-11-16 本人が推奨を選んだ）。

受け取るのは画像（png・jpg・gif・webp・heic）・PDF・テキスト（txt・md・csv）だけ。1 ファイル MAX まで、1 回の発言に COUNT 個まで。
置き場所は、セッション宛ては本体の状態フォルダ（attachments/<日付>/）、ハブ（Hermes）宛てはコンテナの中の /opt/data/voice-loop/attachments/。
渡し方は、発言の末尾に「添付（画面から）」の行で場所を書く（相手が自分の道具で開く）。

守り（README「守り」）。相手は、別のサイトのページ（画面の口へは Origin の確かめがある）と、乗せられた Hermes（置き場を同じ利用者で書ける）:
  - ファイルの名前は本体が付ける（日時と乱数）。送られた名前は、拡張子と、画面と発言に出す表示の名前（制御文字を外して 1 行・80 字）にだけ使う
  - 中身の先頭を拡張子と照らす（画像と PDF は決まった頭、テキストは UTF-8 で NUL が無い）。合わなければ受け取らない
  - 状態フォルダには O_EXCL・O_NOFOLLOW で 0600 に書く。7 日で消す（名前の形が合うふつうのファイルだけ）
  - Hermes 宛ては、ホストの側から置き場に書かない。コンテナの中で Hermes の利用者として、固定の台本（SCRIPT）で、フォルダを symlink をたどらずに
    1 段ずつ開いて O_EXCL・O_NOFOLLOW で書く
"""
import json
import os
import re
import secrets
import stat
import subprocess
import time
from pathlib import Path

MAX = 20 * 1024 * 1024  # 1 ファイルの上限
COUNT = 5  # 1 回の発言に付けられる数
KEEP = 7 * 86400  # 置いておく日数
TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".heic": "image/heic",
         ".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv"}
NAME_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{8}\.(?:png|jpg|jpeg|gif|webp|heic|pdf|txt|md|csv)")
_DROP = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁠-⁩﻿\ud800-\udfff]")
HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"}
MARK = "vl-attach-put"  # コンテナの中の台本の目印
CONTAINER_DIR = "/opt/data/voice-loop/attachments"


def _clean(name) -> str:
    """フォルダの部分を外し、制御文字・向きを変える文字を空白にして 1 行にした名前（長さはそのまま）。"""
    base = re.split(r"[/\\]", str(name or ""))[-1]
    return " ".join(_DROP.sub(" ", base).split())


def shown_name(name) -> str:
    """画面と発言に出す名前: _clean を 80 字までにしたもの。空なら「添付」。"""
    v = _clean(name)
    return (v if len(v) <= 80 else v[:79] + "…") or "添付"


def ext_of(name) -> str:
    """拡張子は、80 字に切る前の名前から取る（長い名前で拡張子が欠けて、正しいファイルを断らないように）。"""
    return os.path.splitext(_clean(name))[1].lower()


def content_problem(ext: str, data: bytes) -> str:
    """中身が拡張子と合うか。合えば ""、合わなければ理由。"""
    ok = {".png": lambda: data.startswith(b"\x89PNG\r\n\x1a\n"),
          ".jpg": lambda: data[:3] == b"\xff\xd8\xff", ".jpeg": lambda: data[:3] == b"\xff\xd8\xff",
          ".gif": lambda: data[:6] in (b"GIF87a", b"GIF89a"),
          ".webp": lambda: data[:4] == b"RIFF" and data[8:12] == b"WEBP",
          ".heic": lambda: data[4:8] == b"ftyp" and data[8:12] in HEIC_BRANDS,
          ".pdf": lambda: data.startswith(b"%PDF-")}.get(ext)
    if ok is not None:
        return "" if ok() else f"中身が {ext} の形ではありません"
    if ext in (".txt", ".md", ".csv"):
        if b"\x00" in data:
            return "テキストに NUL があります"
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return "テキストが UTF-8 ではありません"
        return ""
    return "扱えない種類です"


def new_name(ext: str, now: float | None = None) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now or time.time())) + "-" + secrets.token_hex(4) + ext


def save(root: Path, name: str, data: bytes) -> Path:
    """状態フォルダの attachments/<日付>/ に 0600 で書く（O_EXCL・O_NOFOLLOW。名前は new_name の形だけ）。書いた場所を返す。"""
    if not NAME_RE.fullmatch(name):
        raise ValueError("名前の形が違います")
    d = root / name[:8]
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    if d.is_symlink() or root.is_symlink():
        raise ValueError("置き場が symlink です")
    fd = os.open(d / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return d / name


def tidy(root: Path, now: float | None = None):
    """KEEP より古い添付を消す（名前の形が合うふつうのファイルだけ。symlink はたどらない）。空になった日付のフォルダも消す。"""
    now = now or time.time()
    if not root.is_dir() or root.is_symlink():
        return
    for d in root.iterdir():
        if not re.fullmatch(r"\d{8}", d.name) or d.is_symlink() or not d.is_dir():
            continue
        for f in d.iterdir():
            try:
                st = f.lstat()
            except OSError:
                continue
            if NAME_RE.fullmatch(f.name) and stat.S_ISREG(st.st_mode) and now - st.st_mtime > KEEP:
                f.unlink(missing_ok=True)
        try:
            d.rmdir()
        except OSError:
            pass


# ---------- コンテナの中の台本（ハブ（Hermes）宛て） ----------
# 引数は「目印 名前」。中身を stdin で受け取り、結果を「VLRESULT {JSON}」の 1 行で返す（hermes_skills と同じ読み方）。
# 置き場（VL_ATTACH_ROOT、ふだんは /opt/data）の voice-loop/attachments を、根から 1 段ずつ symlink をたどらずに開き（無ければ作る）、
# O_EXCL・O_NOFOLLOW で書く。同じフォルダの、KEEP より古い名前の形の合うふつうのファイルを消す。
_SCRIPT_MAIN = r'''
import json, os, re, stat, sys, time


def done(**kw):
    sys.stdout.write("\nVLRESULT " + json.dumps(kw, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    os._exit(0)


name = sys.argv[2]
if not NAME_RE.fullmatch(name):
    done(result="error", why="名前の形が違います")
data = sys.stdin.buffer.read(MAX + 1)
if len(data) > MAX:
    done(result="error", why="大きすぎます")
root = os.environ.get("VL_ATTACH_ROOT", "/opt/data")
fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    for p in ("voice-loop", "attachments"):
        try:
            nfd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        except FileNotFoundError:
            os.mkdir(p, 0o750, dir_fd=fd)
            nfd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        os.close(fd)
        fd = nfd
    now = time.time()
    for old in os.listdir(fd):
        st = os.stat(old, dir_fd=fd, follow_symlinks=False)
        if NAME_RE.fullmatch(old) and stat.S_ISREG(st.st_mode) and now - st.st_mtime > KEEP:
            os.unlink(old, dir_fd=fd)
    ffd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640, dir_fd=fd)
    with os.fdopen(ffd, "wb") as f:
        f.write(data)
except OSError as e:
    done(result="error", why=type(e).__name__ + ": " + str(e)[:160])
finally:
    try:
        os.close(fd)
    except OSError:
        pass
done(result="put", path=root.rstrip("/") + "/voice-loop/attachments/" + name)
'''
SCRIPT = f"NAME_RE = __import__('re').compile({NAME_RE.pattern!r})\nMAX = {MAX}\nKEEP = {KEEP}\n" + _SCRIPT_MAIN


def put_container(docker: str | None, container: str, user: str, python: str, name: str, data: bytes, env: dict | None = None) -> str:
    """ハブ（Hermes）のコンテナの中に置き、コンテナの中の場所を返す。置けなければ RuntimeError。"""
    if not docker:
        raise RuntimeError("docker が見つかりません")
    extra = [a for k, v in (env or {}).items() for a in ("-e", f"{k}={v}")]
    try:
        p = subprocess.run([docker, "exec", "-i", "-u", user, *extra, container, "timeout", "--kill-after=5", "60", python, "-c", SCRIPT, MARK, name],
                           input=data, capture_output=True, timeout=90)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise RuntimeError(f"コンテナに置けませんでした（{type(e).__name__}）")
    line = next((l for l in reversed(p.stdout.decode("utf-8", "replace").splitlines()) if l.startswith("VLRESULT ")), "")
    try:
        out = json.loads(line[len("VLRESULT "):]) if line else {}
    except ValueError:
        out = {}
    if out.get("result") != "put" or not isinstance(out.get("path"), str):
        raise RuntimeError(f"コンテナに置けませんでした（{out.get('why') or (p.stderr.decode('utf-8', 'replace').strip().splitlines() or ['答えがありません'])[-1][:160]}）")
    return out["path"]
