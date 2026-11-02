"""端末ごとの設定と状態の置き場。本体のフォルダ（herdr が入れ直すと置き換わる）の外で、場所は herdr がプラグインに用意するフォルダ。"""
import os
from pathlib import Path

PLUGIN_ID = "u-space.voice-loop"  # herdr-plugin.toml の id と同じ
CONFIG_DIR = Path(os.environ.get("VL_CONFIG_DIR") or Path.home() / ".config/herdr/plugins/config" / PLUGIN_ID)
STATE_DIR = Path(os.environ.get("VL_STATE_DIR") or Path.home() / ".local/state/herdr/plugins" / PLUGIN_ID)
