#!/usr/bin/env python3
"""codex_pool.py 用の JSONL app-server モック。

環境変数 ``MOCK_APP_SERVER_CONFIG`` に JSON を渡す。``jobs`` はプロンプト文字列をキーにし、
``delay`` / ``message`` / ``fail`` / ``hang`` を指定できる。``die_after_turns`` は受信した
turn/start 件数でプロセス全体を途中終了させる。``MOCK_APP_SERVER_RECEIVE_LOG`` があれば、
受信順を JSONL に追記する。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time


def config() -> dict:
    try:
        value = json.loads(os.environ.get("MOCK_APP_SERVER_CONFIG", "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


CFG = config()
LOCK = threading.Lock()
THREADS: dict[str, str] = {}
TURN_COUNT = 0


def emit(obj: dict) -> None:
    with LOCK:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def log_received(method: str, params: dict) -> None:
    path = os.environ.get("MOCK_APP_SERVER_RECEIVE_LOG")
    if not path:
        return
    record = {"at": time.time(), "method": method, "params": params}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def log_argv() -> None:
    """起動引数をテスト側に渡す（pool の config 配管検証専用）。"""
    path = os.environ.get("MOCK_APP_SERVER_ARGV_LOG")
    if not path:
        return
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(sys.argv, fh, ensure_ascii=False)


def reply(ident, result=None, error=None) -> None:
    obj = {"jsonrpc": "2.0", "id": ident}
    if error is not None:
        obj["error"] = error
    else:
        obj["result"] = result or {}
    emit(obj)


def run_turn(thread_id: str, prompt: str, opts: dict) -> None:
    if opts.get("hang"):
        return
    # 全 turn の最初の item を先に送るため、同時 turn の通知が必ず交錯する。
    emit({"jsonrpc": "2.0", "method": "turn/started", "params": {"threadId": thread_id}})
    emit({"jsonrpc": "2.0", "method": "item/completed", "params": {
        "threadId": thread_id,
        "item": {"id": f"item-{thread_id}", "type": "commandExecution", "status": "completed",
                 "command": f"mock {prompt}", "exitCode": 0},
    }})
    time.sleep(float(opts.get("delay", 0.04)))
    if opts.get("auth_error"):
        emit({"jsonrpc": "2.0", "method": "error", "params": {"threadId": thread_id, "message": "401 token_invalidated"}})
        return
    if opts.get("fail"):
        emit({"jsonrpc": "2.0", "method": "turn/failed", "params": {
            "threadId": thread_id, "error": {"message": opts.get("message", f"mock failed: {prompt}")},
        }})
        return
    text = opts.get("message", f"完了: {prompt}")
    emit({"jsonrpc": "2.0", "method": "item/completed", "params": {
        "threadId": thread_id,
        "item": {"id": f"message-{thread_id}", "type": "agentMessage", "text": text},
    }})
    if opts.get("legacy_usage"):
        usage = {"input_tokens": 10, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
                 "output_tokens": 5, "reasoning_output_tokens": 0}
        emit({"jsonrpc": "2.0", "method": "turn/completed", "params": {
            "threadId": thread_id, "turn": {"usage": usage},
        }})
    else:
        usage = {"totalTokens": 15, "inputTokens": 10, "cachedInputTokens": 0,
                 "cacheWriteInputTokens": 0, "outputTokens": 5, "reasoningOutputTokens": 0}
        emit({"jsonrpc": "2.0", "method": "thread/tokenUsage/updated", "params": {
            "threadId": thread_id, "turnId": f"turn-{TURN_COUNT}",
            "tokenUsage": {"total": usage, "last": usage, "modelContextWindow": 258400},
        }})
        emit({"jsonrpc": "2.0", "method": "turn/completed", "params": {
            "threadId": thread_id,
            "turn": {"id": f"turn-{TURN_COUNT}", "status": "completed", "items": []},
        }})
    time.sleep(float(opts.get("rate_limits_delay", 0)))
    emit({"jsonrpc": "2.0", "method": "account/rateLimits/updated", "params": {
        "rateLimits": {
            "limitId": "codex", "limitName": None,
            "primary": {"usedPercent": 12.5, "windowDurationMins": 10080,
                        "resetsAt": 1788835715},
            "secondary": None,
            "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
            "planType": "pro", "rateLimitReachedType": None, "spendControlReached": None,
        },
    }})


def main() -> int:
    global TURN_COUNT
    log_argv()
    for raw in sys.stdin:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        method, params, ident = msg.get("method"), msg.get("params") or {}, msg.get("id")
        if not method:
            continue
        log_received(method, params)
        if method == "initialize":
            reply(ident, {"serverInfo": {"name": "mock-app-server"}})
        elif method == "thread/start":
            thread_id = f"thread-{len(THREADS) + 1}"
            THREADS[thread_id] = ""
            reply(ident, {"thread": {"id": thread_id}})
        elif method == "turn/start":
            thread_id = params.get("threadId")
            input_items = params.get("input") or []
            prompt = str((input_items[0] if input_items else {}).get("text", ""))
            TURN_COUNT += 1
            reply(ident, {"turn": {"id": f"turn-{TURN_COUNT}"}})
            if CFG.get("die_after_turns") and TURN_COUNT >= int(CFG["die_after_turns"]):
                # 応答を返してから死ぬことで、pool 側の EOF 復旧を確実に通す。
                os._exit(17)
            opts = (CFG.get("jobs") or {}).get(prompt, {})
            threading.Thread(target=run_turn, args=(thread_id, prompt, opts), daemon=True).start()
        elif method == "turn/interrupt":
            reply(ident, {"interrupted": True})
        elif ident is not None:
            reply(ident, {})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
