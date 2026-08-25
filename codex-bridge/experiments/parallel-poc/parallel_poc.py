#!/usr/bin/env python3
"""codex app-server 1プロセス多重化 PoC.

1個の `codex app-server` プロセス内で 3 つの thread の turn を同時に走らせ、
各 turn がシェルで観測した START/END エポック秒の重なりから並行性を実測する。

標準ライブラリのみ。Python 3.11+。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

POC = Path(__file__).resolve().parent
RPC_LOG = POC / "rpc-log.jsonl"
STDERR_LOG = POC / "appserver-stderr.log"
RESULT_JSON = POC / "result.json"
AUTH_JSON = Path.home() / ".codex" / "auth.json"

N_THREADS = 3
SLEEP_SECS = 20
OVERALL_TIMEOUT = 8 * 60
INIT_TIMEOUT = 20.0  # このあいだに initialize 応答が無ければ別フレーミングを試す

PROMPT = (
    "シェルで `date +%s` → `sleep {sleep}` → `date +%s` を実行し、"
    "最終メッセージとして開始と終了のエポック秒2つだけを "
    "『START=<n> END=<n>』形式で報告して。他の作業はしない。"
).format(sleep=SLEEP_SECS)

T0 = time.time()


def rel(t: float | None = None) -> float:
    return round((time.time() if t is None else t) - T0, 3)


class RpcLog:
    def __init__(self, path: Path) -> None:
        self._fh = path.open("w", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, direction: str, payload: object, **extra: object) -> None:
        rec = {"t": rel(), "dir": direction, "msg": payload}
        rec.update(extra)
        with self._lock:
            self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            self._fh.close()


class AppServer:
    """codex app-server との JSON-RPC stdio クライアント。

    フレーミングは受信・送信とも自動判定する:
      - 受信: 先頭行が "Content-Length:" ならヘッダ方式、そうでなければ JSONL
      - 送信: まず JSONL、initialize 応答が来なければ Content-Length で再送
    """

    def __init__(self, log: RpcLog) -> None:
        self.log = log
        self.stderr_fh = STDERR_LOG.open("wb")
        self.proc = subprocess.Popen(
            ["codex", "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr_fh,
            cwd=str(POC),
        )
        self.send_framing = "jsonl"
        self.recv_framing: str | None = None
        self._send_lock = threading.Lock()
        self._next_id = 1
        self._responses: dict[int, dict] = {}
        self._resp_cv = threading.Condition()
        self.events: list[dict] = []  # (t, method, params) 受信通知の時系列
        self._events_lock = threading.Lock()
        self._stop = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # ---- 送信 ----
    def _encode(self, obj: dict) -> bytes:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        if self.send_framing == "jsonl":
            return body + b"\n"
        return b"Content-Length: %d\r\n\r\n" % len(body) + body

    def _send(self, obj: dict) -> None:
        data = self._encode(obj)
        with self._send_lock:
            assert self.proc.stdin is not None
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        self.log.write("send", obj, framing=self.send_framing)

    def request(self, method: str, params: dict) -> int:
        with self._resp_cv:
            rid = self._next_id
            self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return rid

    def respond(self, rid: object, result: dict) -> None:
        self._send({"jsonrpc": "2.0", "id": rid, "result": result})

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def wait_response(self, rid: int, timeout: float) -> dict:
        deadline = time.time() + timeout
        with self._resp_cv:
            while rid not in self._responses:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError(f"no response for request id={rid}")
                self._resp_cv.wait(remaining)
            return self._responses.pop(rid)

    def call(self, method: str, params: dict, timeout: float = 60.0) -> dict:
        rid = self.request(method, params)
        msg = self.wait_response(rid, timeout)
        if "error" in msg:
            raise RuntimeError(f"{method} failed: {msg['error']}")
        return msg.get("result") or {}

    # ---- 受信 ----
    def _read_message(self, fh) -> dict | None:
        while True:
            line = fh.readline()
            if not line:
                return None
            s = line.strip()
            if not s:
                continue
            if s.lower().startswith(b"content-length:"):
                n = int(s.split(b":", 1)[1].strip())
                # 残りヘッダを空行まで読み飛ばす
                while True:
                    h = fh.readline()
                    if not h or h.strip() == b"":
                        break
                body = fh.read(n)
                if self.recv_framing is None:
                    self.recv_framing = "content-length"
                return json.loads(body.decode("utf-8"))
            try:
                msg = json.loads(s.decode("utf-8"))
            except json.JSONDecodeError:
                self.log.write("recv-unparsed", s.decode("utf-8", "replace"))
                continue
            if self.recv_framing is None:
                self.recv_framing = "jsonl"
            return msg

    def _read_loop(self) -> None:
        fh = self.proc.stdout
        assert fh is not None
        while not self._stop:
            try:
                msg = self._read_message(fh)
            except Exception as exc:  # noqa: BLE001
                self.log.write("recv-error", repr(exc))
                break
            if msg is None:
                break
            self.log.write("recv", msg, framing=self.recv_framing)
            if "id" in msg and "method" not in msg:
                with self._resp_cv:
                    self._responses[msg["id"]] = msg
                    self._resp_cv.notify_all()
            elif "id" in msg and "method" in msg:
                # server -> client リクエスト（承認要求など）。承認して進める。
                m = msg["method"]
                if "pproval" in m:
                    self.respond(msg["id"], {"decision": "accept"})
                else:
                    self.respond(msg["id"], {})
            else:
                with self._events_lock:
                    self.events.append(
                        {"t": rel(), "method": msg.get("method"), "params": msg.get("params") or {}}
                    )

    def snapshot_events(self) -> list[dict]:
        with self._events_lock:
            return list(self.events)

    def close(self) -> None:
        self._stop = True
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=10)
        finally:
            for fh in (self.proc.stdin, self.proc.stdout):
                try:
                    if fh:
                        fh.close()
                except Exception:  # noqa: BLE001
                    pass
            self.stderr_fh.close()


def observe_processes(tag: str) -> dict:
    out = subprocess.run(
        ["pgrep", "-fl", "codex"], capture_output=True, text=True
    ).stdout.splitlines()
    ours = [l for l in out if re.search(r"(^|/)codex(-cli)?\s+app-server", l)]
    return {"tag": tag, "t": rel(), "total_codex_lines": len(out), "app_server_lines": ours, "all": out}


def auth_mtime() -> float | None:
    try:
        return AUTH_JSON.stat().st_mtime  # 内容は一切読まない
    except OSError:
        return None


START_END_RE = re.compile(r"START\s*=\s*(\d{9,11}).*?END\s*=\s*(\d{9,11})", re.S)


def extract_start_end(text: str) -> tuple[int, int] | None:
    m = START_END_RE.search(text or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def overlap(a: tuple[int, int], b: tuple[int, int]) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def main() -> int:
    log = RpcLog(RPC_LOG)
    workdirs = []
    for i in range(1, N_THREADS + 1):
        wd = POC / f"work{i}"
        wd.mkdir(exist_ok=True)
        workdirs.append(wd)

    auth_before = auth_mtime()
    proc_before = observe_processes("before")

    srv = AppServer(log)
    result: dict = {
        "auth_json_mtime_before": auth_before,
        "processes_before": proc_before,
        "prompt": PROMPT,
    }
    try:
        # --- initialize（フレーミング自動判定） ---
        init_params = {
            "clientInfo": {"name": "codex-parallel-poc", "title": "Parallel PoC", "version": "0.1.0"}
        }
        rid = srv.request("initialize", init_params)
        try:
            init_res = srv.wait_response(rid, INIT_TIMEOUT)
        except TimeoutError:
            print("[info] JSONL で応答なし → Content-Length フレーミングを試行", flush=True)
            srv.send_framing = "content-length"
            rid = srv.request("initialize", init_params)
            init_res = srv.wait_response(rid, INIT_TIMEOUT)
        if "error" in init_res:
            raise RuntimeError(f"initialize failed: {init_res['error']}")
        result["framing_send"] = srv.send_framing
        result["initialize_result"] = init_res.get("result")
        print(f"[ok] initialize (send framing={srv.send_framing})", flush=True)

        # initialized 通知（受理されなくても致命ではない）
        try:
            srv.notify("initialized", {})
        except Exception as exc:  # noqa: BLE001
            log.write("note", f"initialized notify failed: {exc!r}")

        # --- thread/start x3 ---
        thread_ids: list[str] = []
        for wd in workdirs:
            res = srv.call(
                "thread/start",
                {
                    "cwd": str(wd),
                    "ephemeral": True,
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                },
                timeout=60.0,
            )
            tid = (res.get("thread") or {}).get("id")
            if not tid:
                raise RuntimeError(f"thread/start returned no thread id: {res}")
            thread_ids.append(tid)
            print(f"[ok] thread/start {tid} cwd={wd.name}", flush=True)
        result["thread_ids"] = thread_ids
        result["thread_start_response_sample"] = {
            k: res.get(k) for k in ("approvalPolicy", "sandbox", "model", "cwd")
        }

        # --- turn/start を間隔なく連続送信 ---
        send_times = {}
        turn_req_ids = {}
        for tid in thread_ids:
            send_times[tid] = rel()
            turn_req_ids[tid] = srv.request(
                "turn/start",
                {
                    "threadId": tid,
                    "input": [{"type": "text", "text": PROMPT}],
                    "sandboxPolicy": {"type": "readOnly"},
                    "approvalPolicy": "never",
                },
            )
        result["turn_start_sent_at"] = send_times
        print(f"[ok] turn/start x{N_THREADS} 送信完了 (t={rel()}s)", flush=True)

        proc_mid = observe_processes("during")
        result["processes_during"] = proc_mid

        # --- turn/completed を 3 本待つ ---
        deadline = time.time() + OVERALL_TIMEOUT
        completed: dict[str, dict] = {}
        while len(completed) < N_THREADS and time.time() < deadline:
            for ev in srv.snapshot_events():
                if ev["method"] == "turn/completed":
                    tid = ev["params"].get("threadId")
                    if tid and tid not in completed:
                        completed[tid] = ev
                        print(f"[ok] turn/completed {tid} (t={ev['t']}s)", flush=True)
            if len(completed) < N_THREADS:
                time.sleep(0.5)

        events = srv.snapshot_events()
        result["timed_out"] = len(completed) < N_THREADS

        # --- 集計 ---
        per_thread = {}
        for idx, tid in enumerate(thread_ids, 1):
            started = next(
                (e["t"] for e in events if e["method"] == "turn/started" and e["params"].get("threadId") == tid),
                None,
            )
            ev = completed.get(tid)
            texts = []
            if ev:
                for item in (ev["params"].get("turn") or {}).get("items", []):
                    if item.get("type") == "agentMessage" and item.get("text"):
                        texts.append(item["text"])
            for e in events:
                if (
                    e["method"] == "item/completed"
                    and e["params"].get("threadId") == tid
                    and (e["params"].get("item") or {}).get("type") == "agentMessage"
                ):
                    t = (e["params"]["item"] or {}).get("text")
                    if t:
                        texts.append(t)
            se = None
            for t in reversed(texts):
                se = extract_start_end(t)
                if se:
                    break
            per_thread[tid] = {
                "index": idx,
                "cwd": str(workdirs[idx - 1]),
                "turn_start_sent_at": send_times.get(tid),
                "turn_started_at": started,
                "turn_completed_at": ev["t"] if ev else None,
                "turn_status": ((ev["params"].get("turn") or {}).get("status") if ev else None),
                "agent_message": texts[-1] if texts else None,
                "shell_start_epoch": se[0] if se else None,
                "shell_end_epoch": se[1] if se else None,
            }
        result["per_thread"] = per_thread

        # 重なり判定（シェル実測）
        spans = {
            tid: (v["shell_start_epoch"], v["shell_end_epoch"])
            for tid, v in per_thread.items()
            if v["shell_start_epoch"] and v["shell_end_epoch"]
        }
        pairs = []
        tids = list(spans)
        for i in range(len(tids)):
            for j in range(i + 1, len(tids)):
                a, b = spans[tids[i]], spans[tids[j]]
                pairs.append(
                    {
                        "a": per_thread[tids[i]]["index"],
                        "b": per_thread[tids[j]]["index"],
                        "overlap_sec": overlap(a, b),
                    }
                )
        result["shell_overlap_pairs"] = pairs
        triple = 0
        if len(spans) == N_THREADS:
            vals = list(spans.values())
            lo = max(v[0] for v in vals)
            hi = min(v[1] for v in vals)
            triple = max(0, hi - lo)
        result["shell_triple_overlap_sec"] = triple

        # 補助証拠: turn 通知の受信時刻ベースの重なり
        nspans = {
            tid: (v["turn_started_at"], v["turn_completed_at"])
            for tid, v in per_thread.items()
            if v["turn_started_at"] is not None and v["turn_completed_at"] is not None
        }
        ntriple = 0.0
        if len(nspans) == N_THREADS:
            vals = list(nspans.values())
            ntriple = round(max(0.0, min(v[1] for v in vals) - max(v[0] for v in vals)), 3)
        result["notification_triple_overlap_sec"] = ntriple

        result["verdict"] = (
            "PARALLEL"
            if triple >= SLEEP_SECS * 0.5
            else ("PARTIAL" if any(p["overlap_sec"] > 0 for p in pairs) else "SERIAL/UNKNOWN")
        )

    finally:
        proc_after_running = observe_processes("after-turns-before-term")
        result["processes_after_turns"] = proc_after_running
        srv.close()
        result["auth_json_mtime_after"] = auth_mtime()
        result["processes_after_term"] = observe_processes("after-term")
        result["framing_recv"] = srv.recv_framing
        RESULT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        log.close()

    # --- 表出力 ---
    print("\n=== フレーミング ===")
    print(f"送信: {result.get('framing_send')} / 受信: {result.get('framing_recv')}")
    print("\n=== タイミング表（epoch はスレッド内シェルの実測値、t は PoC 開始からの秒）===")
    hdr = f"{'#':<2} {'send t':>7} {'started t':>9} {'completed t':>11} {'START':>11} {'END':>11} {'dur':>5} status"
    print(hdr)
    for tid, v in sorted(result["per_thread"].items(), key=lambda kv: kv[1]["index"]):
        dur = (
            v["shell_end_epoch"] - v["shell_start_epoch"]
            if v["shell_start_epoch"] and v["shell_end_epoch"]
            else None
        )
        print(
            f"{v['index']:<2} {v['turn_start_sent_at'] or '-':>7} {v['turn_started_at'] or '-':>9} "
            f"{v['turn_completed_at'] or '-':>11} {v['shell_start_epoch'] or '-':>11} "
            f"{v['shell_end_epoch'] or '-':>11} {dur if dur is not None else '-':>5} {v['turn_status']}"
        )
    print("\n=== 重なり ===")
    for p in result.get("shell_overlap_pairs", []):
        print(f"thread{p['a']} x thread{p['b']}: {p['overlap_sec']}s")
    print(f"3本同時の重なり(シェル実測): {result.get('shell_triple_overlap_sec')}s")
    print(f"3本同時の重なり(turn通知受信時刻): {result.get('notification_triple_overlap_sec')}s")
    print(f"\n判定: {result.get('verdict')}")
    print(f"\nauth.json mtime: before={result.get('auth_json_mtime_before')} after={result.get('auth_json_mtime_after')}")
    print("codex app-server プロセス（実行中）:")
    for l in result["processes_during"]["app_server_lines"]:
        print("  " + l)
    print(f"\n詳細: {RESULT_JSON}\nRPCログ: {RPC_LOG}")
    return 0 if result.get("verdict") == "PARALLEL" else 1


if __name__ == "__main__":
    sys.exit(main())
