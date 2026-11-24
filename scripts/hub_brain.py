"""ハブの頭との約束。

voice-loop（herdr の拡張）は、声・画面・通知・セッションの読み書き・見張り・関門を持つ。
判断・記憶・手順・振り返り・定期実行は頭の側に置く。本体が頭に求めるのは、ここに書いた形だけ。
頭を替えても、本体の側の守り（口・発言の確かめ・関門）は変わらない。

  話す        ユーザーの発言を渡して、返事を受け取る（Talker.exchange・Talker.ask。herdr のセッションなら ask() が送って読む）
  道具        hubctl と /api/hub/ の口。どの頭にも同じ（頭は実作業をせず、セッションに頼む）
  知らせる    頼んだ件の結果をハブに届け、報告を受け取る（report_target・report_begin・report_end）
  確かめる    本体が頭の外で持つ、本人の発言の台帳（turns・heard・reply_to）。承認に答える・閉じるの守りはこれで確かめる

頭の専用の部品は、本体の道具を core だけで呼ぶ。使ってよい名前は CORE_TOOLS で、spike/brain_boundary_test.py が確かめる。
"""
import os


def _name_char(ch: str) -> bool:
    """呼び名に使える 1 字: ひらがな・カタカナ・漢字（々）・英数字（全角も）・「・ー」。ほかの文字（記号・空白・制御、ハングルの埋め字のような
    見えない文字、ほかの文字体系の似た形の字）は受け付けない。"""
    o = ord(ch)
    return (ch.isascii() and ch.isalnum()) or 0x3041 <= o <= 0x3096 or 0x309D <= o <= 0x309F or 0x30A1 <= o <= 0x30FA or 0x30FC <= o <= 0x30FF \
        or 0x4E00 <= o <= 0x9FFF or ch in "々・" or 0xFF10 <= o <= 0xFF19 or 0xFF21 <= o <= 0xFF3A or 0xFF41 <= o <= 0xFF5A


def _agent_name(raw: str) -> tuple[str, str]:
    """(呼び名, 使えなかった値)。画面の文字・通知・Slack の文にそのまま入れるので、使える字（_name_char）だけ、16 字まで。"""
    v = (raw or "").strip()
    if not v:
        return "", ""
    return (v, "") if len(v) <= 16 and all(_name_char(ch) for ch in v) else ("", v)


# 利用者が話しかける相手の呼び名（config.env の VL_AGENT_NAME）。画面・通知・Slack に出す。書かなければ、これまでの表示のまま
# （役の名前を出していたところは「ハブ」、話す相手として部品の名前を出していたところは「Hermes」）。コードの名前・記録の印は変えない
AGENT_NAME, AGENT_NAME_BAD = _agent_name(os.environ.get("VL_AGENT_NAME", ""))
AGENT = AGENT_NAME or "ハブ"
AGENT_H = AGENT_NAME or "Hermes"
AGENT_H_SP = AGENT_H + (" " if AGENT_H[-1].isascii() and AGENT_H[-1].isalnum() else "")  # 「Hermes の」と「ヴィカの」の書き分け

# 頭の部品が使ってよい本体の道具（本体のモジュールの名前）
CORE_TOOLS = {"CLIENTS", "_clip", "_probe", "_probes", "hlog", "pane_lock", "push_all", "read_lines", "read_sent", "write_json"}


class Brain:
    """ハブの頭。既定は「持っていない」の動きで、頭ごとに必要なものだけ書く。"""

    label = AGENT  # 画面と通知で、知らせの送り手として出す名前

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

    def via(self, turn: str) -> str:
        """発言 turn がどこから来たか。機械（スタックチャン）からなら "device"、画面からなら空。
        承認に答える・閉じるの守りが、機械からの発言を答えに使わないために見る。記録に残らない頭は "device"（断る側）を返す。"""
        return "device"

    def current(self, turn: str) -> bool:
        """発言 turn を受け付けたあとに、利用者が止めていない・次の発言をしていないか。承認に答える・閉じるの守りが、操作の直前に見る
        （止めた・話しかけ直したあとに、頭が回の途中で答えを送ったり閉じたりしないように。2026-11-24、a2a 30fd5b94）。分からなければ False（断る側）。"""
        return False

    def reply_to(self, turn: str) -> str:
        """発言 turn へのハブの返答。"""
        raise NotImplementedError

    def replying(self, turn: str) -> str | None:
        """発言 turn が、画面の「返信」で報告を指したものなら、その報告のセッション（分からなければ空）。返信でなければ None。
        返信の発言の「直前に聞いた文」（heard）は返信先の報告。返信の発言は、閉じる許可には使わない（本体が断る）。"""
        raise NotImplementedError

    def reply_ask(self, turn: str) -> dict | None:
        """発言 turn が、画面の「返信」で報告を指したものなら、その報告が見ていた問い {"pane", "fp", "session"}（report_end の ask。
        分からない欄は空）。返信でなければ None。本体は、その問いがいまも同じセッションに出ているときだけ、返信をその問いへの答えに使う。"""
        return None

    async def report_target(self) -> dict | None:
        """頼んだ件の報告を受ける相手（一覧の項目と同じ形）。居なければ None。"""
        raise NotImplementedError

    def report_begin(self, headline: str, about: str) -> bool:
        """報告の前。頭が記録を持たないときだけ、本体が見出しを記録する。記録に書けたら True。"""
        return True

    def heard_mark(self) -> int:
        """いまの受け付けの番号（発言を受け付けたとき・止めたときに進む。時計は使わない）。報告を頼んだときに控え、report_end に渡す。"""
        return 0

    async def report_end(self, state: str, reply: str, about: str, saved: bool, pane: str = "", topic: str = "", ask: dict | None = None,
                         mark: int | None = None) -> bool:
        """報告のあと。記録に書けたかを返す。topic は、どの件の報告か（記録を持つ頭は、報告の記録に書く）。
        ask は、止まった報告でハブが見た問い {"fp": 画面の指紋, "session": セッション}（記録を持つ頭は報告の記録に書き、reply_ask で返す）。
        mark は報告を頼んだときの heard_mark。そのあとに利用者が話しかけた・止めたなら、報告は「利用者が聞いた文」にしない。
        報告は report_delivered で鳴らす画面に送り終えてから「利用者が聞いた文」にする（開いている画面が無ければ、聞いた文にしない）。"""
        return saved

    def report_delivered(self, mark: int | None = None):
        """報告を、鳴らす画面に送り終えた。頼んだあとに利用者が話しかけた・止めていなければ、ここで報告を「利用者が聞いた文」にする。"""
        return None

    def reply_topic(self) -> str:
        """画面の「↩ 返信」で報告に返した発言を、頭がいま扱っているなら、その報告の件（この間に頭が送った依頼は同じ件の続き）。無ければ空。"""
        return ""

    def topic_report(self, tid: str) -> dict | None:
        """件の一番新しい報告（{"at": 記録の時刻, "about": 名前}）。画面が件を開くとき、返信先にする。無ければ None。"""
        return None

    def open_items(self) -> dict:
        """あなたの判断を待っている件。{"items": [{"n", "title", "project"}]（新しい順）, "problem": 読めなかったか, "partial": 一部だけ読んだか}。
        本体が決まった問い（「次は？」）に答えるときに読む。頭が判断待ちを持たなければ items は空。"""
        return {"items": [], "problem": False, "partial": False}


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

    def admit(self) -> int:
        """利用者の新しい発言を受け付けた（添付の用意・順番待ちの前に、受けたその場で呼ぶ）。受け付けの番号を進めて返す。exchange の admitted に渡す。
        これより前に受け付けた往復と頼んだ報告の返事は、あとで届いても「利用者が聞いた文」にしない（利用者は次の発言に移り、その返事は読まれない）。"""
        return 0

    async def exchange(self, text: str, reply_at: float | None = None, extra: str = "", item: int | None = None,
                       via: str = "", admitted: int | None = None) -> tuple[str, str, bool]:
        """ユーザーの発言を 1 往復する。admitted は admit の番号（無ければ往復を始めたときの番号）。返事が届いたときに番号が進んでいたら
        （そのあとに話しかけた・止めた）、返事は記録に残すが「利用者が聞いた文」と台帳の返事の欄には置かない。via は発言の来た所（機械なら "device"。台帳に残し、承認に答える・閉じるの守りが見る）。(返事, 失敗の理由, 記録に書けたか)。reply_at は、画面の「返信」が指した報告の記録の時刻（無ければ None）。
        item は、画面の受け箱（💡）で押した判断待ちの記録の番号（無ければ None。形が違えば -1）。その件への返信として渡し、text は空でもよい。
        返信と同じく、承認に答える・閉じるには使わせない。
        extra は本体が発言の末尾に足す行（添付の場所）。相手に渡す文と記録には付けるが、承認・閉じるの守りが見る発言は利用者の言葉（text）のまま。"""
        raise NotImplementedError

    async def refused(self, text: str, why: str, via: str = ""):
        """利用者の発言 text を、本体が送らずに断った（via は発言の来た所。機械なら "device"）（添付の形・宛先など。理由 why）。断った発言も台帳と記録に載せ、承認・閉じるの文脈を失効させる
        （利用者が最後に聞いたのは本体の断り。前の問い・確認への答えに、次の発言を使わせない）。"""
        raise NotImplementedError

    async def answered_here(self, text: str, answer: str, via: str = "") -> bool:
        """利用者の発言 text に、本体が決まった問いとしてその場で答えた（answer。LLM・頭を通していない。scripts/quick.py）。
        発言と答えを台帳と記録に載せる（「すぐ次の発言」を進める）。利用者が最後に聞いたのは本体の答えなので、次の発言の「聞いた文」は空にする
        （本体の答えのあとの「はい」を、その前に聞いた頭の問いへの答えに使わせない）。頭が次に受け取る発言には、本体が答えたことを添えて文脈を保つ。
        記録に書けたかを返す。"""
        raise NotImplementedError

    def delivered(self, admitted: int):
        """admit の番号で受け付けた発言への返事を、画面に送り終えた。そのあとに利用者が話しかけた・止めていなければ、ここで返事を
        「利用者が聞いた文」と台帳の返事の欄に置く（返事ができただけでは置かない。送る前に話しかけ直した・止めたなら、返事は聞いていない）。"""
        return None

    def stopped(self):
        """利用者が画面・顔で「止める」を押した（読み上げと返事待ちを止めた）。利用者は、いま読まれていた返事と、まだ届いていない返事を聞いていない。
        受け付けの番号を進め、承認に答える・閉じるの守りが「利用者が聞いた問い」として読む文（直前に聞いた文と、台帳の最新の返事）を空にする。
        止める前に受け付けた往復と頼んだ報告の返事は、あとで届いても「聞いた文」にしない（記録には残す。止めたあとの「はい」を、聞いていない問いへの答えに使わせない。2026-11-24）。"""
        raise NotImplementedError

    def put_attachment(self, name: str, data: bytes) -> str:
        """画面から添付したファイルを、この話し相手が開ける場所に置き、その場所を返す（置けなければ RuntimeError）。名前は本体が付けたもの。"""
        raise NotImplementedError

    def ask(self, text: str) -> str:
        """1 往復して返事を返す（自動通知など。失敗は例外）。"""
        raise NotImplementedError
