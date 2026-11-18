"""Hermes の学び（手順の書き足しと記憶）を自動で当て、変わった跡を残し、画面の「戻す」で戻すための部品（2026-11-16 本人が推奨を選んだ）。

これまでは、手順の書き足しを画面の 💡 で本人が採っていた（承認）。本人の決定（2026-11-07・11-10・11-16）は「学びは自動で当てる。承認待ちを作らない。
見たいときに見る画面と『戻す』だけ」。そこで、本体が 1 日 LIMIT 件まで自動で当て、当てる前の中身を控えて、画面の「学んだこと」から戻せるようにする。

守り（README「守り」）。相手は外の文面に乗せられた Hermes で、置き場（ホストの ~/.hermes = コンテナの /opt/data）を同じ利用者で書ける:
  - 当てる処理は hermes_skills の台本のまま（Hermes 自身の apply_skill_pending）。自動で当てるのは 1 日 LIMIT 件まで。超えた分は承認待ちに残し、次の日に回す
  - 当てる前と後の手順のフォルダの中身は、コンテナの中の固定の台本（SCRIPT）で読む。前の中身と、後の中身の指紋は、本体の側（Hermes が書けない状態フォルダ）に控える
  - 戻すのは画面の「戻す」（2 段）だけ。戻す台本は、いまの中身の指紋が当てた直後と同じときだけ書き戻す（あとで変わっていれば何もしない）。
    symlink・通常でないファイル・hardlink・「..」の場所はたどらず、フォルダを作る・ファイルを書く・消す・空のフォルダを消す直前に、手順のフォルダが
    戻し始めたときと同じ場所にあるかを確かめる。ただし台本は Hermes と同じ利用者で動くので、同じ利用者が確かめと書き込みの間にフォルダを移す競り合いは
    防ぎ切れない（届くのは Hermes がもともと書ける場所だけ。本体のホスト側の処理は置き場に書かない。コンテナからの書き込みはホストの ~/.hermes に反映される）
  - 記憶は Hermes が直接書く。本体は 1 分おきに読み（hermes_memory の読み方。symlink をたどらない）、足された・消えた項目を跡に残す。戻すは記憶の台本で
"""
import base64
import hashlib
import inspect
import json
import os
import stat
import subprocess

import hermes_skills
from hermes_skills import CHECK_MARK, HOST_WAIT, MARK, PY, RUN_LIMIT, USER, WORKDIR, docker_bin

LIMIT = 5  # 1 日（日本時間）に自動で当てる手順の書き足しの数
SNAP_FILE_MAX = 256 * 1024  # 手順のフォルダの中の 1 ファイルの上限
SNAP_TOTAL_MAX = 1024 * 1024  # 1 つの手順のフォルダの合計の上限
SNAP_FILES_MAX = 200
SNAP_BATCH_SKILLS_MAX = 20  # 1 つの書き足しが触る手順の数の上限（超えたら自動では当てない）
SNAP_BATCH_TOTAL_MAX = 4 * 1024 * 1024  # 1 つの書き足しが触る手順の、控えの合計の上限（base64 の字数。超えたら自動では当てない）


def _parts(rel: str) -> list[str]:
    """置き場の根からの場所を、段ごとに分ける。空・「.」「..」・絶対の場所・バックスラッシュは断る（ValueError）。"""
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel or "\x00" in rel:
        raise ValueError("場所の形が違います")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError("場所の形が違います")
    return parts


def _open_parent(root: str, parts: list[str], make: bool = False) -> int | None:
    """根から parts の手前の段までを、symlink をたどらずに開いた fd。make なら無い段を作る。途中が無ければ None。"""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for p in parts[:-1]:
            try:
                nfd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not make:
                    os.close(fd)
                    return None
                os.mkdir(p, 0o755, dir_fd=fd)
                nfd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _ident(root: str, parts: list[str]) -> tuple | None:
    """根から parts を symlink をたどらずに開いたときの、各段のフォルダの (dev, inode) の並び。途中が無ければ None。"""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    out = []
    try:
        for p in parts:
            try:
                nfd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                return None
            os.close(fd)
            fd = nfd
            st = os.fstat(fd)
            out.append((st.st_dev, st.st_ino))
        return tuple(out)
    finally:
        os.close(fd)


def _same(root: str, base: list[str], held, dfd: int, parts: list[str]):
    """書く・消す直前に、根から手順のフォルダまでが、戻し始めたときと同じフォルダか（held）、書くフォルダ dfd が根からたどったものと同じかを確かめる。
    違えば ValueError（置き場の外へ移されたフォルダに書かない）。"""
    if held is not None and _ident(root, base) != held:
        raise ValueError("戻す途中で手順のフォルダが移されました")
    st, now = os.fstat(dfd), _ident(root, parts[:-1])
    if not now or now[-1] != (st.st_dev, st.st_ino):
        raise ValueError("戻す途中で手順のフォルダが移されました")


def snap_dir(root: str, rel: str) -> dict:
    """置き場の根 root の下の rel（手順のフォルダ）の中身: {"exists": bool, "files": {フォルダからの場所: base64}, "modes": {場所: 権限の 9 ビット}}。
    symlink・通常でないもの・大きすぎるものがあれば ValueError（読めない手順は自動で当てない・戻さない）。"""
    parts = _parts(rel)
    parent = _open_parent(root, parts)
    if parent is None:
        return {"exists": False, "files": {}, "modes": {}}
    try:
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        except FileNotFoundError:
            return {"exists": False, "files": {}, "modes": {}}
    finally:
        os.close(parent)
    files, modes, total = {}, {}, [0]

    def walk(dfd: int, prefix: str):
        for name in sorted(os.listdir(dfd)):
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                sub = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dfd)
                try:
                    walk(sub, prefix + name + "/")
                finally:
                    os.close(sub)
            elif stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
                if st.st_size > SNAP_FILE_MAX or len(files) >= SNAP_FILES_MAX:
                    raise ValueError("手順のフォルダが大きすぎます")
                ffd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
                with os.fdopen(ffd, "rb") as f:
                    fst = os.fstat(ffd)  # 調べてから開くまでに差し替えられていないか（hardlink・FIFO・別のファイル）
                    if not stat.S_ISREG(fst.st_mode) or fst.st_nlink != 1 or (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
                        raise ValueError("手順のフォルダのファイルが読む途中で差し替えられました")
                    data = f.read(SNAP_FILE_MAX + 1)
                total[0] += len(data)
                if len(data) > SNAP_FILE_MAX or total[0] > SNAP_TOTAL_MAX:
                    raise ValueError("手順のフォルダが大きすぎます")
                files[prefix + name] = base64.b64encode(data).decode()
                modes[prefix + name] = stat.S_IMODE(fst.st_mode) & 0o777  # 実行できる台本の x も控える（戻すときに同じ権限で書く）
            else:
                raise ValueError("手順のフォルダに symlink か通常でないファイルがあります")

    try:
        walk(fd, "")
    finally:
        os.close(fd)
    return {"exists": True, "files": files, "modes": modes}


def digest(snap: dict) -> str:
    """中身の指紋（あるか・ファイルの場所と中身・権限）。"""
    return hashlib.sha256(json.dumps([bool(snap.get("exists")), sorted((snap.get("files") or {}).items()),
                                      sorted((snap.get("modes") or {}).items())]).encode()).hexdigest()


def restore_dir(root: str, rel: str, before: dict, expect: str) -> str:
    """rel の中身を before に戻す。いまの中身の指紋が expect（当てた直後）と違えば、何も書かずに "changed"。戻したら "restored"。
    before のファイルの場所も、根からの場所と同じ決まりで確かめる（「..」・絶対の場所は断る）。"""
    now = snap_dir(root, rel)
    if digest(now) != expect:
        return "changed"
    want = {k: base64.b64decode(v, validate=True) for k, v in (before.get("files") or {}).items()} if before.get("exists") else {}
    modes = before.get("modes") or {}
    for k in want:
        _parts(k)
        if not isinstance(modes.get(k, 0o644), int) or isinstance(modes.get(k, 0o644), bool):
            raise ValueError("控えの権限の形が違います")
    base = _parts(rel)
    held = _ident(root, base)  # 戻し始めるときの手順のフォルダ（と途中のフォルダ）。書くたびに、根から同じものをたどれるかを確かめる
    for k in sorted(set(now["files"]) - set(want), key=lambda p: -p.count("/")):  # 当てたときに増えたファイルを消す
        parts = base + _parts(k)
        dfd = _open_parent(root, parts)
        if dfd is not None:
            try:
                _same(root, base, held, dfd, parts)
                os.unlink(parts[-1], dir_fd=dfd)
            finally:
                os.close(dfd)
    for k, data in want.items():  # 前の中身を書く（いったん隣に書いてから置き換える）
        parts = base + _parts(k)
        if held is not None and _ident(root, base) != held:  # フォルダを作る前にも確かめる
            raise ValueError("戻す途中で手順のフォルダが移されました")
        dfd = _open_parent(root, parts, make=True)
        try:
            _same(root, base, held if held else _ident(root, base), dfd, parts)
            tmp = f".vl-restore-{os.urandom(6).hex()}"  # 元の名前を入れない固定の長さ（長い名前でも、名前の長さの上限を超えない）
            mode = modes.get(k, 0o644) & 0o777  # 控えた権限（setuid などの上の位は落とす）。控えに無ければ 0644
            tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            with os.fdopen(tfd, "wb") as f:
                f.write(data)
                os.fchmod(tfd, mode)  # umask に左右されずに、控えた権限にする
            os.replace(tmp, parts[-1], src_dir_fd=dfd, dst_dir_fd=dfd)
        finally:
            os.close(dfd)
    if held and _ident(root, base) != held:  # 書き終えたあとも、手順のフォルダが置き場の同じ場所にあるか（移されていれば戻せたとは言わない）
        raise ValueError("戻す途中で手順のフォルダが移されました")
    if not before.get("exists"):  # 当てたときに作られた手順: 空になったフォルダを、深いほうから消す
        dirs = sorted({"/".join(_parts(k)[:i]) for k in now["files"] for i in range(1, len(_parts(k)))}, key=lambda p: -p.count("/"))
        for d in dirs + [""]:
            parts = base + (_parts(d) if d else [])
            dfd = _open_parent(root, parts)
            if dfd is not None:
                try:
                    if d:  # 中のフォルダ: 手順のフォルダが同じ場所にあるか
                        _same(root, base, held, dfd, parts)
                    else:  # 手順のフォルダそのもの: 手順のフォルダが同じ場所にあり、消す親（dfd）が根からたどった親と同じか
                        if held is not None and _ident(root, base) != held:
                            raise ValueError("戻す途中で手順のフォルダが移されました")
                        st = os.fstat(dfd)
                        up = _ident(root, base[:-1]) if base[:-1] else ((os.stat(root, follow_symlinks=False).st_dev, os.stat(root, follow_symlinks=False).st_ino),)
                        if not up or up[-1] != (st.st_dev, st.st_ino):
                            raise ValueError("戻す途中で手順のフォルダが移されました")
                    os.rmdir(parts[-1], dir_fd=dfd)
                except OSError:
                    pass  # 空でない（Hermes がほかのファイルを置いた）なら残す
                finally:
                    os.close(dfd)
    return "restored"


def front_name(sm, snap: dict) -> str | None:
    """手順の別名（SKILL.md の frontmatter の name）を、控えとして読んだのと同じバイト列から、Hermes 自身の読み方
    （_read_frontmatter_name と同じ: BOM を外し、先頭 4000 字を parse_frontmatter で読み、name の文字列を名前の上限で切る）で読む。
    SKILL.md が無ければ None。読み方の無い版の Hermes・読めないときは例外（別名を確かめられないので、扱わない）。"""
    if not callable(getattr(sm, "_read_frontmatter_name", None)):
        raise ValueError("この版の Hermes では手順の別名を確かめられないので、自動では当てません")
    raw = (snap.get("files") or {}).get("SKILL.md")
    if raw is None:
        return None
    from agent.skill_utils import parse_frontmatter
    fm, _ = parse_frontmatter(base64.b64decode(raw).decode("utf-8-sig", "replace")[:4000])
    name = fm.get("name") if isinstance(fm, dict) else None
    return name[:getattr(sm, "MAX_NAME_LENGTH", 64)] if isinstance(name, str) else None


def snap_payload(sm, payload: dict) -> dict:
    """書き足し（payload）が触る手順のフォルダの中身を、置き場の根からの場所ごとに読む（コンテナの中で、Hermes の部品 sm を使う）。
    置き場の外の手順・書き足し全体で大きすぎる・手順の鍵が無い版の Hermes・名前とフォルダの名前が違う手順は ValueError（自動では当てない）。"""
    if not callable(getattr(sm, "_skill_mutation_locks", None)):
        raise ValueError("この版の Hermes には手順の鍵が無いので、自動では当てません（画面の「採る」は使えます）")
    root = str(sm._skills_dir())
    real_root = os.path.realpath(root)
    ops = payload.get("operations") if payload.get("action") == "batch" else [payload]
    out, planned = {}, {}
    for op in ops:
        found = sm._find_skill(op["name"])
        # まだ無い手順は、同じ書き足しの中で先に出てきた作る先にそろえる（作ったあとの操作は、作られた手順に当たるため）
        p = found["path"] if found else planned.get(op["name"]) or sm._resolve_skill_dir(op["name"], op.get("category"))
        if not found:
            planned.setdefault(op["name"], p)
        # Hermes の手順の鍵は名前の末尾で決まる。別名（frontmatter の名前）で見つかる手順は、鍵がフォルダの名前とずれるので扱わない
        if os.path.basename(str(p).rstrip("/")) != op["name"].rsplit("/", 1)[-1]:
            raise ValueError("手順の名前とフォルダの名前が違うので、自動では当てません")
        real = os.path.realpath(str(p))
        if real == real_root or not real.startswith(real_root + os.sep):
            raise ValueError("手順の置き場の外の手順です")
        rel = os.path.relpath(real, real_root)
        if rel not in out:
            out[rel] = snap_dir(root, rel)
            alias = front_name(sm, out[rel])  # 別名（frontmatter の名前）がフォルダの名前と違えば、Hermes が別名で書くときの鍵とずれる
            if alias is not None and alias != os.path.basename(rel):
                raise ValueError("別名（frontmatter の名前）がフォルダの名前と違う手順なので、自動では当てません")
            size = sum(len(v) for snap in out.values() for v in snap["files"].values())
            if len(out) > SNAP_BATCH_SKILLS_MAX or size > SNAP_BATCH_TOTAL_MAX:  # 書き足し全体でも大きすぎる控えは作らない（自動では当てない）
                raise ValueError("書き足しが触る手順が多すぎるか、大きすぎます")
    return out


def learn_hold(payload: dict):
    """自動で当てる台本（APPLY_SCRIPT）の、当てる直前の確かめ。Hermes が手順を書き換えるときと同じ鍵（手順ごとのファイルの鍵）を取り、
    台本が終わるまで持つ。いまの中身の指紋が、控えを取ったときの指紋（環境変数 VL_LEARN_EXPECT）と違えば、当てずに changed で終わる
    （控えを取ったあとに Hermes が書いた中身を、戻すで消さないため）。当てたあとの指紋を返す関数を返す（読めなければ None を返す）。"""
    from tools import skill_manager_tool as sm
    ops = payload.get("operations") if payload.get("action") == "batch" else [payload]
    locks = getattr(sm, "_skill_mutation_locks", None)
    if not callable(locks):  # 鍵が無い版の Hermes では、自動では当てない（控えのあとの書き込みを混ぜない保証が無いため）
        done(result="unsupported", why="この版の Hermes には手順の鍵が無いので、自動では当てません")
    globals()["_LEARN_LOCKS"] = locks([op["name"] for op in ops])  # 持ち続ける（台本の終わりで離れる）
    try:
        now = {k: digest(v) for k, v in snap_payload(sm, payload).items()}
    except Exception as e:
        done(result="changed", why="当てる前に手順を読めませんでした（" + type(e).__name__ + "）")
    if now != json.loads(os.environ.get("VL_LEARN_EXPECT") or "{}"):
        done(result="changed", why="控えを取ったあとに手順が変わりました")

    def after():  # 当てて別名ができた（読めない）ときは None（戻せない）
        try:
            return {k: digest(v) for k, v in snap_payload(sm, payload).items()}
        except Exception:
            return None
    return after


# ---------- コンテナの中の台本 ----------
# 引数は「目印 種類」。要求の JSON を stdin で受け取り、結果を「VLRESULT {JSON}」の 1 行で返す（hermes_skills と同じ読み方）。
#   snap    … 承認待ちの記録の payload が触る手順のフォルダの中身（置き場の根からの場所ごと）。置き場の外の手順は unsupported
#   restore … {"skills": {場所: {"before": 中身, "expect": 指紋}}}。全部の指紋が合うときだけ、前の中身に戻す（合わなければ changed で何もしない）
_SCRIPT_MAIN = r'''
import json, os, sys
from pathlib import Path


def done(**kw):
    sys.stdout.write("\nVLRESULT " + json.dumps(kw, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    os._exit(0)


mode = sys.argv[2]
req = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
from tools import skill_manager_tool as sm
root = str(sm._skills_dir())
real_root = os.path.realpath(root)
if mode == "snap":
    try:
        out = snap_payload(sm, req.get("payload") or {})
    except Exception as e:  # 読めない形（symlink・大きすぎる）・置き場の外・Hermes の探し方の失敗。自動では当てない
        done(result="unsupported", why=(type(e).__name__ + ": " + str(e))[:200])
    done(result="snap", skills=out)
if mode == "restore":
    skills = req.get("skills") or {}
    # Hermes が手順を書き換えるときと同じ鍵（手順ごとのファイルの鍵）を、指紋を照らしてから戻し終えるまで持つ
    # （照らしたあとに Hermes が書いた中身を、戻すで上書きしない）。この鍵の無い版の Hermes では、鍵なしで戻す
    from contextlib import nullcontext
    locks = getattr(sm, "_skill_mutation_locks", None)
    # フォルダの名前と、いまの別名（frontmatter の名前）の両方の鍵を取る（Hermes がどちらの名前で書いても待たせる）
    names = set()
    for rel in skills:
        names.add(Path(rel).name)
        if not callable(getattr(sm, "_read_frontmatter_name", None)):
            continue  # 別名を読む関数の無い版の Hermes（別名で探すこともない）では、フォルダの名前の鍵だけ
        try:
            alias = front_name(sm, snap_dir(root, rel))
        except Exception as e:  # 関数があるのに読めない: 鍵を決められないので戻さない
            done(result="failed", why="手順の別名を読めませんでした（" + type(e).__name__ + "）")
        if alias:
            names.add(alias)
    held = locks(sorted(names)) if callable(locks) else nullcontext()
    try:
        with held:
            for rel, it in skills.items():
                if digest(snap_dir(root, rel)) != it["expect"]:
                    done(result="changed", why="当てたあとに手順が変わっています")
            for rel, it in skills.items():
                if restore_dir(root, rel, it["before"], it["expect"]) != "restored":
                    done(result="changed", why="戻す途中で手順が変わりました")
    except (ValueError, OSError) as e:
        done(result="failed", why=type(e).__name__ + ": " + str(e)[:200])
    done(result="restored")
done(result="error", why="知らない種類です")
'''
SCRIPT = ("import base64, hashlib, json, os, stat\n"
          + f"SNAP_FILE_MAX = {SNAP_FILE_MAX}\nSNAP_TOTAL_MAX = {SNAP_TOTAL_MAX}\nSNAP_FILES_MAX = {SNAP_FILES_MAX}\n"
          + f"SNAP_BATCH_SKILLS_MAX = {SNAP_BATCH_SKILLS_MAX}\nSNAP_BATCH_TOTAL_MAX = {SNAP_BATCH_TOTAL_MAX}\n"
          + "".join(inspect.getsource(f) + "\n" for f in (_parts, _open_parent, _ident, _same, snap_dir, digest, restore_dir, front_name, snap_payload)) + _SCRIPT_MAIN)
# 自動で当てる台本: hermes_skills の台本（approve）の前に、控えの確かめ（learn_hold）を足したもの。目印は hermes_skills と同じ
APPLY_SCRIPT = ("import base64, hashlib, json, os, stat\n"
                + f"SNAP_FILE_MAX = {SNAP_FILE_MAX}\nSNAP_TOTAL_MAX = {SNAP_TOTAL_MAX}\nSNAP_FILES_MAX = {SNAP_FILES_MAX}\n"
                + f"SNAP_BATCH_SKILLS_MAX = {SNAP_BATCH_SKILLS_MAX}\nSNAP_BATCH_TOTAL_MAX = {SNAP_BATCH_TOTAL_MAX}\n"
                + "".join(inspect.getsource(f) + "\n" for f in (_parts, _open_parent, _ident, _same, snap_dir, digest, front_name, snap_payload, learn_hold))
                + "LEARN_HOLD = learn_hold\n" + hermes_skills.SCRIPT)


def command(container: str, mode: str, env: dict | None = None) -> list[str] | None:
    """台本を動かす命令。戻す（書く）は当てる・捨てると同じ目印（終わったかの確かめに数える）、読むは確かめの目印。"""
    exe = docker_bin()
    if not exe:
        return None
    extra = [a for k, v in (env or {}).items() for a in ("-e", f"{k}={v}")]
    return [exe, "exec", "-i", "-u", USER, "-w", WORKDIR, *extra, container,
            "timeout", "--kill-after=5", str(RUN_LIMIT), PY, "-c", SCRIPT, MARK if mode == "restore" else CHECK_MARK, mode]


def run_script(container: str, mode: str, req: dict, wait: float = HOST_WAIT, env: dict | None = None) -> dict:
    """台本を動かして結果を返す（{"result": …}）。読み方は hermes_skills.run_script と同じ。"""
    cmd = command(container, mode, env)
    if not cmd:
        return {"result": "error", "why": "docker が見つかりません（config.env の VL_DOCKER に場所を書けます）"}
    try:
        p = subprocess.run(cmd, input=json.dumps(req, ensure_ascii=False).encode(), capture_output=True, timeout=wait)
    except subprocess.TimeoutExpired:
        return {"result": "timeout", "why": f"{wait:.0f} 秒待っても終わりませんでした"}
    except OSError as e:
        return {"result": "error", "why": f"docker を動かせません（{type(e).__name__}）"}
    return hermes_skills.read_result(p)
