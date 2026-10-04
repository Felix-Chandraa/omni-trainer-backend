from __future__ import annotations
import argparse, json, os, signal, threading, time
from pathlib import Path
from typing import Any
from . import instructor_console_rev9r2 as r2

DEV018_REV9R3_INTERRUPTED_RECOVERY = True
CONTROL_LOSS_GRACE_SEC=max(0.0,float(os.environ.get("OMNI_CONTROL_LOSS_GRACE_SEC","1.0")))
CONTINUITY_STABLE_SEC=max(0.0,float(os.environ.get("OMNI_CONTINUITY_STABLE_SEC","1.0")))
WATCH_POLL_SEC=0.20

class InstructorRuntime(r2.InstructorRuntime):
    def __init__(self, base: Path, source_root: Path):
        super().__init__(base, source_root)
        self._r3_stop=threading.Event(); self._r3_thread=None
        self._r3_reason=None; self._r3_detail=None
        self._r3_interrupted_utc_ns=None; self._r3_recovered_utc_ns=None

    def start(self)->dict[str,Any]:
        super().start()
        with self.lock:
            cur=self._attempt()
            if cur is None or r2.rev6._state_text(cur.state).lower()!="active":
                raise RuntimeError("rev9r3 expected Active after Start")
            self._r3_reason=None; self._r3_detail=None
            self._r3_interrupted_utc_ns=None; self._r3_recovered_utc_ns=None
            self._start_watch_locked(cur.id)
            return self.status()

    def _start_watch_locked(self, attempt_id:str)->None:
        self._r3_stop.set(); self._r3_stop=threading.Event()
        self._r3_thread=threading.Thread(target=self._watch,args=(attempt_id,self._r3_stop),daemon=True,name=f"OmniR3Watch:{attempt_id[:8]}")
        self._r3_thread.start()
        self._audit("rev9r3_watch_started",{"attempt_id":attempt_id,"control_loss_grace_s":CONTROL_LOSS_GRACE_SEC,"continuity_stable_s":CONTINUITY_STABLE_SEC})

    def _continuity_locked(self)->dict[str,Any]:
        station=self._student_station_status_locked(); bridge=self._control_bridge()
        ctl={"client_connected":False,"authority":None}
        if bridge is not None:
            try: ctl=bridge.public_status()
            except Exception: pass
        expected=None if not self.ctx else self.ctx.get("student_station_id")
        actual=station.get("station_id")
        preserved=True
        if self._rev9_hold_snapshot:
            preserved=all(r2._same_process(x) for x in self._rev9_hold_snapshot.values())
        return {
            "student_connected":bool(station.get("connected")),
            "student_last_seen_age_s":station.get("last_seen_age_s"),
            "station_id":actual,"expected_station_id":expected,
            "same_station":bool(expected and actual and expected==actual),
            "control_connected":bool(ctl.get("client_connected")),
            "authority_present":bool(ctl.get("authority")),
            "authority":ctl.get("authority"),
            "process_identity_preserved":bool(preserved),
        }

    @staticmethod
    def _continuity_ok(c:dict[str,Any])->bool:
        return bool(c.get("student_connected") and c.get("same_station") and c.get("control_connected") and c.get("authority_present") and c.get("process_identity_preserved"))

    def _interrupt(self, reason:str, detail:dict[str,Any])->bool:
        with self.lock:
            cur=self._attempt()
            if cur is None: return False
            state=r2.rev6._state_text(cur.state).lower()
            if state not in {"active","paused"}: return False
            bridge=self._control_bridge()
            if bridge is None or bridge.router is None: raise RuntimeError("Flight control bridge missing")
            self._rev9_pause_barrier=True
            bridge.router.release_attempt(cur.id,reason=f"rev9r3_{reason}")
            root=self._rev9_hold_root_pid; snap=self._rev9_hold_snapshot; newly=False
            try:
                if root is None or snap is None:
                    root,snap=self._snapshot_owned_topology_locked()
                    r2._signal_topology(root,snap,signal.SIGSTOP)
                    ok,states=r2._wait_stop_state(snap,stopped=True,timeout=2.0)
                    if not ok: raise RuntimeError(f"fault hold incomplete: {states}")
                    newly=True
                intr=self.sessions.transition(self.instructor,cur.id,self.contract["State"].INTERRUPTED,expected_revision=cur.revision,reason=reason)
                now=time.monotonic()
                if state=="active" and self._rev9_active_segment_mono is not None:
                    self._rev9_active_accum_s+=max(0.0,now-self._rev9_active_segment_mono); self._rev9_active_segment_mono=None
                if newly:
                    self._rev9_hold_root_pid=root; self._rev9_hold_snapshot=snap; self._rev9_hold_started_mono=now
                self.ctx["active_attempt"]=None; self.ctx["paused_attempt"]=None; self.ctx["interrupted_attempt"]=intr
                self._r3_reason=reason; self._r3_detail=dict(detail); self._r3_interrupted_utc_ns=time.time_ns(); self._r3_recovered_utc_ns=None
                self._record_lifecycle_locked("Interrupted",intr.revision)
                self._audit("attempt_interrupted_authoritative_hold",{"attempt_id":cur.id,"from_state":state,"reason":reason,"revision":intr.revision,"authority_assignment_retained":True,"detail":detail})
                return True
            except Exception:
                if newly and root is not None and snap:
                    try:
                        r2._signal_topology(root,snap,signal.SIGCONT); r2._wait_stop_state(snap,stopped=False,timeout=2.0)
                    finally: self._rev9_pause_barrier=False
                raise

    def _recover_to_paused(self, c:dict[str,Any])->bool:
        with self.lock:
            cur=self._attempt()
            if cur is None or r2.rev6._state_text(cur.state).lower()!="interrupted": return False
            if not self._continuity_ok(c): return False
            if self._rev9_hold_snapshot is None or self._rev9_hold_root_pid is None: raise RuntimeError("Interrupted hold snapshot missing")
            paused=self.sessions.transition(self.instructor,cur.id,self.contract["State"].PAUSED,expected_revision=cur.revision,reason="continuity_verified_after_interruption",continuity_verified=True)
            self.ctx["interrupted_attempt"]=None; self.ctx["paused_attempt"]=paused; self.ctx["continuity_verified"]=True
            self._r3_recovered_utc_ns=time.time_ns(); self._record_lifecycle_locked("Paused",paused.revision)
            self._audit("interrupted_continuity_verified_to_paused",{"attempt_id":cur.id,"revision":paused.revision,"continuity":c,"authoritative_hold_remains":True,"auto_resume":False})
            return True

    def _watch(self, attempt_id:str, stop:threading.Event)->None:
        ctl_lost=None; stable=None
        while not stop.wait(WATCH_POLL_SEC):
            try:
                with self.lock:
                    if not self.ctx or self.ctx.get("attempt_id")!=attempt_id: return
                    cur=self._attempt()
                    if cur is None: return
                    state=r2.rev6._state_text(cur.state).lower()
                    if state=="ended": return
                    c=self._continuity_locked()
                now=time.monotonic()
                if state in {"active","paused"}:
                    stable=None
                    if not c["student_connected"]:
                        self._interrupt("student_station_heartbeat_timeout",c); ctl_lost=None; continue
                    if not c["control_connected"]:
                        if ctl_lost is None: ctl_lost=now
                        if now-ctl_lost>=CONTROL_LOSS_GRACE_SEC:
                            self._interrupt("flight_control_client_disconnected",c); ctl_lost=None
                        continue
                    ctl_lost=None
                    if not c["authority_present"]:
                        self._interrupt("flight_authority_assignment_missing",c); continue
                    if state=="paused" and not c["process_identity_preserved"]:
                        self._interrupt("paused_process_identity_changed",c); continue
                elif state=="interrupted":
                    ctl_lost=None
                    if self._continuity_ok(c):
                        if stable is None: stable=now
                        if now-stable>=CONTINUITY_STABLE_SEC:
                            self._recover_to_paused(c); stable=None
                    else: stable=None
            except Exception as exc:
                self.last_error=f"rev9r3 watchdog: {type(exc).__name__}: {exc}"
                try: self._audit("rev9r3_watch_error",{"attempt_id":attempt_id,"error":str(exc)})
                except Exception: pass

    def _cleanup_runtime(self)->None:
        self._r3_stop.set(); super()._cleanup_runtime()

    def status(self)->dict[str,Any]:
        out=super().status(); out["dev"]="DEV-018-rev9r3"; cur=out.get("current")
        if cur is not None:
            try:
                with self.lock: cont=self._continuity_locked()
            except Exception: cont=None
            state=str(cur.get("state") or "").lower()
            cur["interrupt"]={
                "reason":self._r3_reason,"detail":self._r3_detail,
                "interrupted_utc_ns":self._r3_interrupted_utc_ns,
                "recovery_verified_utc_ns":self._r3_recovered_utc_ns,
                "recovery_verified":bool(self._r3_recovered_utc_ns and state=="paused"),
                "continuity":cont,"auto_resume":False,
                "authority_assignment_retained":True,
            }
            cur["fault_policy"]={"enabled":True,"control_loss_grace_s":CONTROL_LOSS_GRACE_SEC,"continuity_stable_s":CONTINUITY_STABLE_SEC,"student_heartbeat_fresh_s":getattr(r2.rev6.v1,"STUDENT_HEARTBEAT_FRESH_SEC",5.0)}
        out["limitations"]=[
            "Student heartbeat/control loss now causes authoritative Flight HOLD and INTERRUPTED.",
            "Recoverable faults release active RC override but retain the same Student Flight authority assignment.",
            "Verified continuity moves INTERRUPTED to PAUSED while the Flight stack remains held.",
            "No automatic resume to ACTIVE exists; Instructor RESUME is mandatory.",
            "Payload/resource/event-scheduler full-pause semantics are not yet claimed.",
        ]
        return out

def probe(base:Path,source:Path)->dict[str,Any]:
    out=r2.probe(base,source); out.update({"rev9r3":True,"automatic_interrupted_fault_policy":True,"fault_hold_authoritative":True,"continuity_recovery_to_paused":True,"auto_resume":False,"authority_assignment_retained_on_recoverable_fault":True}); out["ok"]=bool(out.get("ok")); return out

def serve(base:Path,source:Path,host:str,port:int,web_root:Path)->int:
    runtime=InstructorRuntime(base,source); r2.Handler.runtime=runtime; r2.Handler.web_root=web_root
    server=r2.rev6.ThreadingHTTPServer((host,port),r2.Handler); runtime.public_port=port; stopped=threading.Event()
    def req(signum=None,frame=None):
        if stopped.is_set(): return
        stopped.set(); threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGINT,req); signal.signal(signal.SIGTERM,req)
    print(f"OMNI Instructor Console DEV-018 rev9 rev3: http://{host}:{port}/",flush=True)
    print("Fault: continuity loss -> HOLD -> INTERRUPTED; verified reconnect -> PAUSED; Instructor RESUME required.",flush=True)
    try: server.serve_forever(poll_interval=0.2)
    finally: server.server_close(); runtime.shutdown()
    return 0

def main(argv=None)->int:
    ap=argparse.ArgumentParser(); ap.add_argument("--base",required=True); ap.add_argument("--source",required=True); ap.add_argument("--host",default="0.0.0.0"); ap.add_argument("--port",type=int,default=8020); ap.add_argument("--web-root"); ap.add_argument("--probe",action="store_true"); a=ap.parse_args(argv)
    base=Path(a.base).expanduser().resolve(); source=Path(a.source).expanduser().resolve()
    if a.probe:
        x=probe(base,source); print(json.dumps(x,indent=2,sort_keys=True)); return 0 if x.get("ok") else 70
    web=Path(a.web_root).expanduser().resolve() if a.web_root else base/"web"/"instructor_console_v1"
    if not (web/"index.html").is_file() or not (web/"student.html").is_file(): raise SystemExit(f"web assets missing: {web}")
    return serve(base,source,a.host,a.port,web)

if __name__=="__main__": raise SystemExit(main())
