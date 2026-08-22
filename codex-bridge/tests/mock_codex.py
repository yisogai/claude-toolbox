#!/usr/bin/env python3
"""codex バイナリのモック。Codex 未インストール環境で配管全体をテストするために使う。

`codex_run.py --mock <scenario>` から **codex 実バイナリの代わりに**起動される。
起動引数は `mock_codex.py <scenario> <codex に渡すはずだった引数...>` で、実行経路
（ストリーミング・タイムアウト・kill・job.json 生成）は本物と同じコードを通る。

シナリオ:
  ok            file_change 2 件 + command_execution 2 件 + agent_message + turn.completed。
                実際に <cd>/MOCK_TOUCHED.txt と MOCK_TOUCHED_2.txt を書く。
  failed        turn.failed を出して exit 1。
  hang          thread.started の後は無限に待つ（idle timeout の検証）。
  slow          1 秒おきにイベントを出し続ける（壁時計 timeout の検証）。
  exit0_no_turn turn.completed に到達せず exit 0（error 判定の検証）。
  schema        last.md に review.schema.json 準拠の JSON を書いて turn.completed。
  garbage       非 JSON 行・不正 UTF-8・dict でない JSON を混ぜてから turn.completed。
  envdump       子プロセスの環境変数の有無を <cd>/MOCK_ENV.json に書いて turn.completed。
  escape        setsid で孫プロセスを残したまま無限に待つ（親を kill しても stdout が
                閉じないケース。壁時計タイムアウト時に job.json が出るかの検証）。
  manycmds      command_execution を 60 件出し、55 件目だけ失敗させる（上限打切りの検証）。
  manyfails     command_execution を 2,000 件すべて失敗で出す（失敗側の上限打切りの検証）。
  startup_error イベントを出さず stderr にだけ理由を書いて exit 2（起動失敗の検証）。
  partial_change file_change を in_progress / failed / completed の 3 状態で出す。
  toplevel_error 最上位 error イベントのみで turn.completed に到達せず exit 0。
  error_then_complete 最上位 error の後に turn.completed に到達する（completed 優先の検証）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

USAGE = {
    "input_tokens": 12000,
    "cached_input_tokens": 4000,
    "cache_write_input_tokens": 1000,
    "output_tokens": 3000,
    "reasoning_output_tokens": 1200,
}

THREAD_ID = "th_mock_0001"


def emit(obj) -> None:
    sys.stdout.buffer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def emit_raw(data: bytes) -> None:
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def parse_opts(argv):
    """codex 実バイナリと同じ引数から -C / -o / -m を拾う。"""
    cd, out, model = None, None, None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-C", "--cd") and i + 1 < len(argv):
            cd = argv[i + 1]
            i += 2
            continue
        if a in ("-o", "--output-last-message") and i + 1 < len(argv):
            out = argv[i + 1]
            i += 2
            continue
        if a in ("-m", "--model") and i + 1 < len(argv):
            model = argv[i + 1]
            i += 2
            continue
        i += 1
    return cd or os.getcwd(), out, model


def write_last(out, text: str) -> None:
    if not out:
        return
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)


def scenario_ok(cd, out):
    emit({"type": "thread.started", "thread_id": THREAD_ID})
    emit({"type": "turn.started"})

    p1 = os.path.join(cd, "MOCK_TOUCHED.txt")
    p2 = os.path.join(cd, "MOCK_TOUCHED_2.txt")
    with open(p1, "w", encoding="utf-8") as f:
        f.write("mock touched\n")
    with open(p2, "w", encoding="utf-8") as f:
        f.write("mock touched 2\n")

    emit({"type": "item.completed", "item": {
        "id": "it_1", "type": "file_change", "status": "completed",
        "changes": [{"path": p1, "kind": "add"}]}})
    emit({"type": "item.completed", "item": {
        "id": "it_2", "type": "file_change", "status": "completed",
        "changes": [{"path": p2, "kind": "update"}]}})
    emit({"type": "item.completed", "item": {
        "id": "it_3", "type": "command_execution", "status": "completed",
        "command": "python3 -m pytest -q", "exit_code": 0, "aggregated_output": "ok"}})
    emit({"type": "item.completed", "item": {
        "id": "it_4", "type": "command_execution", "status": "failed",
        "command": "npm run lint", "exit_code": 1, "aggregated_output": "1 problem"}})

    # 非致命の error item（ConfigWarning / DeprecationNotice 相当）。
    # exec_events.rs の Item::Error は "non-fatal error surfaced as an item" と明記されている。
    emit({"type": "item.completed", "item": {
        "id": "it_err", "type": "error", "message": "mock: deprecated config key `foo` (non-fatal)"}})

    msg = ("## 結果: 完了\n## 変更ファイル\n- MOCK_TOUCHED.txt: 追加\n"
           "- MOCK_TOUCHED_2.txt: 更新\n## 実行した検証\n- pytest: pass\n")
    write_last(out, msg)
    emit({"type": "item.completed", "item": {"id": "it_5", "type": "agent_message", "text": msg}})
    emit({"type": "turn.completed", "usage": USAGE})
    return 0


def scenario_failed(cd, out):
    emit({"type": "thread.started", "thread_id": THREAD_ID})
    emit({"type": "turn.started"})
    emit({"type": "turn.failed", "error": {"message": "mock: sandbox denied the write"}})
    return 1


def scenario_hang(cd, out):
    emit({"type": "thread.started", "thread_id": THREAD_ID})
    while True:
        time.sleep(1)


def scenario_slow(cd, out):
    emit({"type": "thread.started", "thread_id": THREAD_ID})
    emit({"type": "turn.started"})
    n = 0
    while True:
        n += 1
        emit({"type": "item.updated", "item": {
            "id": f"it_{n}", "type": "reasoning", "text": f"mock thinking {n}"}})
        time.sleep(1)


def scenario_exit0_no_turn(cd, out):
    emit({"type": "thread.started", "thread_id": THREAD_ID})
    emit({"type": "turn.started"})
    emit({"type": "item.completed", "item": {
        "id": "it_1", "type": "agent_message", "text": "途中で打ち切られた出力"}})
    return 0


def scenario_schema(cd, out):
    emit({"type": "thread.started", "thread_id": THREAD_ID})
    emit({"type": "turn.started"})
    payload = {
        "verdict": "needs-attention",
        "findings": [{
            "severity": "major", "file": "scripts/codex_run.py",
            "line_start": 10, "line_end": 12, "category": "correctness",
            "title": "タイムアウト後に子プロセスが残りうる",
            "description": "モックの指摘（テスト用）", "confidence": "high",
        }],
        "summary": "mock review",
        "next_steps": ["kill_group のテストを足す"],
    }
    text = json.dumps(payload, ensure_ascii=False)
    write_last(out, text)
    emit({"type": "item.completed", "item": {"id": "it_1", "type": "agent_message", "text": text}})
    emit({"type": "turn.completed", "usage": USAGE})
    return 0


def scenario_garbage(cd, out):
    emit({"type": "thread.started", "thread_id": THREAD_ID})
    emit_raw("これは JSON ではない行です\n".encode("utf-8"))
    emit_raw(b"\xff\xfe invalid utf-8 line \x80\x81\n")
    emit_raw(b"[1, 2, 3]\n")          # JSON だが dict ではない
    emit_raw(b'{"type": "item.completed", "item": {"broken\n')  # 途中で切れた JSON
    emit({"type": "item.completed", "item": {
        "id": "it_1", "type": "agent_message", "text": "ゴミ混じりでも完走する"}})
    emit({"type": "turn.completed", "usage": USAGE})
    return 0


def scenario_envdump(cd, out):
    emit({"type": "thread.started", "thread_id": THREAD_ID})
    emit({"type": "turn.started"})
    dump = {
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY"),
        "CODEX_API_KEY": os.environ.get("CODEX_API_KEY"),
        "keys": sorted(k for k in os.environ if "API_KEY" in k),
        "cwd": os.getcwd(),
    }
    with open(os.path.join(cd, "MOCK_ENV.json"), "w", encoding="utf-8") as f:
        json.dump(dump, f, ensure_ascii=False)
    write_last(out, "env dumped")
    emit({"type": "item.completed", "item": {"id": "it_1", "type": "agent_message", "text": "env dumped"}})
    emit({"type": "turn.completed", "usage": USAGE})
    return 0


def scenario_escape(cd, out):
    """setsid で孫を作り、親（この mock）が kill されても stdout を握らせ続ける。

    codex 側が内部で `setsid` するツールを起動した場合の再現。親のプロセスグループを
    kill しても孫は生き残り、読取スレッドは EOF を受け取れない。
    """
    emit({"type": "thread.started", "thread_id": THREAD_ID})
    emit({"type": "turn.started"})
    child = subprocess.Popen(
        # 第 2 引数（cd）は ps から自分の孫だと判別するための目印
        [sys.executable, "-c", "import time; time.sleep(30)", cd],
        start_new_session=True,   # プロセスグループを抜ける
    )                              # stdout/stderr は親から継承（= パイプを握り続ける）
    with open(os.path.join(cd, "MOCK_GRANDCHILD.pid"), "w", encoding="utf-8") as f:
        f.write(f"{child.pid}\n")
    while True:
        time.sleep(1)


def scenario_manycmds(cd, out):
    emit({"type": "thread.started", "thread_id": THREAD_ID})
    emit({"type": "turn.started"})
    for n in range(1, 61):
        failed = (n == 55)
        emit({"type": "item.completed", "item": {
            "id": f"cmd_{n}", "type": "command_execution",
            "status": "failed" if failed else "completed",
            "command": f"echo cmd-{n}" + (" && exit 1" if failed else ""),
            "exit_code": 1 if failed else 0,
            "aggregated_output": "boom" if failed else "ok"}})
    write_last(out, "## 結果: 完了\n")
    emit({"type": "turn.completed", "usage": USAGE})
    return 0


def scenario_manyfails(cd, out):
    """失敗コマンドだけを大量に出す（H-1: 失敗側にも上限が要ることの検証）。"""
    emit({"type": "thread.started", "thread_id": THREAD_ID})
    emit({"type": "turn.started"})
    for n in range(1, 2001):
        emit({"type": "item.completed", "item": {
            "id": f"fail_{n}", "type": "command_execution", "status": "failed",
            "command": f"pytest tests/test_{n}.py -x  # " + "x" * 60,
            "exit_code": 1, "aggregated_output": "boom"}})
    write_last(out, "## 結果: 完了\n")
    emit({"type": "turn.completed", "usage": USAGE})
    return 0


def scenario_startup_error(cd, out):
    """イベントを一切出さず stderr にだけ理由を書いて落ちる（引数エラー等の再現）。"""
    sys.stderr.write("error: unexpected argument '--full-auto' found\n"
                     "  tip: a similar argument exists: '--ephemeral'\n"
                     "Usage: codex exec [OPTIONS] [PROMPT]\n")
    sys.stderr.flush()
    return 2


def scenario_partial_change(cd, out):
    emit({"type": "thread.started", "thread_id": THREAD_ID})
    emit({"type": "turn.started"})
    emit({"type": "item.started", "item": {
        "id": "fc_1", "type": "file_change", "status": "in_progress",
        "changes": [{"path": os.path.join(cd, "IN_PROGRESS.txt"), "kind": "add"}]}})
    emit({"type": "item.completed", "item": {
        "id": "fc_2", "type": "file_change", "status": "failed",
        "changes": [{"path": os.path.join(cd, "FAILED.txt"), "kind": "add"}]}})
    emit({"type": "item.completed", "item": {
        "id": "fc_3", "type": "file_change", "status": "completed",
        "changes": [{"path": os.path.join(cd, "DONE.txt"), "kind": "update"}]}})
    write_last(out, "## 結果: 完了\n")
    emit({"type": "turn.completed", "usage": USAGE})
    return 0


def scenario_toplevel_error(cd, out):
    emit({"type": "thread.started", "thread_id": THREAD_ID})
    emit({"type": "turn.started"})
    emit({"type": "error", "message": "mock: fatal stream error"})
    return 0


def scenario_error_then_complete(cd, out):
    emit({"type": "thread.started", "thread_id": THREAD_ID})
    emit({"type": "turn.started"})
    emit({"type": "error", "message": "mock: transient stream hiccup"})
    write_last(out, "## 結果: 完了\n")
    emit({"type": "turn.completed", "usage": USAGE})
    return 0


SCENARIOS = {
    "ok": scenario_ok,
    "escape": scenario_escape,
    "manycmds": scenario_manycmds,
    "manyfails": scenario_manyfails,
    "startup_error": scenario_startup_error,
    "partial_change": scenario_partial_change,
    "toplevel_error": scenario_toplevel_error,
    "error_then_complete": scenario_error_then_complete,
    "failed": scenario_failed,
    "hang": scenario_hang,
    "slow": scenario_slow,
    "exit0_no_turn": scenario_exit0_no_turn,
    "schema": scenario_schema,
    "garbage": scenario_garbage,
    "envdump": scenario_envdump,
}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: mock_codex.py <scenario> [codex args...]", file=sys.stderr)
        return 2
    scenario, rest = argv[0], argv[1:]
    fn = SCENARIOS.get(scenario)
    if fn is None:
        print(f"unknown scenario: {scenario}", file=sys.stderr)
        return 2

    cd, out, _model = parse_opts(rest)

    # プロンプトは stdin から来る（本物と同じく "-" 指定）。読み捨てて親の書込を詰まらせない。
    if "-" in rest and not sys.stdin.closed:
        try:
            sys.stdin.read()
        except Exception:
            pass

    return fn(cd, out) or 0


if __name__ == "__main__":
    sys.exit(main())
