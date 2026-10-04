from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import socket
import threading
import time
import uuid

import websockets

from .active_start import AuthorizedActiveStart
from .client_evidence_gateway import EvidenceLanTelemetryGateway
from .evidence_recorder import AttemptEvidenceRecorder, AttemptIdentity
from .fg_adapter import FGObserver
from .models import Actor, CoreError, Role, State
from .multi_live_trial import evidence, slot_ports_free, wait_tcp_cleanup, worker_argv
from .multi_runtime import RuntimeSlotAllocator, extract_arduplane_template, find_latest_working_worker_log, validate_template
from .omni_launcher_adapter import choose_jsbsim, source_from_root
from .readiness import TrustedReadinessCoordinator
from .session import SessionManager
from .store import Store
from .worker_lifecycle import AttemptWorkerCoordinator

KNOT_TO_MPS=0.514444


def free_tcp_port():
    s=socket.socket(socket.AF_INET,socket.SOCK_STREAM); s.bind(('127.0.0.1',0)); p=s.getsockname()[1]; s.close(); return p


def state_dict(p):
    return {'lat_deg':p.lat_deg,'lon_deg':p.lon_deg,'alt_msl_m':p.alt_msl_m,'agl_m':p.agl_raw_m,
            'roll_deg':p.roll_deg,'pitch_deg':p.pitch_deg,'yaw_deg':p.yaw_deg,
            'vcas_mps':None if p.vcas_kt is None else float(p.vcas_kt)*KNOT_TO_MPS,
            'climb_mps':p.climb_mps,'frame':'FGNetFDM'}


def visual_dict(p):
    return {'lat':p.lat_deg,'lon':p.lon_deg,'alt':p.agl_raw_m,'alt_msl':p.alt_msl_m,'agl':p.agl_raw_m,
            'roll':p.roll_deg,'pitch':p.pitch_deg,'yaw':p.yaw_deg,'hdg':p.yaw_deg%360.0,'pose_source':'fg'}


async def client(uri, token, done, out):
    try:
        async with websockets.connect(uri,max_size=8192) as ws:
            await ws.send(json.dumps({'type':'hello','protocol':'omni.training.v1','role':'student',
                'student_id':'student-1','token':token,'station_id':'student-station-1','training_role':'FLIGHT'}))
            welcome=json.loads(await asyncio.wait_for(ws.recv(),5))
            if welcome.get('type')!='welcome': raise RuntimeError(f'no welcome: {welcome}')
            out['welcome']=welcome

            ping='dev013-ping-1'; sent=time.monotonic_ns()
            await ws.send(json.dumps({'type':'clock_ping','ping_id':ping,'client_send_mono_ns':sent,'client_utc_ms':int(time.time()*1000)}))
            while True:
                msg=json.loads(await asyncio.wait_for(ws.recv(),5))
                if msg.get('type')=='clock_pong': break
            await ws.send(json.dumps({'type':'clock_sample','ping_id':ping,'client_recv_mono_ns':time.monotonic_ns()}))

            tele=None
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                msg=json.loads(await asyncio.wait_for(ws.recv(),5))
                if msg.get('type')=='telemetry': tele=msg; break
            if tele is None: raise RuntimeError('no telemetry')
            d=tele['data']
            await ws.send(json.dumps({'type':'client_event','action':'telemetry_received','scope':'observation',
                'client_mono_ns':time.monotonic_ns(),'client_utc_ms':int(time.time()*1000),
                'data':{'seq':tele.get('seq'),'lat':d.get('lat'),'lon':d.get('lon'),'alt_msl':d.get('alt_msl'),
                        'actor_id':'spoofed-client-actor','attempt_id':'spoofed-attempt'}}))
            await ws.send(json.dumps({'type':'client_event','action':'ui_click','scope':'ui',
                'client_mono_ns':time.monotonic_ns(),'client_utc_ms':int(time.time()*1000),
                'data':{'target_id':'viewFollow','engineering_headless_client':True}}))
            await ws.send(json.dumps({'type':'cmd','name':'arm'}))
            while True:
                reply=json.loads(await asyncio.wait_for(ws.recv(),5))
                if reply.get('type')=='error':
                    out['command_error']=reply.get('code'); break
            out['telemetry_seq']=tele.get('seq')
            await asyncio.sleep(0.25)
    except Exception as exc:
        out['error']=f'{type(exc).__name__}: {exc}'
    finally:
        done.set()


def run_trial(base:Path, source_root:Path, live_seconds:float):
    source=source_from_root(source_root); jsbsim=choose_jsbsim(source,home=Path.home())
    proven=find_latest_working_worker_log(base); template=extract_arduplane_template(proven); validate_template(template)
    runtime=base/'runtime'/'dev013-live'/f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    runtime.mkdir(parents=True,exist_ok=False)
    tj=runtime/'arduplane-template.json'; tj.write_text(json.dumps({'source_worker_log':str(proven),'arduplane_argv':list(template)},indent=2))
    store=Store(runtime/'training.sqlite'); sessions=SessionManager(store); workers=AttemptWorkerCoordinator(store,runtime/'workers')
    readiness=TrustedReadinessCoordinator(store,sessions,workers); active_gate=AuthorizedActiveStart(store,sessions,workers)
    slots=RuntimeSlotAllocator(runtime/'slot-leases',max_slots=3); instructor=Actor('dev013-instructor',Role.INSTRUCTOR)
    attempt=None; lease=None; rec=None; gateway=None; worker_started=False; accepted=False
    try:
        sid=sessions.create_session(instructor,'student-1'); eid=sessions.create_exercise(instructor,sid,'dev013-real-client-evidence-v1')
        attempt=sessions.create_attempt(instructor,eid,'omni-1'); lease=slots.claim(attempt_id=attempt.id,session_id=sid,aircraft_id=attempt.aircraft_id,preferred_slot=1)
        if not slot_ports_free(lease): raise CoreError('slot 1 busy before DEV-013')
        rec=AttemptEvidenceRecorder(runtime/'evidence',AttemptIdentity(sid,eid,attempt.id,attempt.aircraft_id,attempt.generation),
            configuration={'dev':'DEV-013','real_websocket_client':True,'feather_gui_used':False,'flight_commands_enabled':False})
        rec.record_lifecycle('Setup',revision=attempt.revision); rec.start_mavlink_tlog(bind_host='127.0.0.1',port=lease.slot.mav_client_udp)
        ws_port=free_tcp_port(); gateway=EvidenceLanTelemetryGateway('127.0.0.1',ws_port)
        assignment=gateway.register_assignment(student_id='student-1',session_id=sid,attempt_id=attempt.id,aircraft_id=attempt.aircraft_id,generation=attempt.generation,token='dev013-loopback-token')
        gateway.register_evidence_principal(assignment,rec,station_id='student-station-1',training_role='FLIGHT')

        with FGObserver(lease.slot.fg_udp) as fg:
            h=workers.start(instructor,attempt.id,argv=worker_argv(base,source,jsbsim,tj,lease,runtime/'direct'/attempt.id),cwd=base); worker_started=True
            ev=evidence(source,base,fg,workers,instructor,attempt,lease,60.0); ready,_=readiness.mark_ready(instructor,attempt.id,ev,expected_revision=attempt.revision); rec.record_lifecycle('Ready',revision=ready.revision)
            ev2=evidence(source,base,fg,workers,instructor,attempt,lease,15.0); active,_=active_gate.start_active(instructor,attempt.id,ev2,expected_revision=ready.revision); rec.record_lifecycle('Active',revision=active.revision)
            gateway.start(); done=threading.Event(); result={}
            th=threading.Thread(target=lambda:asyncio.run(client(f'ws://127.0.0.1:{ws_port}',assignment.token,done,result)),daemon=True); th.start()
            deadline=time.monotonic()+max(5.0,live_seconds); states=0
            while True:
                if time.monotonic()>deadline+8: raise CoreError('client evidence timeout')
                p=fg.drain()
                if p is not None:
                    rec.record_state(state_dict(p),source='FGNetFDM'); gateway.publish_telemetry(assignment,visual_dict(p),quality={'pose_source':'fg'}); states+=1
                if done.is_set() and states>=30 and time.monotonic()>=deadline: break
                time.sleep(1/60)
            th.join(timeout=2)
            if result.get('error'): raise CoreError('client failed: '+result['error'])
            if result.get('command_error')!='commands_disabled_dev013': raise CoreError('command path not fail-closed')
            cur=store.attempt(attempt.id); ended=sessions.transition(instructor,attempt.id,State.ENDED,expected_revision=cur.revision,reason='DEV-013 proof complete'); rec.record_lifecycle('Ended',revision=ended.revision)

        gateway.stop(); gateway=None; workers.stop(instructor,attempt.id,timeout=15.0); worker_started=False; wait_tcp_cleanup(lease,timeout=12.0); slots.release(attempt.id)
        idx_path=rec.finalize(status='complete'); idx=json.loads(idx_path.read_text()); pkg=idx_path.parent
        ch=idx['channels'].get('clients/student-station-1/events',{}); clk=idx['channels'].get('clients/student-station-1/clock',{}); st=idx['channels'].get('server/state',{}); tl=idx.get('mavlink_tlog') or {}
        if ch.get('records_written',0)<4: raise CoreError(f'client evidence too small: {ch}')
        if clk.get('records_written',0)<1: raise CoreError('clock evidence missing')
        if st.get('records_written',0)<30: raise CoreError('state evidence too small')
        if tl.get('frames',0)<10: raise CoreError('tlog too small')
        rows=[]
        for rel in ch.get('segments',[]):
            rows += [json.loads(x) for x in (pkg/rel).read_text().splitlines() if x.strip()]
        tr=[r for r in rows if r.get('payload',{}).get('action')=='telemetry_received']
        if not tr: raise CoreError('telemetry_received missing')
        row=tr[0]
        if row['payload']['actor_id']!='student-1': raise CoreError('client replaced authoritative actor identity')
        if row['identity']['attempt_id']!=attempt.id: raise CoreError('client replaced authoritative Attempt identity')
        if slots.snapshot(): raise CoreError('lease remains')
        if not slot_ports_free(lease): raise CoreError('slot not reusable')
        print('RECORDED REAL CLIENT PATH:',f"server_states={st.get('records_written')}",f"tlog_frames={tl.get('frames')}",f"client_events={ch.get('records_written')}",f"clock_samples={clk.get('records_written')}")
        print('ATTRIBUTION VERIFIED: actor=student-1 station=student-station-1 role=FLIGHT')
        print('COMMAND FAIL-CLOSED VERIFIED: commands_disabled_dev013')
        print('EVIDENCE PACKAGE:',pkg)
        print('STATUS: DEV-013 ACCEPTED (engineering)')
        print('Proof: real worker + real WebSocket client evidence ingestion.')
        accepted=True; return 0
    except Exception as exc:
        print('STATUS: DEV-013 FAILED:',f'{type(exc).__name__}: {exc}'); return 133
    finally:
        if gateway is not None:
            try: gateway.stop()
            except Exception: pass
        if worker_started and attempt is not None:
            try: workers.stop(instructor,attempt.id,timeout=15.0)
            except Exception as exc: print('worker cleanup warning:',exc)
        if attempt is not None:
            try: slots.release(attempt.id)
            except Exception: pass
        if rec is not None and not rec._finalized:
            try: rec.abort('DEV-013 accepted' if accepted else 'DEV-013 live trial aborted')
            except Exception as exc: print('recorder cleanup warning:',exc)
        store.close()


def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument('--base',required=True); p.add_argument('--source',required=True); p.add_argument('--live-seconds',type=float,default=6.0); a=p.parse_args(argv)
    return run_trial(Path(a.base).expanduser().resolve(),Path(a.source).expanduser().resolve(),a.live_seconds)

if __name__=='__main__': raise SystemExit(main())
