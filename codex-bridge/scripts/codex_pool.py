#!/usr/bin/env python3
"""短命の codex app-server に複数のジョブを投入するワーカープール。

一つのバッチだけを処理する子プロセスとして app-server を起動し、JSON-RPC の thread/start /
turn/start を最大 ``--max-parallel`` 件まで並行させる。``--codex-config`` と
``--web-search`` は app-server の ``-c`` 設定として渡す。app-server が死んだ場合、タイムアウト、
シグナル、認証異常のいずれでも running の job.json を残さないことを最優先にする。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import codex_lib as lib  # noqa: E402
from codex_run import AUTH_PATTERN, Slot, acquire_slot, try_acquire_slot  # noqa: E402,F401


MAX_PARALLEL = 4
REQUEST_TIMEOUT = 30.0
POLL_SEC = 0.05
GRACE_SEC = 3.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex_pool.py", description="codex app-server の短命ワーカープール")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="jobs-file のジョブを一括実行する")
    run.add_argument("--jobs-file", required=True, help="jobs 配列を持つ JSON ファイル")
    run.add_argument("--pool-dir", required=True, help="pool.json と jobs/ の出力先")
    run.add_argument("--max-parallel", type=int, default=3, help="同時 turn 数（既定 3、最大 4）")
    run.add_argument("--timeout-sec", type=float, default=3600.0, help="プール全体の壁時計秒")
    run.add_argument("--job-timeout-sec", type=float, default=1800.0, help="各ジョブの壁時計秒")
    run.add_argument("--idle-timeout-sec", type=float, default=600.0, help="全通知が止まる許容秒")
    run.add_argument("--drain-ms", type=float, default=1000.0, help="全ジョブ終了後に末尾通知を待つミリ秒")
    run.add_argument("--model", default="gpt-5.6-terra", help="既定モデル")
    run.add_argument("--effort", default="high", help="既定 reasoning effort")
    run.add_argument("--codex-config", action="append", default=[], metavar="KEY=VALUE",
                     help="app-server に渡す Codex 設定（-c KEY=VALUE、繰り返し指定可）")
    run.add_argument("--web-search", action="store_true",
                     help='Web 検索を live モードで許可（-c web_search="live" を追加）')
    run.add_argument("--codex-bin", default=None, help="codex 実バイナリ")
    run.add_argument("--mock-server", default=None, help="app-server を模す Python スクリプト（テスト専用）")
    return parser


def read_json(path: Path, what: str):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{what} を読めない: {exc}") from exc


def load_jobs(path: Path) -> list[dict]:
    raw = read_json(path, "jobs-file")
    jobs = raw.get("jobs") if isinstance(raw, dict) else None
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= 8:
        raise ValueError("jobs-file の jobs は 1〜8 件の配列である必要があります")
    seen: set[str] = set()
    out: list[dict] = []
    for n, job in enumerate(jobs, 1):
        if not isinstance(job, dict):
            raise ValueError(f"jobs[{n}] は object である必要があります")
        ident = job.get("id")
        if not isinstance(ident, str) or not ident or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for c in ident):
            raise ValueError(f"jobs[{n}].id は一意な英数字ハイフン文字列である必要があります")
        if ident in seen:
            raise ValueError(f"jobs[{n}].id が重複しています: {ident}")
        seen.add(ident)
        has_prompt = isinstance(job.get("prompt"), str)
        has_file = isinstance(job.get("prompt_file"), str)
        if has_prompt == has_file:
            raise ValueError(f"jobs[{n}] は prompt または prompt_file の一方だけを指定してください")
        cwd = job.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            raise ValueError(f"jobs[{n}].cwd は必須です")
        cwd_path = Path(cwd).expanduser().resolve()
        if not cwd_path.is_dir():
            raise ValueError(f"jobs[{n}].cwd は存在するディレクトリである必要があります: {cwd_path}")
        if "write" in job and not isinstance(job["write"], bool):
            raise ValueError(f"jobs[{n}].write は bool である必要があります")
        for key in ("model", "effort", "output_schema_file"):
            if key in job and not isinstance(job[key], str):
                raise ValueError(f"jobs[{n}].{key} は文字列である必要があります")
        prompt = job.get("prompt")
        if has_file:
            prompt_path = Path(job["prompt_file"]).expanduser().resolve()
            try:
                prompt = prompt_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ValueError(f"jobs[{n}].prompt_file を読めない: {exc}") from exc
        schema = None
        if job.get("output_schema_file"):
            schema = read_json(Path(job["output_schema_file"]).expanduser().resolve(), f"jobs[{n}].output_schema_file")
        out.append({**job, "cwd": str(cwd_path), "prompt": prompt, "output_schema": schema})
    return out


def kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return
    try:
        proc.wait(timeout=GRACE_SEC)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


def _iter_error_values(msg: dict):
    """通知メッセージから「エラーを表すフィールド」だけを取り出す。

    コンテンツ系 item（userMessage の echo・agentMessage 本文・commandExecution の
    コマンド/出力・reasoning 等）は照合しない。対象リポのコードやプロンプトが
    401 / unauthorized / codex login を普通に含むため、内容照合は必ず誤爆する
    （2026-08-25 に二度実誤爆: プロンプト echo → 修正後に commandExecution 出力）。
    本物の認証エラーは error 系フィールドにしか現れない。
    """
    params = msg.get("params") or {}
    for v in (msg.get("error"), params.get("error")):
        if v:
            yield v
    item = params.get("item") or {}
    if isinstance(item, dict) and item.get("type") == "error":
        yield item
    turn = params.get("turn") or {}
    if isinstance(turn, dict) and turn.get("error"):
        yield turn["error"]
    status = params.get("status")
    if isinstance(status, dict) and status.get("error"):
        yield status["error"]


def _auth_text(value) -> bool:
    if isinstance(value, dict):
        return any(_auth_text(v) for v in value.values())
    if isinstance(value, list):
        return any(_auth_text(v) for v in value)
    # 素朴な部分一致（"401" 等）はパス名等で誤爆するため codex_run.AUTH_PATTERN に統一。
    return isinstance(value, str) and AUTH_PATTERN.search(value) is not None


def contains_auth_error(msg) -> bool:
    if not isinstance(msg, dict):
        return False
    return any(_auth_text(v) for v in _iter_error_values(msg))


class RpcClient:
    """JSONL app-server の単一 reader。通知は owner が threadId で振り分ける。"""

    def __init__(self, argv: list[str], cwd: str, on_notification):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     cwd=cwd, start_new_session=True)
        self._on_notification = on_notification
        self._send_lock = threading.Lock()
        self._cv = threading.Condition()
        self._responses: dict[int, dict] = {}
        self._next_id = 1
        self.eof = False
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.stderr_reader = threading.Thread(target=self._drain_stderr, daemon=True)
        self.stderr_lines: list[str] = []
        self.stderr_reader.start()

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr_lines.append(line.decode("utf-8", "replace"))

    def _read(self) -> None:
        assert self.proc.stdout is not None
        try:
            for raw in self.proc.stdout:
                try:
                    msg = json.loads(raw.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                if "id" in msg and "method" not in msg:
                    with self._cv:
                        self._responses[msg["id"]] = msg
                        self._cv.notify_all()
                elif "id" in msg and "method" in msg:
                    # approvalPolicy=never でも、未知の server request で停止しない。
                    self.respond(msg["id"], {"decision": "accept"} if "pproval" in msg["method"] else {})
                else:
                    self._on_notification(msg)
        finally:
            with self._cv:
                self.eof = True
                self._cv.notify_all()

    def send(self, obj: dict) -> None:
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self._send_lock:
            if self.proc.stdin is None:
                raise BrokenPipeError("app-server stdin がない")
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def request(self, method: str, params: dict) -> int:
        with self._cv:
            ident = self._next_id
            self._next_id += 1
        self.send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        return ident

    def respond(self, ident, result: dict) -> None:
        self.send({"jsonrpc": "2.0", "id": ident, "result": result})

    def notify(self, method: str, params: dict) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def call(self, method: str, params: dict, timeout: float = REQUEST_TIMEOUT) -> dict:
        ident = self.request(method, params)
        deadline = time.monotonic() + timeout
        with self._cv:
            while ident not in self._responses:
                if self.eof:
                    raise RuntimeError("app-server が応答前に終了しました")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"{method} の応答がありません")
                self._cv.wait(remaining)
            response = self._responses.pop(ident)
        if "error" in response:
            raise RuntimeError(f"{method} failed: {response['error']}")
        return response.get("result") or {}


@dataclass
class JobState:
    spec: dict
    path: Path
    model: str
    effort: str
    started: object | None = None
    started_mono: float | None = None
    ended: object | None = None
    thread_id: str | None = None
    status: str = "queued"
    usage: dict | None = None
    usage_source: str | None = None
    model_context_window: int | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    touched: list[dict] = field(default_factory=list)
    commands: list[dict] = field(default_factory=list)
    completed_event: bool = False
    events_lock: threading.Lock = field(default_factory=threading.Lock)


class Pool:
    def __init__(self, args, jobs: list[dict], pool_dir: Path):
        self.args, self.pool_dir = args, pool_dir
        self.jobs = [JobState(j, pool_dir / "jobs" / j["id"], j.get("model") or args.model,
                              j.get("effort") or args.effort) for j in jobs]
        self.by_thread: dict[str, JobState] = {}
        self.rpc: RpcClient | None = None
        self.slot: Slot | None = None
        self.started = lib.now_utc()
        self.started_mono = time.monotonic()
        self.last_activity = self.started_mono
        self.abort_reason: str | None = None
        self.rate_limits: dict | None = None
        self.unrouted_count = 0
        self.lock = threading.Lock()

    def payload(self, job: JobState, ended=None) -> dict:
        finished = ended or job.ended or lib.now_utc()
        started = job.started or self.started
        duration = (finished - started).total_seconds()
        last = job.path / "last.md"
        return {
            "status": job.status, "exit_code": None, "thread_id": job.thread_id,
            "model": job.model, "effort": job.effort, "mode": "pool", "write": bool(job.spec.get("write", False)),
            "mock": bool(self.args.mock_server), "cwd": job.spec["cwd"],
            "started_at": lib.iso(started), "ended_at": lib.iso(finished), "duration_sec": round(max(0, duration), 3),
            "usage": job.usage, "usage_source": job.usage_source,
            "usage_partial": bool(job.usage and job.status != "completed"),
            "model_context_window": job.model_context_window,
            "credits_est": lib.credits_est(job.usage, job.model) if job.usage else None,
            "rate_limits": self.rate_limits,
            "touched_files": job.touched, "commands": job.commands,
            "last_message_path": str(last) if last.exists() and last.stat().st_size else None,
            "structured_output": None, "errors": job.errors, "warnings": job.warnings,
            "job_dir": str(job.path),
        }

    def write_job(self, job: JobState) -> None:
        job.path.mkdir(parents=True, exist_ok=True)
        # 異常終了で通知が一件も来なくても、ジョブ成果物の三点セットを揃える。
        # 「running のまま放置しない」だけでなく、収集側がファイル欠落で落ちないようにする。
        if not (job.path / "events.jsonl").exists():
            lib.atomic_write_text(job.path / "events.jsonl", "")
        if not (job.path / "last.md").exists():
            lib.atomic_write_text(job.path / "last.md", "")
        payload = self.payload(job)
        last = job.path / "last.md"
        if job.spec.get("output_schema") is not None:
            if payload["last_message_path"]:
                try:
                    payload["structured_output"] = json.loads(last.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    payload["warnings"].append(f"last.md を JSON として解釈できなかった: {exc}")
            else:
                payload["warnings"].append("output_schema_file を指定したが last.md が空のため structured_output は null")
        if payload["usage"] and not self.args.mock_server:
            try:
                lib.append_jsonl(lib.usage_ledger_path(), {
                    "ts": payload["ended_at"], "job_dir": str(job.path), "mode": "pool", "model": job.model,
                    "effort": job.effort, "write": payload["write"], "cwd": payload["cwd"],
                    "claude_session_id": os.environ.get("CLAUDE_CODE_SESSION_ID"), "thread_id": job.thread_id,
                    "resumed": False, "resume_of": None, "mock": False, "usage": payload["usage"],
                    "usage_source": payload["usage_source"], "usage_partial": payload["usage_partial"],
                    "credits_est": payload["credits_est"], "status": payload["status"],
                })
            except OSError as exc:
                payload["warnings"].append(f"使用量台帳への追記に失敗した: {exc}")
        lib.atomic_write_json(job.path / "job.json", payload)

    def _event_path(self, job: JobState) -> Path:
        job.path.mkdir(parents=True, exist_ok=True)
        return job.path / "events.jsonl"

    def on_notification(self, msg: dict) -> None:
        params = msg.get("params") or {}
        thread_id = params.get("threadId") or params.get("thread_id")
        method = msg.get("method")
        with self.lock:
            self.last_activity = time.monotonic()
            job = self.by_thread.get(thread_id)
            if contains_auth_error(msg):
                self.abort_reason = "認証エラーを受信したためプール全体を停止した。codex login で再ログインしてください"
            if method == "account/rateLimits/updated":
                rate_limits = lib.normalize_rate_limits(
                    params.get("rateLimits"), "account/rateLimits/updated")
                self.rate_limits = lib.merge_rate_limits(self.rate_limits, rate_limits)
            log_unrouted = job is None and self.unrouted_count < 500
            if log_unrouted:
                self.unrouted_count += 1
        if job is None:
            if log_unrouted:
                lib.append_jsonl(self.pool_dir / "unrouted.jsonl", msg)
            return
        lib.append_jsonl(self._event_path(job), msg)
        if method in ("item/completed", "item/updated", "item/started"):
            item = params.get("item") or {}
            kind = item.get("type")
            if kind == "agentMessage" and item.get("text"):
                job.messages.append(str(item["text"]))
            elif kind == "fileChange" and item.get("status") == "completed":
                for change in item.get("changes") or []:
                    if isinstance(change, dict) and change not in job.touched:
                        job.touched.append({"path": change.get("path"), "kind": change.get("kind")})
            elif kind == "commandExecution" and method == "item/completed":
                rec = {"command": item.get("command"), "exit_code": item.get("exitCode", item.get("exit_code")), "status": item.get("status")}
                if rec not in job.commands:
                    job.commands.append(rec)
        elif method == "thread/tokenUsage/updated":
            # 実機検証 2026-09-01（U1）: この通知は turn 完了の直前に1回だけ届き、turn の途中経過としては
            # 飛ばない（--job-timeout-sec 3 の中断ジョブでは1件も届かず usage は None のままだった）。
            # したがって pool のジョブ timeout では usage は取得できない。
            token_usage = params.get("tokenUsage") or {}
            usage = lib.normalize_usage(token_usage.get("total") or token_usage.get("last"))
            if usage:
                job.usage, job.usage_source = usage, "thread/tokenUsage/updated"
            context_window = token_usage.get("modelContextWindow")
            if isinstance(context_window, int):
                job.model_context_window = context_window
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            usage = lib.normalize_usage(turn.get("usage") or params.get("usage"))
            if usage:
                job.usage, job.usage_source = usage, "turn/completed"
            for item in turn.get("items") or []:
                if item.get("type") == "agentMessage" and item.get("text"):
                    job.messages.append(str(item["text"]))
            job.completed_event = True
        elif method == "turn/failed":
            error = params.get("error") or {}
            job.errors.append(error.get("message") if isinstance(error, dict) else str(error))
            self.finish(job, "failed")

    def finish(self, job: JobState, status: str, message: str | None = None) -> None:
        if job.status not in ("running", "queued"):
            return
        if message:
            job.errors.append(message)
        job.status, job.ended = status, lib.now_utc()
        if job.messages:
            lib.atomic_write_text(job.path / "last.md", job.messages[-1])
        self.write_job(job)

    def start_job(self, job: JobState) -> None:
        assert self.rpc is not None
        job.started, job.started_mono, job.status = lib.now_utc(), time.monotonic(), "running"
        job.path.mkdir(parents=True, exist_ok=True)
        try:
            result = self.rpc.call("thread/start", {
                "cwd": job.spec["cwd"], "ephemeral": True, "approvalPolicy": "never",
                "sandbox": "workspace-write" if job.spec.get("write", False) else "read-only", "model": job.model,
            })
            thread = result.get("thread") or {}
            job.thread_id = thread.get("id")
            if not job.thread_id:
                raise RuntimeError("thread/start が thread.id を返さなかった")
            with self.lock:
                self.by_thread[job.thread_id] = job
            params = {"threadId": job.thread_id, "input": [{"type": "text", "text": job.spec["prompt"]}],
                      "sandboxPolicy": {"type": "workspaceWrite" if job.spec.get("write", False) else "readOnly"},
                      "approvalPolicy": "never", "effort": job.effort}
            if job.spec.get("output_schema") is not None:
                params["outputSchema"] = job.spec["output_schema"]
            self.rpc.call("turn/start", params)
        except (OSError, RuntimeError, TimeoutError, BrokenPipeError) as exc:
            self.finish(job, "failed", f"ジョブ開始に失敗した: {exc}")

    def interrupt(self, job: JobState, status: str, reason: str) -> None:
        if job.status != "running":
            return
        try:
            if self.rpc and job.thread_id:
                self.rpc.call("turn/interrupt", {"threadId": job.thread_id}, timeout=2.0)
        except (OSError, RuntimeError, TimeoutError, BrokenPipeError):
            pass
        self.finish(job, status, reason)

    def finalise_unfinished(self, status: str, reason: str) -> None:
        for job in self.jobs:
            if job.status in ("running", "queued"):
                if job.status == "running":
                    self.interrupt(job, status, reason)
                else:
                    self.finish(job, status, reason)

    def run(self) -> int:
        deadline = self.started_mono + self.args.timeout_sec
        self.slot = acquire_slot(1, deadline)
        if self.slot is None:
            self.finalise_unfinished("failed", "codex 実行スロットを取得できなかった")
            return 4
        try:
            if self.args.mock_server:
                argv = [sys.executable, str(Path(self.args.mock_server).expanduser().resolve())]
            else:
                binary, _ = lib.resolve_codex_bin(self.args.codex_bin)
                if not binary:
                    self.finalise_unfinished("failed", "codex バイナリが見つからない")
                    return 4
                argv = [binary, "app-server"]
            for config in self.args.codex_config:
                argv += ["-c", config]
            if self.args.web_search:
                # 値の引用符も引数に含め、TOML の文字列として解釈させる。
                argv += ["-c", 'web_search="live"']
            try:
                self.rpc = RpcClient(argv, str(self.pool_dir), self.on_notification)
                self.rpc.call("initialize", {"clientInfo": {"name": "codex-pool", "title": "Codex Pool", "version": "1.0"}})
                self.rpc.notify("initialized", {})
            except (OSError, RuntimeError, TimeoutError, BrokenPipeError) as exc:
                self.finalise_unfinished("failed", f"app-server の起動またはハンドシェイクに失敗した: {exc}")
                return 4

            pending = deque(self.jobs)
            while pending or any(j.status == "running" for j in self.jobs):
                now = time.monotonic()
                if self.abort_reason:
                    self.finalise_unfinished("failed", self.abort_reason)
                    break
                if self.rpc.proc.poll() is not None or self.rpc.eof:
                    self.finalise_unfinished("failed", "app-server が途中で終了した")
                    break
                if now >= deadline:
                    self.finalise_unfinished("timeout", "プール全体の壁時計タイムアウト")
                    break
                if now - self.last_activity >= self.args.idle_timeout_sec:
                    self.finalise_unfinished("timeout", "app-server からの通知がないアイドルタイムアウト")
                    break
                for job in list(self.jobs):
                    if job.status == "running" and job.started_mono and now - job.started_mono >= self.args.job_timeout_sec:
                        self.interrupt(job, "timeout", "ジョブ単位の壁時計タイムアウト")
                    elif job.status == "running" and job.completed_event:
                        if job.messages:
                            self.finish(job, "completed")
                        else:
                            self.finish(job, "failed", "turn/completed を受信したが最終 agentMessage がない")
                while pending and sum(j.status == "running" for j in self.jobs) < self.args.max_parallel:
                    self.start_job(pending.popleft())
                time.sleep(POLL_SEC)
            drain_deadline = time.monotonic() + self.args.drain_ms / 1000.0
            while (time.monotonic() < drain_deadline and self.rpc.proc.poll() is None
                   and not self.rpc.eof):
                time.sleep(min(POLL_SEC, max(0, drain_deadline - time.monotonic())))
            return self.exit_code()
        finally:
            if self.rpc:
                kill_group(self.rpc.proc)
            if self.slot:
                self.slot.release()
            self.write_pool()

    def exit_code(self) -> int:
        statuses = {j.status for j in self.jobs}
        if "timeout" in statuses:
            return 3
        if statuses == {"completed"}:
            return 0
        return 2

    def write_pool(self) -> None:
        ended = lib.now_utc()
        statuses = [j.status for j in self.jobs]
        status = "completed" if statuses and all(s == "completed" for s in statuses) else ("timeout" if "timeout" in statuses else "failed")
        payload = {
            "status": status, "started_at": lib.iso(self.started), "ended_at": lib.iso(ended),
            "duration_sec": round((ended - self.started).total_seconds(), 3),
            "rate_limits": self.rate_limits,
            "jobs": [{"id": j.spec["id"], "status": j.status, "thread_id": j.thread_id,
                      "duration_sec": round(((j.ended or ended) - (j.started or self.started)).total_seconds(), 3)} for j in self.jobs],
        }
        lib.append_rate_limits({
            "ended_at": payload["ended_at"], "job_dir": str(self.pool_dir), "mode": "pool",
            "model": self.args.model, "status": status, "rate_limits": self.rate_limits,
            "mock": bool(self.args.mock_server),
        })
        lib.atomic_write_json(self.pool_dir / "pool.json", payload)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if (not (1 <= args.max_parallel <= MAX_PARALLEL) or args.timeout_sec <= 0
            or args.job_timeout_sec <= 0 or args.idle_timeout_sec <= 0 or args.drain_ms < 0):
        lib.eprint("エラー: max-parallel は 1〜4、各 timeout は正数、drain-ms は 0 以上で指定してください")
        return 1
    try:
        jobs = load_jobs(Path(args.jobs_file).expanduser().resolve())
    except ValueError as exc:
        lib.eprint(f"エラー: {exc}")
        return 1
    pool_dir = Path(args.pool_dir).expanduser().resolve()
    pool_dir.mkdir(parents=True, exist_ok=True)
    pool = Pool(args, jobs, pool_dir)

    def on_signal(signum, frame):  # noqa: ARG001
        name = signal.Signals(signum).name
        pool.finalise_unfinished("killed", f"{name} を受けたため停止した")
        if pool.rpc:
            kill_group(pool.rpc.proc)
        if pool.slot:
            pool.slot.release()
        pool.write_pool()
        os._exit(2)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)
    return pool.run()


if __name__ == "__main__":
    sys.exit(main())
