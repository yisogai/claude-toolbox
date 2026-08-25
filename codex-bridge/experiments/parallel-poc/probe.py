import sys, json, time
sys.path.insert(0,'/private/tmp/claude-504/-Users-isogai--claude/565e1f4f-8342-40ab-b8e7-bff87fc4d411/scratchpad/poc')
import parallel_poc as P
P.RPC_LOG = P.POC/'rpc-log-probe.jsonl'
P.STDERR_LOG = P.POC/'appserver-stderr-probe.log'
log = P.RpcLog(P.RPC_LOG)
srv = P.AppServer(log)
try:
    ip = {"clientInfo":{"name":"codex-parallel-poc","title":"Parallel PoC","version":"0.1.0"}}
    rid = srv.request("initialize", ip)
    try:
        res = srv.wait_response(rid, 15.0)
    except TimeoutError:
        print("JSONL no reply -> content-length")
        srv.send_framing="content-length"
        rid = srv.request("initialize", ip)
        res = srv.wait_response(rid, 15.0)
    print("SEND FRAMING:", srv.send_framing, "RECV FRAMING:", srv.recv_framing)
    print("INIT:", json.dumps(res, ensure_ascii=False)[:600])
    srv.notify("initialized", {})
    wd = P.POC/'work1'; wd.mkdir(exist_ok=True)
    r = srv.call("thread/start", {"cwd":str(wd),"ephemeral":True,"approvalPolicy":"never","sandbox":"read-only"}, timeout=60)
    print("THREAD:", json.dumps({k:r.get(k) for k in ('approvalPolicy','sandbox','model','cwd')},ensure_ascii=False))
    print("THREAD ID:", (r.get('thread') or {}).get('id'))
    time.sleep(2)
    print("EVENTS:", [e['method'] for e in srv.snapshot_events()])
finally:
    srv.close(); log.close()
