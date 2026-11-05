# 試験用（マージしない）: execution_file から、探りのコマンドの結果と実行の構造だけを出す。
# 結果の本文はこのあと「名前=整数」の行だけに絞ってから表示する。
def text:
  if type == "string" then .
  elif type == "array" then map(select(.type == "text") | .text) | join("\n")
  else "" end;
([.[] | select(.type == "system" and .subtype == "init")][0] // {}) as $init
| [.[] | select(.type == "assistant") | .message.content[]? | select(.type == "tool_use")] as $uses
| [$uses[] | select(.name == "Bash" and .input.command == "bash probe/sandbox_probe.sh")] as $probe
| ($probe | map(.id)) as $ids
| [.[] | select(.type == "user") | .message.content[]? | select(.type == "tool_result")
       | select(.tool_use_id as $t | $ids | index($t))] as $results
| [.[] | select(.type == "result") | .permission_denials[]?] as $denied
| "meta_tool_calls=\($uses | length)",
  "meta_probe_calls=\($probe | length)",
  "meta_probe_disable_sandbox=\([$probe[] | select(.input.dangerouslyDisableSandbox == true)] | length)",
  "meta_probe_errors=\([$results[] | select(.is_error == true)] | length)",
  "meta_denied=\($denied | length)",
  "meta_claude_code_version=\($init.claude_code_version // "")",
  ($results[] | .content | text)
