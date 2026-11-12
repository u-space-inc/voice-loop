"""Teams の見張り（teams_watch.py が動かす claude -p --chrome）のフック。

  teams_hook.py guard     PreToolUse。決めた道具と決めた URL だけを通す（ほかは終わりの数 2 で止める）
  teams_hook.py capture   PostToolUse。get_page_text の結果を、決めたファイルに書く（LLM に書き写させない）

guard が通すもの（ほかは全部止める）:
- ToolSearch（Chrome の道具の説明を読み込むだけ）
- Chrome の tabs_context_mcp・tabs_close_mcp・get_page_text
- Chrome の navigate: 環境変数 VL_TEAMS_URL と完全に同じ URL だけ（その回に読む組織の 1 つ）
- Chrome の computer: action が wait のときだけ（クリック・文字の入力・キー・スクロールは止める）
読んだページには相手（先方）が書いた文が入る。その中の指示で LLM が動こうとしても、外へ書く・押す・ほかのページを開く道具は通らない。
teams_watch.py はこの命令を sh で包み、終わりの数が 0 でなければ 2 に直して渡す（フック自体が動かなくても通さない）。
"""
import json
import os
import sys

P = "mcp__claude-in-chrome__"
PLAIN = {"ToolSearch", P + "tabs_context_mcp", P + "tabs_close_mcp", P + "get_page_text"}


def allowed(name, inp, url: str) -> bool:
    if not isinstance(name, str) or not isinstance(inp, dict):
        return False
    if name in PLAIN:
        return True
    if name == P + "navigate":
        return bool(url) and inp.get("url") == url
    if name == P + "computer":
        return inp.get("action") == "wait" and set(inp) <= {"action", "duration", "tabId"}
    return False


def guard() -> int:
    try:
        d = json.load(sys.stdin)
        name, inp = d.get("tool_name"), d.get("tool_input") or {}
        if allowed(name, inp, os.environ.get("VL_TEAMS_URL", "")):
            return 0
        print(f"Teams の見張りは {name} をこの形では使えません（一覧を読むだけに限っています）", file=sys.stderr)
    except Exception as e:  # noqa: BLE001  読めない入力・想定外の形は、通さない
        print(f"Teams の見張りの関門が呼び出しを確かめられませんでした（{type(e).__name__}）", file=sys.stderr)
    return 2


def capture() -> int:
    try:
        d = json.load(sys.stdin)
        if d.get("tool_name") != P + "get_page_text":
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
        out = os.environ["VL_TEAMS_OUT"]
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:  # noqa: BLE001  書けなければ、読む側で「取得に失敗」になる
        pass
    return 0


if __name__ == "__main__":
    sys.exit({"guard": guard, "capture": capture}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: 2)())
