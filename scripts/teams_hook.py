"""Teams の見張り（teams_watch.py が動かす claude -p --chrome）のフック。

  teams_hook.py guard     PreToolUse。決めた道具と決めた入力だけを通す（ほかは終わりの数 2 で止める）
  teams_hook.py capture   PostToolUse。javascript_tool の結果を、決めたファイルに書き足す（LLM に書き写させない）

guard が通すもの（ほかは全部止める）:
- ToolSearch（道具の説明を読み込むだけ。使うときは、その道具がもう一度ここを通る）
- Chrome の tabs_context_mcp・tabs_close_mcp
- Chrome の navigate: 環境変数 VL_TEAMS_URL と完全に同じ URL だけ（その回に読む組織の 1 つ）
- Chrome の computer: action が wait で、入力が action・duration・tabId だけのとき（クリック・文字の入力・キー・スクロールは止める）
- Chrome の javascript_tool: action が javascript_exec で、入力が action・text・tabId だけのとき。text は LLM が渡したものを使わず、
  環境変数 VL_TEAMS_JS の決まったスクリプト（一覧を一度に写し、その写しを番号の順に切り出して返すだけ）に差し替える（updatedInput）。
  番号は、このフックが VL_TEAMS_COUNTER のファイルで数える（0 から。LLM には決めさせない）。MAX_CHUNKS 回を超えたら止める
読んだページには相手（先方）が書いた文が入る。その中の指示で LLM が動こうとしても、外へ書く・押す・ほかのページを開く・
ほかのスクリプトを動かす道は通らない。teams_watch.py はこの命令を sh で包み、終わりの数が 0 でなければ 2 に直して渡す
（フック自体が動かなくても通さない）。
"""
import fcntl
import json
import os
import sys

P = "mcp__claude-in-chrome__"
PLAIN = {"ToolSearch", P + "tabs_context_mcp", P + "tabs_close_mcp"}
MAX_CHUNKS = 60


def next_chunk(path: str) -> int:
    """この回の何番目の切り出しか（0 から）。ファイルで数える（鍵をかけ、同時の呼び出しでも番号を重ねない）。"""
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        k = int(f.read().strip() or "0")
        f.seek(0)
        f.truncate()
        f.write(str(k + 1))
        f.flush()
    return k


def decide(name, inp, url: str, js: str, counter: str = ""):
    """通すなら (True, 差し替えた入力または None)。通さないなら (False, None)。"""
    if not isinstance(name, str) or not isinstance(inp, dict):
        return False, None
    if name in PLAIN:
        return True, None
    if name == P + "navigate":
        return bool(url) and inp.get("url") == url, None
    if name == P + "computer":
        return inp.get("action") == "wait" and set(inp) <= {"action", "duration", "tabId"}, None
    if name == P + "javascript_tool":
        if js and counter and "__K__" in js and inp.get("action") == "javascript_exec" and set(inp) <= {"action", "text", "tabId"}:
            k = next_chunk(counter)
            if k < MAX_CHUNKS:
                return True, {**inp, "text": js.replace("__K__", str(k))}
    return False, None


def guard() -> int:
    try:
        d = json.load(sys.stdin)
        name, inp = d.get("tool_name"), d.get("tool_input") or {}
        ok, updated = decide(name, inp, os.environ.get("VL_TEAMS_URL", ""), os.environ.get("VL_TEAMS_JS", ""), os.environ.get("VL_TEAMS_COUNTER", ""))
        if ok:
            if updated is not None:
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                                         "permissionDecisionReason": "Teams の一覧を写して切り出す決まったスクリプトに差し替えた",
                                                         "updatedInput": updated}}))
            return 0
        print(f"Teams の見張りは {name} をこの形では使えません（一覧を読むだけに限っています）", file=sys.stderr)
    except Exception as e:  # noqa: BLE001  読めない入力・想定外の形は、通さない
        print(f"Teams の見張りの関門が呼び出しを確かめられませんでした（{type(e).__name__}）", file=sys.stderr)
    return 2


def capture() -> int:
    try:
        d = json.load(sys.stdin)
        if d.get("tool_name") != P + "javascript_tool":
            return 0
        r = d.get("tool_response")
        if isinstance(r, str):
            text = r
        elif isinstance(r, list):
            text = "\n".join(b.get("text", "") for b in r if isinstance(b, dict) and b.get("type") == "text")
        elif isinstance(r, dict):
            text = "\n".join(b.get("text", "") for b in r.get("content", []) if isinstance(b, dict) and b.get("type") == "text")
        else:
            text = ""
        out = os.environ["VL_TEAMS_OUT_JS"]
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)  # 切り出しを順に書き足す
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:  # noqa: BLE001  書けなければ、読む側で「取得に失敗」になる
        pass
    return 0


if __name__ == "__main__":
    sys.exit({"guard": guard, "capture": capture}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: 2)())
