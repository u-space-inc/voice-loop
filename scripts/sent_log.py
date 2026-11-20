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
MAX_COPIES = 5000        # 1 つの thread/ で見るファイルの数
FIND_TTL = 300           # 控えの場所を探し直すまでの秒
READ_TTL = 60            # 控えを読み直すまでの秒
GIT_DAYS = 14            # 手元の枝のうち、最後のコミットがこの日数以内のものの控えも読む
GIT_REFS = 60            # 1 つの案件で見る枝の数（枝ごとには控えの階層の一覧を 1 回だけ取り、同じ版の控え・thread/ は 1 度だけ読む）
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


def _full(v) -> str:
    """見分けに使う値（1 行にそろえ、制御文字と向きを変える文字を空白にするが、字数では切らない）。"""
    return " ".join(_CTRL.sub(" ", str(v or "")).split())


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
        if state["dirs"] > WALK_DIRS or time.monotonic() > state["deadline"]:  # 全体の上限。ここで探すのをやめる
            state["partial"] = state["stop"] = True
            break
        depth = len(Path(cur).relative_to(project).parts)
        dirs[:] = [] if depth >= MAX_DEPTH - 1 else sorted(d for d in dirs if not d.startswith(".") and d not in SKIP)
        if LOG_NAME in files:
            p = Path(cur) / LOG_NAME
            if not p.is_symlink() and p.is_file() and _inside(p, project):
                if len(out) >= MAX_LOGS:  # 上限を越える控えがある。読み残すので一部の印
                    state["partial"] = True
                    break
                out.append(p)
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
    # partial は「どこかを読み残した」（案件ごとの上限・git の失敗も）、stop は「全体の数・時間の上限に達した」で、stop だけが後の案件を探すのをやめる
    found, walk = {}, {"dirs": 0, "deadline": state["deadline"], "partial": False, "stop": False}
    for d in sorted(root.iterdir()):
        if walk["stop"]:
            break
        if d.name.startswith(".") or not _plain(d.name) or d.is_symlink() or not d.is_dir():
            continue
        try:
            ps = _find_logs(d, walk)
        except OSError:
            continue
        known = {p.relative_to(d).as_posix() for p in ps}
        for rel in _branch_logs(d, walk):  # 作業木に無く、最近の枝にだけある控え（その案件で初めての控えが、マージ前の枝にだけある）
            if rel in known:
                continue
            if len(ps) >= MAX_LOGS:  # 上限を越える控えがある。読み残すので一部の印
                walk["partial"] = True
                break
            known.add(rel)
            ps.append(d / rel)
        if ps:
            found[d.name] = ps
    _found.update(at=time.time(), root=root, logs=found, partial=walk["partial"])
    state["partial"] = state["partial"] or walk["partial"]
    return found


def _branch_rel(rel: str) -> bool:
    """git が返した控えの場所（案件のフォルダからの相対）が、作業木で探すときと同じ範囲か。"""
    parts = rel.split("/")
    return (len(parts) <= MAX_DEPTH and parts[-1] == LOG_NAME
            and all(x and x not in (".", "..") and not x.startswith(".") and x not in SKIP and _plain(x) for x in parts))


def _branch_logs(project: Path, state: dict) -> list[str]:
    """最近（GIT_DAYS 日）のコミットで変わった teams-sync-log.md の場所を、案件のリポジトリに聞く（作業木に無い控えも見つけるため）。
    案件のフォルダに .git が無ければリポジトリではないので、何も返さない（親のリポジトリへは上らない）。.git があるのに git が
    答えない（時間切れ・量の上限・失敗）とき、呼んでいる間に期限を越えたときは、探しきれなかったとして一部の印を付ける。
    名前は NUL だけで区切られる（--format= と -z）ので、手を加えずにそのまま範囲を確かめる。場所は作業木で探すときと同じ範囲だけ。"""
    try:
        os.lstat(project / ".git")
    except OSError:
        return []
    if time.monotonic() > state["deadline"]:
        state["partial"] = state["stop"] = True
        return []
    out = _git(project, "log", "--all", f"--since={GIT_DAYS}.days.ago", "--no-show-signature", "--no-ext-diff", "--no-textconv",
               "--name-only", "-z", "--format=", "--", ":(glob)**/" + LOG_NAME)
    if time.monotonic() > state["deadline"]:
        state["partial"] = state["stop"] = True
    if out is None:  # この案件の git が答えない。この案件は読み残すが、ほかの案件は探し続ける
        state["partial"] = True
        return []
    rels = []
    for rel in out.split("\0"):
        if rel and rel not in rels and _branch_rel(rel):
            if len(rels) >= MAX_LOGS:  # 上限を越える候補がある。読み残すので一部の印
                state["partial"] = True
                break
            rels.append(rel)
    return rels


def _stamp(y: int, m: int, d: int, H: int, M: int) -> float | None:
    try:
        return dt.datetime(y, m, d, H, M, tzinfo=JST).timestamp()
    except ValueError:
        return None


_PIPE = re.compile(r"(?<!\\)\|")


def _cells(line: str) -> list[str]:
    """表の行を欄に分ける。エスケープした縦棒（\\|）は欄の区切りにせず、欄の中の | に戻す。"""
    body = line.strip()
    body = body[1:] if body.startswith("|") else body
    body = body[:-1] if body.endswith("|") and not body.endswith("\\|") else body
    return [c.strip().replace("\\|", "|") for c in _PIPE.split(body)]


def parse(text: str) -> list[dict]:
    """控えの表から、当方の投稿の行を出す。同じ投稿（時刻・題・ラベル）でも、記録・対応の欄が違う行は残す（後の回に同じ投稿を末尾として見ただけの行も
    重なるが、送った回の記録を落とさないほうを選ぶ）。欄までまったく同じ行だけを 1 つにする。"""
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
            # 同じラベル・時刻でも、スレッドが違えば別の投稿として残す（後の回に同じ投稿を見ただけの行が重なることはある）。
            # 見分けは字数で切る前の値で行う（頭の部分が同じ別のスレッドを、1 つにまとめない）。返す値だけを切る
            done_full = _full(c[5]) if len(c) > 5 else ""
            key = (at, _full(c[1]), _full(p.group("label")), _full(c[4]), done_full)  # 記録・対応の欄が違えば別の行として残す（送った回の記録を落とさない）
            if key in seen:
                continue
            seen.add(key)
            out.append({"at": at, "thread": clip(c[1], "thread"), "label": clip(p.group("label"), "thread"), "what": clip(c[4], "what"),
                        "done": clip(c[5], "done") if len(c) > 5 else "", "_id": key})
    return out


def copies(names) -> list[dict]:
    """控えと同じフォルダの thread/ にある、出した投稿の控えのファイルの名前から（中身は読まない）。"""
    out = []
    for name in names:
        m = _COPY.fullmatch(name)
        at = _stamp(*(int(m.group(i)) for i in range(1, 6))) if m else None
        if at is not None:
            out.append({"at": at, "copy": clip(m.group(6).replace("_", " "), "copy"), "_id": (at, "", "", "", "", _full(m.group(6)))})
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


def _local(root: Path, name: str, rel: tuple[str, ...], state: dict) -> list[tuple[str, list[str]]]:
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
        if t is not None:  # 1 つずつ見て、数（MAX_COPIES）と時間の上限で打ち切る（打ち切ったら一部の印）
            try:
                with os.scandir(t) as it:
                    for i, e in enumerate(it):
                        if i >= MAX_COPIES or time.monotonic() > state["deadline"]:
                            state["partial"] = True
                            break
                        if e.is_file(follow_symlinks=False):
                            names.append(e.name)
            except OSError:
                state["partial"] = True
            finally:
                os.close(t)
    finally:
        os.close(d)
    return [(raw.decode("utf-8", "replace"), names)]


def _git(project: Path, *args: str) -> str | None:
    """読むだけの git。fsmonitor・フック・テキスト変換を動かさず、足りないオブジェクトを取りに行かず（GIT_NO_LAZY_FETCH・protocol.allow=never）、
    親のフォルダへリポジトリを探しに上らず（GIT_CEILING_DIRECTORIES）、ロックを取らず、問い合わせない。失敗・時間切れは None。
    呼ぶのは rev-parse・for-each-ref・show（blob を --no-textconv で）・ls-tree・log（場所の名前だけ。署名を確かめず、差分の外部の道具を使わない）だけ。"""
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1",
           "GIT_CEILING_DIRECTORIES": str(project.parent)}
    try:
        proc = subprocess.Popen(["git", "-C", str(project), "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                                 "-c", "protocol.allow=never", "-c", "log.showSignature=false", "--no-pager", *args],
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
    try:
        os.lstat(project / ".git")
    except OSError:
        return []  # リポジトリではない（枝は無い）
    top = _git(project, "rev-parse", "--show-toplevel")
    if top is None:  # .git があるのに git が答えない。枝を読めなかった
        state["partial"] = True
        return []
    if Path(top.strip()) != project:
        return []
    refs = _git(project, "for-each-ref", "--sort=-committerdate", "--format=%(objectname) %(committerdate:unix)", "refs/heads", "refs/remotes")
    if refs is None:  # 枝の一覧を読めなかった
        state["partial"] = True
        return []
    out, shas = [], []
    for line in refs.splitlines():
        sha, _, when = line.partition(" ")
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha) or not when.isdigit() or time.time() - int(when) > GIT_DAYS * 86400:
            continue
        if sha in shas:
            continue
        if len(shas) >= GIT_REFS:  # 上限を越える最近の枝がある。読み残すので一部の印
            state["partial"] = True
            break
        shas.append(sha)
    blobs, trees = {}, {}  # 控えの版（blob）・thread/ の版（tree）→ 中身。同じ版は 1 度だけ読む
    for sha in shas:
        if time.monotonic() > state["deadline"]:
            state["partial"] = True
            break
        listing = _git(project, "ls-tree", "-z", sha, "--", rel_s.rsplit("/", 1)[0] + "/")  # 控えの階層の一覧（-z: 日本語の名前を引用符で包ませない）
        if listing is None:  # 時間切れ・量の上限・失敗（空の一覧は "" で返る）。この枝は読めなかった
            state["partial"] = True
            continue
        blob = tree = None
        for entry in listing.split("\0"):
            meta, _, path = entry.partition("\t")
            f = meta.split(" ")
            if len(f) != 3 or not re.fullmatch(r"[0-9a-f]{40,64}", f[2]):
                continue
            base = path.rsplit("/", 1)[-1]
            if base == LOG_NAME and f[1] == "blob" and f[0] in ("100644", "100755"):
                blob = f[2]
            elif base == "thread" and f[1] == "tree":
                tree = f[2]
        if blob is None:
            continue
        if blob not in blobs:
            blobs[blob] = _git(project, "show", "--no-textconv", blob)
        if tree and tree not in trees:
            names = _git(project, "ls-tree", "-z", tree)  # thread/ の中の通常のファイルの名前だけ（symlink・フォルダは除く）
            if names is None:
                state["partial"] = True
            trees[tree] = [e.partition("\t")[2] for e in (names or "").split("\0")
                           if e.partition("\t")[0].split(" ")[:2] in (["100644", "blob"], ["100755", "blob"])]
            if len(trees[tree]) > MAX_COPIES:
                trees[tree] = trees[tree][:MAX_COPIES]
                state["partial"] = True
        if blobs[blob] is None:  # 控えの中身を読めなかった
            state["partial"] = True
            continue
        out.append((blobs[blob], trees.get(tree, []) if tree else []))
    if not _same_dir(project, st):  # 呼んでいる間に案件のフォルダが入れ替わった。どのリポジトリを読んだか分からないので捨てる
        return []
    return out


def _merge(sources: list[tuple[str, list[str]]]) -> list[dict]:
    """控えの行と、thread/ の控えのファイルの名前を合わせる。ファイルは、どの行の投稿かを確かめる手がかりが無いので、
    時刻が同じでも行に結び付けず、ファイルだけの行として返す（取り違えて別の投稿の根拠にしないため）。"""
    rows, seen = [], set()
    for text, names in sources:
        for r in parse(text):
            key = r["_id"]  # 時刻・スレッド・ラベルが同じでも、記録・対応の欄が違えば両方残す（作業木に後の回の行、枝に送った回の行、など）
            if key not in seen:
                seen.add(key)
                rows.append(r)
    done = set()
    for text, names in sources:
        for cp in copies(names):
            if cp["_id"] not in done:
                done.add(cp["_id"])
                rows.append({"at": cp["at"], "thread": "", "label": "", "what": "", "done": "", "copy": cp["copy"], "_id": cp["_id"]})
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
    rows = _merge(_local(root, name, rel, sub) + _branches(root, name, rel, sub))
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
            key = r["_id"]  # 切る前の値で見分ける（控えの行は 時刻・スレッド・ラベル・記録、ファイルだけの行は 時刻・名前）
            if key in seen or (since is not None and r["at"] < since):
                continue
            seen.add(key)
            rows.append({**{k: v for k, v in r.items() if not k.startswith("_")}, "project": name,
                         "at_text": dt.datetime.fromtimestamp(r["at"], JST).strftime("%m/%d %H:%M")})
    rows.sort(key=lambda r: -r["at"])
    n = max(1, min(int(limit), 200))
    return {"rows": rows[:n], "more": max(0, len(rows) - n), "projects": sorted(found), "partial": state["partial"]}
