#!/usr/bin/env bash
# 試験用（マージしない）: Claude が動かすコマンドから、親プロセスの鍵が読めるかを確かめる。
# 鍵の値は出さない。出力は「名前=整数」の行だけ（printf の %d で数に限る）。
#   bash probe/sandbox_probe.sh                 … 箱の中（Claude の Bash）から見る
#   bash probe/sandbox_probe.sh watch OUT STOP  … 箱の外で見張り、STOP ができるまで最大を OUT に書く
set -u

PROC=${PROBE_PROC:-/proc}
SCAN_TMP=${PROBE_TMP:-/tmp}
OAUTH_NAME='^[A-Z_]*CLAUDE_CODE_OAUTH_TOKEN='
OAUTH_FORM='sk-ant-oat[0-9][0-9]-'
OAUTH_FORM_FILE='sk-ant-oat[0-9][0-9]-[A-Za-z0-9_-]{40,}'
GITHUB_FORM='gh[sopu]_[A-Za-z0-9]{20,}'
CANARY='^VL_PROBE_CANARY='
if command -v timeout >/dev/null 2>&1; then TO='timeout 15'; else TO=''; fi

say() { printf '%s=%d\n' "$1" "$2"; }

# 環境変数が自分にあるか（値は捨てる）
is_set() { if printenv "$1" >/dev/null 2>&1; then printf 1; else printf 0; fi; }

# ファイルを開けるか
opens() { if { : < "$1"; } 2>/dev/null; then printf 1; else printf 0; fi; }

# 型に合う行を持つファイルの数。/proc の environ・cmdline は NUL 区切りなので行にして見る
count_matching() {
  pat=$1; shift
  n=0
  for f in "$@"; do
    if tr '\000' '\n' 2>/dev/null < "$f" | grep -q -E -e "$pat"; then n=$((n + 1)); fi
  done
  printf '%d' "$n"
}

# フォルダの下で、型に合う行を持つファイルの数。フォルダが無ければ -1
count_tree() {
  if [ -z "$2" ] || [ ! -d "$2" ]; then printf '%d' -1; return; fi
  printf '%d' "$(( $($TO grep -r -l -s -a -D skip -E -e "$1" "$2" 2>/dev/null | wc -l) ))"
}

# プロセスの見え方と、ほかのプロセスの環境・引数に鍵があるか
proc_metrics() {
  p=$1
  envs=( "$PROC"/[0-9]*/environ )
  cmds=( "$PROC"/[0-9]*/cmdline )
  procs=0; opened=0; nonempty=0; claude=0
  for d in "$PROC"/[0-9]*; do [ -d "$d" ] && procs=$((procs + 1)); done
  for f in "${envs[@]}"; do
    if { : < "$f"; } 2>/dev/null; then
      opened=$((opened + 1))
      if tr '\000' '\n' 2>/dev/null < "$f" | grep -q .; then nonempty=$((nonempty + 1)); fi
    fi
  done
  for c in "$PROC"/[0-9]*/comm; do
    [ "$(cat "$c" 2>/dev/null)" = claude ] && claude=$((claude + 1))
  done
  pid1=$(cat "$PROC/1/comm" 2>/dev/null)
  say "${p}procs_visible" "$procs"
  say "${p}pid1_is_bwrap" "$([ "$pid1" = bwrap ] && echo 1 || echo 0)"
  say "${p}pid1_is_systemd" "$([ "$pid1" = systemd ] && echo 1 || echo 0)"
  say "${p}claude_procs" "$claude"
  say "${p}environ_opened" "$opened"
  say "${p}environ_nonempty" "$nonempty"
  say "${p}environ_oauth_name" "$(count_matching "$OAUTH_NAME" "${envs[@]}")"
  say "${p}environ_oauth_form" "$(count_matching "$OAUTH_FORM" "${envs[@]}")"
  say "${p}environ_github_form" "$(count_matching "$GITHUB_FORM" "${envs[@]}")"
  say "${p}environ_canary" "$(count_matching "$CANARY" "${envs[@]}")"
  say "${p}cmdline_oauth_form" "$(count_matching "$OAUTH_FORM" "${cmds[@]}")"
}

inside() {
  # 1. 自分の環境（鍵を外す設定が効いたか）
  for name in CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN GITHUB_TOKEN GH_TOKEN \
              OVERRIDE_GITHUB_TOKEN ACTIONS_RUNTIME_TOKEN ACTIONS_ID_TOKEN_REQUEST_TOKEN; do
    say "self_env_$(printf '%s' "$name" | tr 'A-Z' 'a-z')" "$(is_set "$name")"
  done
  say self_env_oauth_form "$(env | grep -c -E -e "$OAUTH_FORM")"
  say self_env_github_form "$(env | grep -c -E -e "$GITHUB_FORM")"
  # 2. 自分と親
  say self_pid "$$"
  say parent_pid "$PPID"
  say parent_environ_opened "$(opens "$PROC/$PPID/environ")"
  say parent_environ_oauth_name "$(count_matching "$OAUTH_NAME" "$PROC/$PPID/environ")"
  # 3. ほかのプロセス
  proc_metrics ""
  # 4. 読めるファイル
  rt=${RUNNER_TEMP:-$HOME/work/_temp}
  ws=${GITHUB_WORKSPACE:-$PWD}
  say files_oauth_form_tmp "$(count_tree "$OAUTH_FORM_FILE" "$SCAN_TMP")"
  say files_oauth_form_runner_temp "$(count_tree "$OAUTH_FORM_FILE" "$rt")"
  say files_oauth_form_home_claude "$(count_tree "$OAUTH_FORM_FILE" "$HOME/.claude")"
  say files_oauth_form_home_config "$(count_tree "$OAUTH_FORM_FILE" "$HOME/.config")"
  say files_oauth_form_workspace "$(count_tree "$OAUTH_FORM_FILE" "$ws")"
  say files_github_form_workspace "$(count_tree "$GITHUB_FORM" "$ws")"
}

watch_loop() {
  out=$1; stop=$2
  secs=${PROBE_WATCH_SECS:-900}
  cur=$out.cur; tmp=$out.tmp
  i=0; hits=0
  rm -f "$out"
  while [ "$i" -lt "$secs" ] && [ ! -e "$stop" ]; do
    proc_metrics outside_ > "$cur"
    if grep -q -E '^outside_environ_oauth_form=[1-9]' "$cur"; then hits=$((hits + 1)); fi
    i=$((i + 1))
    [ -s "$out" ] || cp "$cur" "$out"
    awk -F= 'NR == FNR { m[$1] = $2 + 0; next }
             { if (!($1 in m) || $2 + 0 > m[$1]) m[$1] = $2 + 0 }
             END { for (k in m) if (k !~ /^outside_samples/) printf "%s=%d\n", k, m[k] }' "$out" "$cur" > "$tmp"
    say outside_samples "$i" >> "$tmp"
    say outside_samples_oauth_seen "$hits" >> "$tmp"
    sort -o "$tmp" "$tmp"
    mv "$tmp" "$out"
    sleep 1
  done
  rm -f "$cur"
}

case ${1:-inside} in
  inside) inside ;;
  watch) watch_loop "${2:?OUT}" "${3:?STOP}" ;;
  *) echo "usage: $0 [watch OUT STOP]" >&2; exit 2 ;;
esac
