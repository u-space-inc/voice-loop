"""一覧の表示に使う小さな計算（ハブの一覧とタスクの一覧で使う）"""


def page(items: list, number: int, size: int = 20) -> list:
    """1 始まりのページ番号で、そのページの要素を返す"""
    start = number * size
    return items[start:start + size]


def recent(items: list, n: int = 5) -> list:
    """新しい順に n 件を返す（items は古い順に並んでいる）"""
    return items[:n]
