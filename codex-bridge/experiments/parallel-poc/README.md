# app-server 並列 PoC（2026-08-25）

`codex app-server` 1プロセスに JSON-RPC(v2, 素の JSONL) で 3 thread を作り、`turn/start` を
連続送信して並行実行を実測確認した PoC。結果: 3本の sleep 20 実行区間が 19 秒重なり（直列なら
60 秒のところ全体 34 秒）、auth.json の mtime は不変（トークン競合なし）。

- `probe.py` — 課金なしのハンドシェイク・フレーミング確認
- `parallel_poc.py` — 本体（`python3 parallel_poc.py`、PARALLEL 判定で exit 0。turn 3個ぶんの
  ChatGPT クレジットを消費するので乱発しない）
- `result.json` — 実測の生データ

背景・設計判断は `docs/research/2026-08-25-codex-local-parallel.md` を参照。
プロトコル要点: initialize{clientInfo} → thread/start{cwd, ephemeral:true, approvalPolicy:"never",
sandbox:"read-only"} → turn/start{threadId, input:[{type:"text",text:…}], sandboxPolicy:{type:"readOnly"}}。
完了は turn/completed 通知、最終文は item/completed の agentMessage。
