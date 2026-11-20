"""出ていく側の記録（利用者の側が Teams に投稿した記録）を、案件のフォルダの Teams の控えから読む。

利用者の Teams の返信は、多くを作業セッションが利用者の承認で投稿し、案件のフォルダの控え `teams-sync-log.md`
（teams-sync の手順の記録）に、確認した回ごとの行として残している。見張りが読めるのは届いた側（メンション）だけなので、
代理（Hermes）が「判断待ちが済んだか」を確かめられるように、この控えの「当方」の行を読むだけの口で渡す（hubctl sent）。

- 探すのは、置き場所（VL_PROJECTS_DIR）の直下の各フォルダの中の `teams-sync-log.md`（深さ MAX_DEPTH まで。
  隠しフォルダ・依存のフォルダ・symlink はたどらない。symlink のフォルダ・ファイルは読まない）。
- 読むのは、表の行の「最後の投稿（日時・発言者）」の欄が「当方」のもの（そのスレッドの最後の投稿が、こちらの投稿）と、
  控えと同じフォルダの `thread/` にある `<年>-<月>-<日>_<時><分>_out_<要約>.md` の名前（中身は読まない）。
- 作業木の控えに加えて、手元の枝（最後のコミットが GIT_DAYS 日以内）の同じ場所の控えも読む（セッションは控えの行を枝で
  コミットし、PR でマージするので、マージ前の行は枝にだけある）。git は読むだけで、取りに行かない（fetch しない）。
- 控えに書かれない投稿（手で直接送ったもの）と、Teams 以外は出ない。控えは機微な値を書かない決まりの記録だが、
  中身は案件のやり取りの要約（引用データ）として渡す。
"""
from __future__ import annotations

import datetime as dt
import os
import re
import stat
import subprocess
import threading
import time
from pathlib import Path

LOG_NAME = "teams-sync-log.md"
MAX_DEPTH = 4            # 案件のフォルダからの深さ（doc/internal/teams-sync-log.md は 3）
MAX_BYTES = 2_000_000    # これより大きい控えは読まない
MAX_LOGS = 5             # 1 つの案件で読む控えの数
FIND_TTL = 300           # 控えの場所を探し直すまでの秒
READ_TTL = 60            # 控えを読み直すまでの秒
GIT_DAYS = 14            # 手元の枝のうち、最後のコミットがこの日数以内のものの控えも読む
GIT_REFS = 12            # 1 つの案件で読む枝の数
GIT_TIMEOUT = 5
WALK_DIRS = 20000        # 1 回に探すフォルダの数の上限（置き場所の全案件で）
BUDGET = 15              # 1 回の読み取りで、探す・読み直すのに使う秒の上限（越えたら残りは読まず、一部だと返す）
SKIP = {"node_modules", "venv", "dist", "build", "target", "__pycache__", "vendor", "site-packages"}
JST = dt.timezone(dt.timedelta(hours=9))
CLIP = {"thread": 120, "what": 300, "done": 200, "copy": 120, "name": 100}

_ROW_TIME = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
# 「深夜帯: 11/17 22:54・当方」「11/14 11:33 以降・当方」。ラベルは「／」「、」で区切られた 1 つぶん
_POST = re.compile(r"(?:(?P<label>[^:：／、]{1,40})[:：]\s*)?(?P<m>\d{1,2})/(?P<d>\d{1,2})\s+(?P<H>\d{1,2}):(?P<M>\d{2})(?:\s*以降)?\s*・\s*(?P<who>[^／、|]{1,40})")
_COPY = re.compile(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})_out_(.{1,200})\.md")
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f\u061c\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\ud800-\udfff]")

_found: dict = {"at": 0.0, "root": None, "logs": {}, "partial": False}
_parsed: dict = {}  # 控えの場所 → (読んだ時刻, 行, 読み切れなかったか)


def clip(v, key: str) -> str:
    """1 行にそろえ、制御文字と向きを変える文字を空白にして、字数で切る。"""
    v = " ".join(_CTRL.sub(" ", str(v or "")).split())
    n = CLIP[key]
    return v if len(v) <= n else v[: n - 1] + "…"


def _inside(path: Path, base: Path) -> bool:
    try:
        return path.resolve().is_relative_to(base.resolve())
    except (OSError, ValueError):
        return False


def _find_logs(project: Path, state: dict) -> list[Path]:
    out = []
    for cur, dirs, files in os.walk(project, followlinks=False):
        state["dirs"] += 1
        if state["dirs"] > WALK_DIRS or time.monotonic() > state["deadline"]:
            state["partial"] = True
            break
        depth = len(Path(cur).relative_to(project).parts)
        dirs[:] = [] if depth >= MAX_DEPTH - 1 else sorted(d for d in dirs if not d.startswith(".") and d not in SKIP)
        if LOG_NAME in files:
            p = Path(cur) / LOG_NAME
            if not p.is_symlink() and p.is_file() and _inside(p, project):
                out.append(p)
                if len(out) >= MAX_LOGS:
                    break
    return out


def _plain(name: str) -> bool:
    """案件の名前が、そのまま 1 行で見せられる文字だけか（制御文字・向きを変える文字・長すぎる名前の案件は読まない）。"""
    return bool(name) and clip(name, "name") == name


def logs(root: Path | None, state: dict | None = None) -> dict[str, list[Path]]:
    """案件の名前 → 控えの場所。置き場所の直下のフォルダ（隠し・symlink・名前が 1 行の文字でないものを除く）だけを見る。
    FIND_TTL 秒は前の結果を使う。探すフォルダの数（WALK_DIRS）か時間（state の期限）を越えたら、そこまでの結果を一部として覚える。"""
    state = state if state is not None else {"deadline": time.monotonic() + BUDGET, "partial": False}
    if not root or not root.is_dir():
        return {}
    if _found["root"] == root and time.time() - _found["at"] < FIND_TTL:
        state["partial"] = state["partial"] or _found["partial"]
        return _found["logs"]
    found, walk = {}, {"dirs": 0, "deadline": state["deadline"], "partial": False}
    for d in sorted(root.iterdir()):
        if walk["partial"]:
            break
        if d.name.startswith(".") or not _plain(d.name) or d.is_symlink() or not d.is_dir():
            continue
        try:
            ps = _find_logs(d, walk)
        except OSError:
            continue
        if ps:
            found[d.name] = ps
    _found.update(at=time.time(), root=root, logs=found, partial=walk["partial"])
    state["partial"] = state["partial"] or walk["partial"]
    return found


def _stamp(y: int, m: int, d: int, H: int, M: int) -> float | None:
    try:
        return dt.datetime(y, m, d, H, M, tzinfo=JST).timestamp()
    except ValueError:
        return None


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse(text: str) -> list[dict]:
    """控えの表から、当方の投稿の行を出す。同じスレッド（題とラベル）・同じ時刻の投稿は、最初に書かれた行だけ（後の回の行は、同じ投稿を末尾として見ただけ）。"""
    out, seen = [], set()
    for line in text.split("\n"):  # splitlines は U+2028 などでも切るので使わない（行の途中で切れて、欄がずれる）
        if not line.startswith("|"):
            continue
        c = _cells(line)
        if len(c) < 5:
            continue
        m = _ROW_TIME.match(c[0])
        if not m:
            continue
        year, checked_month = int(m.group(1)), int(m.group(2))
        for p in _POST.finditer(c[2]):
            if not p.group("who").strip().startswith("当方"):
                continue
            mo = int(p.group("m"))
            at = _stamp(year - (1 if mo > checked_month else 0), mo, int(p.group("d")), int(p.group("H")), int(p.group("M")))
            if at is None:
                continue
            label, thread = clip(p.group("label"), "thread"), clip(c[1], "thread")
            key = (at, thread, label)  # 同じラベル・時刻でも、スレッドが違えば別の投稿として残す（後の回に同じ投稿を見ただけの行が重なることはある）
            if key in seen:
                continue
            seen.add(key)
            out.append({"at": at, "thread": thread, "label": label, "what": clip(c[4], "what"),
                        "done": clip(c[5], "done") if len(c) > 5 else ""})
    return out


def copies(names) -> list[dict]:
    """控えと同じフォルダの thread/ にある、出した投稿の控えのファイルの名前から（中身は読まない）。"""
    out = []
    for name in names:
        m = _COPY.fullmatch(name)
        at = _stamp(*(int(m.group(i)) for i in range(1, 6))) if m else None
        if at is not None:
            out.append({"at": at, "copy": clip(m.group(6).replace("_", " "), "copy")})
    return out


def _open_dir(root: Path, name: str, parts: tuple[str, ...]) -> int:
    """置き場所の中の案件のフォルダから、1 段ずつ symlink をたどらずにフォルダを開く（探したあとに途中を symlink に替えられても、外へ出ない）。"""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in (name, *parts):
            if part in ("", ".", "..") or "/" in part:
                raise OSError("場所の形が違います")
            nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def _local(root: Path, name: str, rel: tuple[str, ...]) -> list[tuple[str, list[str]]]:
    """作業木にある控えの中身と、thread/ のファイルの名前。読む直前に、置き場所から 1 段ずつ symlink をたどらずに開き直し、
    通常のファイルだけを上限まで読む（FIFO などで止まらないよう O_NONBLOCK）。thread/ も同じ開き方で、通常のファイルの名前だけ。"""
    try:
        d = _open_dir(root, name, rel[:-1])
    except OSError:
        return []
    try:
        try:
            f = os.open(rel[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=d)
        except OSError:
            return []
        with os.fdopen(f, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
                return []
            raw = fh.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return []
        names = []
        try:
            t = os.open("thread", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=d)
        except OSError:
            t = None
        if t is not None:
            try:
                names = [n for n in os.listdir(t) if stat.S_ISREG(os.stat(n, dir_fd=t, follow_symlinks=False).st_mode)]
            except OSError:
                names = []
            finally:
                os.close(t)
    finally:
        os.close(d)
    return [(raw.decode("utf-8", "replace"), names)]


def _git(project: Path, *args: str) -> str | None:
    """読むだけの git。fsmonitor・フック・テキスト変換を動かさず、足りないオブジェクトを取りに行かず（GIT_NO_LAZY_FETCH・protocol.allow=never）、
    親のフォルダへリポジトリを探しに上らず（GIT_CEILING_DIRECTORIES）、ロックを取らず、問い合わせない。失敗・時間切れは None。
    呼ぶのは rev-parse・for-each-ref・show（blob を --no-textconv で）・ls-tree だけ。"""
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1",
           "GIT_CEILING_DIRECTORIES": str(project.parent)}
    try:
        proc = subprocess.Popen(["git", "-C", str(project), "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                                 "-c", "protocol.allow=never", "--no-pager", *args],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    except OSError:
        return None
    timer = threading.Timer(GIT_TIMEOUT, proc.kill)  # 時間切れで止める（読み取りが詰まっても、ここで終わる）
    timer.start()
    try:
        data = proc.stdout.read(MAX_BYTES + 1)  # 受け取る量は上限 + 1 まで。越えたら、それ以上は受け取らずに止める
        if len(data) > MAX_BYTES:
            proc.kill()
        proc.stdout.close()
        code = proc.wait()
    except (OSError, ValueError):
        proc.kill()
        proc.wait()
        return None
    finally:
        timer.cancel()
    if code != 0 or len(data) > MAX_BYTES:
        return None
    return data.decode("utf-8", "replace")


def _same_dir(path: Path, st: os.stat_result) -> bool:
    try:
        now = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(now.st_mode) and (now.st_dev, now.st_ino) == (st.st_dev, st.st_ino)


def _branches(root: Path, name: str, rel: tuple[str, ...], state: dict) -> list[tuple[str, list[str]]]:
    """手元の枝（マージ前の控えの行は、セッションが作った枝にだけあることが多い）の、控えの中身と thread/ の名前。
    最後のコミットが GIT_DAYS 日以内の枝を、新しい順に GIT_REFS 本まで。案件のフォルダは、置き場所から symlink をたどらずに開いた
    フォルダと同じもの（同じ装置・inode）であることを、git を呼ぶ前と呼び終えたあとに確かめ、途中で入れ替わっていれば枝の結果を捨てる。
    git は案件のフォルダがリポジトリの根のときだけ使う（親のリポジトリの枝・symlink の先のリポジトリを読まない）。時間の上限を越えたら残りは読まない。"""
    try:
        fd = _open_dir(root, name, ())
    except OSError:
        return []
    try:
        st = os.fstat(fd)
    finally:
        os.close(fd)
    project = root.resolve() / name
    if not _same_dir(project, st):
        return []
    rel_s = "./" + "/".join(rel)
    top = _git(project, "rev-parse", "--show-toplevel")
    if not top or Path(top.strip()) != project:
        return []
    refs = _git(project, "for-each-ref", "--sort=-committerdate", "--format=%(objectname) %(committerdate:unix)", "refs/heads", "refs/remotes")
    out, shas = [], []
    for line in (refs or "").splitlines():
        sha, _, when = line.partition(" ")
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha) or not when.isdigit() or time.time() - int(when) > GIT_DAYS * 86400:
            continue
        if sha not in shas:
            shas.append(sha)
        if len(shas) >= GIT_REFS:
            break
    for sha in shas:
        if time.monotonic() > state["deadline"]:
            state["partial"] = True
            break
        text = _git(project, "show", "--no-textconv", f"{sha}:{rel_s}")
        if text is None:
            continue
        tree = _git(project, "ls-tree", "-z", "--name-only", sha, "--", rel_s.rsplit("/", 1)[0] + "/thread/") or ""  # -z: 日本語の名前を引用符で包ませない
        out.append((text, [n.rsplit("/", 1)[-1] for n in tree.split("\0") if n]))
    if not _same_dir(project, st):  # 呼んでいる間に案件のフォルダが入れ替わった。どのリポジトリを読んだか分からないので捨てる
        return []
    return out


def _merge(sources: list[tuple[str, list[str]]]) -> list[dict]:
    """控えの行と、thread/ の控えのファイルの名前を合わせる。ファイルは、どの行の投稿かを確かめる手がかりが無いので、
    時刻が同じでも行に結び付けず、ファイルだけの行として返す（取り違えて別の投稿の根拠にしないため）。"""
    rows, seen = [], set()
    for text, names in sources:
        for r in parse(text):
            key = (r["at"], r["thread"], r["label"])
            if key not in seen:
                seen.add(key)
                rows.append(r)
    done = set()
    for text, names in sources:
        for cp in copies(names):
            if (cp["at"], cp["copy"]) not in done:
                done.add((cp["at"], cp["copy"]))
                rows.append({"at": cp["at"], "thread": "", "label": "", "what": "", "done": "", "copy": cp["copy"]})
    return rows


def _read(root: Path, name: str, path: Path, state: dict) -> list[dict]:
    """作業木と手元の枝の控えを合わせた、当方の投稿の行。READ_TTL 秒は前の結果を使う。時間の上限を越えていたら読み直さず、
    前の結果（無ければ空）を使って一部と印を付ける。"""
    hit = _parsed.get(path)
    if hit and time.time() - hit[0] < READ_TTL:
        state["partial"] = state["partial"] or hit[2]
        return hit[1]
    if time.monotonic() > state["deadline"]:
        state["partial"] = True
        return hit[1] if hit else []
    project = root / name
    try:
        rel = path.relative_to(project).parts
    except ValueError:
        return []
    if project.is_symlink():
        return []
    sub = {"deadline": state["deadline"], "partial": False}  # この控えを読み切れたか（読み切れなかった結果は、一部の印と一緒に覚える）
    rows = _merge(_local(root, name, rel) + _branches(root, name, rel, sub))
    _parsed[path] = (time.time(), rows, sub["partial"])
    state["partial"] = state["partial"] or sub["partial"]
    return rows


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def sent(root: Path | None, project: str = "", since: float | None = None, limit: int = 30) -> dict:
    """当方の投稿を新しい順に。project は控えのある案件の名前と一字一句同じときだけ絞る（場所には使わない）。
    探す・読み直すのは BUDGET 秒まで。越えて読めなかった案件・枝があれば partial を真にする。"""
    state = {"deadline": time.monotonic() + BUDGET, "partial": False}
    found = logs(root, state)
    if project and project not in found:  # 探しきれなかったときは、控えが無いとは言わない
        return {"error": "incomplete" if state["partial"] else "no_project", "projects": sorted(found), "partial": state["partial"]}
    rows, seen = [], set()
    # 同じ控えの写し（別のフォルダに置いた古い clone など）の同じ行は、新しく書かれた控えのほうの 1 行だけにする
    order = sorted(((name, p) for name, ps in found.items() if not project or name == project for p in ps),
                   key=lambda np: -_mtime(np[1]))
    for name, p in order:
        for r in _read(root, name, p, state):
            key = (r["at"], r["thread"], r["label"], r["what"]) if (r["thread"] or r["what"]) else (r["at"], "", "", "", r.get("copy", ""))
            if key in seen or (since is not None and r["at"] < since):
                continue
            seen.add(key)
            rows.append({**r, "project": name, "at_text": dt.datetime.fromtimestamp(r["at"], JST).strftime("%m/%d %H:%M")})
    rows.sort(key=lambda r: -r["at"])
    n = max(1, min(int(limit), 200))
    return {"rows": rows[:n], "more": max(0, len(rows) - n), "projects": sorted(found), "partial": state["partial"]}
