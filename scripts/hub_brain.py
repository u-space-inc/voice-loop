"""ハブの頭との約束。

voice-loop（herdr の拡張）は、声・画面・通知・セッションの読み書き・見張り・関門を持つ。
判断・記憶・手順・振り返り・定期実行は頭の側に置く。本体が頭に求めるのは、ここに書いた形だけ。
頭を替えても、本体の側の守り（口・発言の確かめ・関門）は変わらない。

  話す        ユーザーの発言を渡して、返事を受け取る（Talker.exchange・Talker.ask。herdr のセッションなら ask() が送って読む）
  道具        hubctl と /api/hub/ の口。どの頭にも同じ（頭は実作業をせず、セッションに頼む）
  知らせる    頼んだ件の結果をハブに届け、報告を受け取る（report_target・report_begin・report_end）
  確かめる    本体が頭の外で持つ、本人の発言の台帳（turns・heard・reply_to）。承認に答える・閉じるの守りはこれで確かめる
  提案を見せる 頭が持っている、本人の判断待ちの数（pending_note）

頭の専用の部品は、本体の道具を core だけで呼ぶ。使ってよい名前は CORE_TOOLS で、spike/brain_boundary_test.py が確かめる。
"""

# 頭の部品が使ってよい本体の道具（本体のモジュールの名前）
CORE_TOOLS = {"CLIENTS", "_clip", "_probe", "_probes", "hlog", "pane_lock", "push_all", "read_lines", "situation_counts", "situation_rules", "write_json"}


class Brain:
    """ハブの頭。既定は「持っていない」の動きで、頭ごとに必要なものだけ書く。"""

    in_herdr = False  # 頭が herdr のセッションか（そうなら、ハブのフォルダのセッションをハブとして数える）
    label = "ハブ"  # 画面と通知で、知らせの送り手として出す名前

    def ensure(self) -> dict:
        """使える状態にする。{"ok": True, "pane": …, "started": bool} か、{"ok": False, "reason": …, "detail": …}。"""
        raise NotImplementedError

    async def boot(self):
        """本体が起動したときに一度だけ呼ぶ。"""

    def watchers(self) -> list:
        """本体が動いている間、裏で回す仕事（引数なしの async 関数）。"""
        return []

    def doctor(self, add):
        """点検の項目を足す。add(名前, 問題なしか, 問題の説明, optional=False)。"""

    def turns(self, n: int = 1) -> list[tuple[str, str]]:
        """本人の発言の (ID, 文) を新しいものから n 件。"""
        raise NotImplementedError

    def heard(self, turn: str) -> str:
        """本人が発言 turn の直前に聞いたハブの返答（本体が受け取った原文）。分からなければ空（守りは断る側に倒れる）。"""
        raise NotImplementedError

    def reply_to(self, turn: str) -> str:
        """発言 turn へのハブの返答。"""
        raise NotImplementedError

    async def report_target(self) -> dict | None:
        """頼んだ件の報告を受ける相手（一覧の項目と同じ形）。居なければ None。"""
        raise NotImplementedError

    def report_begin(self, headline: str, about: str) -> bool:
        """報告の前。頭が記録を持たないときだけ、本体が見出しを記録する。記録に書けたら True。"""
        return True

    async def report_end(self, state: str, reply: str, about: str, saved: bool) -> bool:
        """報告のあと。記録に書けたかを返す。"""
        return saved

    def memory_for_intake(self) -> dict:
        """拾う役の入力に足す欄（頭が会話から覚えたこと）。"""
        return {}

    def pending_note(self) -> str:
        """朝のまとめに足す、本人の判断待ちの案内。無ければ空。"""
        return ""


class Talker:
    """herdr の外にいる話し相手。本体は pane の名前で見分ける（herdr の pane ではない）。"""

    pane = ""
    name = ""
    off_text = ""  # 使えないときに画面へ出す文

    def on(self) -> bool:
        """設定があり、話し相手として一覧に出すか。"""
        raise NotImplementedError

    def entry(self) -> dict:
        """話し相手の一覧の項目。"""
        raise NotImplementedError

    def history(self) -> list[dict]:
        """画面に出す会話（role と text）。"""
        raise NotImplementedError

    def sending_text(self) -> str:
        """送ったときの状態の文。"""
        raise NotImplementedError

    async def exchange(self, text: str) -> tuple[str, str, bool]:
        """ユーザーの発言を 1 往復する。(返事, 失敗の理由, 記録に書けたか)。"""
        raise NotImplementedError

    def ask(self, text: str) -> str:
        """1 往復して返事を返す（自動通知など。失敗は例外）。"""
        raise NotImplementedError

    def doctor_as_talker(self, add):
        """ハブでないときの点検の項目。"""
