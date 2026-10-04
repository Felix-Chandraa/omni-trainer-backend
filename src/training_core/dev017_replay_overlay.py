# DEV-017 replay actuator overlay.
# Never mutates original evidence. It copies the package into runtime,
# enriches server/state*.jsonl payloads from aircraft.tlog SERVO_OUTPUT_RAW
# and VFR_HUD, updates common integrity metadata, then DEV-014 replays the
# derived runtime copy. No FDM/ArduPilot worker is launched.
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from typing import Any

try:
    from pymavlink import mavutil
except Exception as exc:
    mavutil = None
    _PYMAVLINK_ERROR = exc
else:
    _PYMAVLINK_ERROR = None

class Dev017ReplayError(RuntimeError):
    pass

@dataclass(frozen=True)
class ActuatorSample:
    utc_s: float
    srv1: int | None
    srv2: int | None
    srv3: int | None
    srv4: int | None
    throttle: float | None

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.srv1 is not None: out["srv1"] = self.srv1
        if self.srv2 is not None: out["srv2"] = self.srv2
        if self.srv3 is not None: out["srv3"] = self.srv3
        if self.srv4 is not None: out["srv4"] = self.srv4
        if self.throttle is not None: out["throttle"] = self.throttle
        return out

def _sha256(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda:f.read(1024*1024), b""):
            h.update(block)
    return h.hexdigest()

def _state_files(package: Path) -> list[Path]:
    return sorted((package/"server").glob("state.*.jsonl"))

def _state_utc_bounds(package: Path) -> tuple[float,float]:
    first=None; last=None
    for f in _state_files(package):
        with f.open("r",encoding="utf-8") as h:
            for line in h:
                if not line.strip(): continue
                r=json.loads(line)
                ns=(r.get("time") or {}).get("server_utc_ns")
                if isinstance(ns,int):
                    s=ns/1e9
                    if first is None: first=s
                    last=s
    if first is None or last is None:
        raise Dev017ReplayError("server state has no server_utc_ns")
    return first,last

def read_actuators(tlog: Path, state_start_s: float, state_end_s: float) -> list[ActuatorSample]:
    if mavutil is None:
        raise Dev017ReplayError(f"pymavlink unavailable: {_PYMAVLINK_ERROR}")
    if not tlog.is_file() or tlog.stat().st_size <= 0:
        raise Dev017ReplayError(f"missing/empty tlog: {tlog}")

    conn=mavutil.mavlink_connection(str(tlog), robust_parsing=True, notimestamps=False)
    samples=[]
    servo=[None,None,None,None]
    throttle=None
    unix_ts=[]
    try:
        while True:
            msg=conn.recv_match(blocking=False)
            if msg is None: break
            typ=msg.get_type()
            if typ not in {"SERVO_OUTPUT_RAW","VFR_HUD"}: continue
            ts=getattr(msg,"_timestamp",None)
            if not isinstance(ts,(int,float)) or not (1_000_000_000 < float(ts) < 5_000_000_000):
                continue
            ts=float(ts); unix_ts.append(ts)
            if typ=="SERVO_OUTPUT_RAW":
                servo=[
                    int(getattr(msg,"servo1_raw",0) or 0),
                    int(getattr(msg,"servo2_raw",0) or 0),
                    int(getattr(msg,"servo3_raw",0) or 0),
                    int(getattr(msg,"servo4_raw",0) or 0),
                ]
            else:
                v=getattr(msg,"throttle",None)
                if isinstance(v,(int,float)): throttle=float(v)
            samples.append(ActuatorSample(ts,servo[0],servo[1],servo[2],servo[3],throttle))
    finally:
        try: conn.close()
        except Exception: pass

    if not samples:
        raise Dev017ReplayError("tlog has no UTC-timestamped SERVO_OUTPUT_RAW/VFR_HUD samples")
    t0=min(unix_ts); t1=max(unix_ts)
    if t1 < state_start_s-30.0 or t0 > state_end_s+30.0:
        raise Dev017ReplayError(
            "tlog UTC does not overlap server state UTC: "
            f"tlog={t0:.3f}..{t1:.3f}, state={state_start_s:.3f}..{state_end_s:.3f}"
        )
    samples.sort(key=lambda x:x.utc_s)
    return samples

def enrich_records(records: list[dict[str,Any]], samples: list[ActuatorSample]) -> int:
    # DEV017_REV5_NESTED_ACTUATORS
    # server/state keeps physical truth under payload.state.
    times=[x.utc_s for x in samples]
    changed=0

    for r in records:
        ns=(r.get("time") or {}).get("server_utc_ns")
        if not isinstance(ns,int):
            continue

        ts=ns/1e9
        idx=bisect_right(times,ts)-1
        if idx < 0:
            if times and times[0]-ts <= 1.0:
                idx=0
            else:
                continue

        sample=samples[idx]

        if ts-sample.utc_s > 2.0:
            continue

        payload=r.get("payload")
        if not isinstance(payload,dict):
            continue

        nested_state=payload.get("state")
        target=nested_state if isinstance(nested_state,dict) else payload

        add=sample.payload()
        before={k:target.get(k) for k in add}
        target.update(add)

        if any(before[k] != add[k] for k in add):
            changed += 1

    return changed

def _walk_update_file_metadata(obj: Any, rel: str, size: int, sha: str) -> int:
    hits=0; name=Path(rel).name
    if isinstance(obj,dict):
        vals=[v for v in obj.values() if isinstance(v,str)]
        refers=any(v==rel or v.endswith("/"+rel) or v==name for v in vals)
        if refers:
            for key in ("sha256","sha_256","hash"):
                if key in obj and isinstance(obj[key],str): obj[key]=sha
            for key in ("size_bytes","bytes","size"):
                if key in obj and isinstance(obj[key],int): obj[key]=size
            hits += 1
        for v in obj.values(): hits += _walk_update_file_metadata(v,rel,size,sha)
    elif isinstance(obj,list):
        for v in obj: hits += _walk_update_file_metadata(v,rel,size,sha)
    return hits

def _update_json_metadata(path: Path, changed_files: list[Path], package: Path) -> int:
    if not path.is_file(): return 0
    try: data=json.loads(path.read_text())
    except Exception: return 0
    hits=0
    for f in changed_files:
        rel=str(f.relative_to(package))
        hits += _walk_update_file_metadata(data,rel,f.stat().st_size,_sha256(f))
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(data,indent=2,sort_keys=True)+"\n")
    os.replace(tmp,path)
    return hits

def build_overlay(package: Path, runtime_root: Path) -> Path:
    package=package.expanduser().resolve()
    if not package.is_dir(): raise Dev017ReplayError(f"evidence package missing: {package}")
    if not _state_files(package): raise Dev017ReplayError("server/state.*.jsonl missing")
    tlog=package/"server"/"aircraft.tlog"
    if not tlog.is_file():
        tlogs=sorted((package/"server").glob("*.tlog"))
        if len(tlogs)!=1: raise Dev017ReplayError("cannot identify exactly one MAVLink tlog")
        tlog=tlogs[0]

    start_s,end_s=_state_utc_bounds(package)
    samples=read_actuators(tlog,start_s,end_s)
    stamp=time.strftime("%Y%m%d-%H%M%S")
    overlay=runtime_root.expanduser().resolve()/f"{stamp}-{package.name[:12]}"
    overlay.parent.mkdir(parents=True,exist_ok=True)
    if overlay.exists(): raise Dev017ReplayError(f"overlay already exists: {overlay}")
    shutil.copytree(package,overlay)

    changed_files=[]; changed_records=0
    for f in _state_files(overlay):
        records=[]
        with f.open("r",encoding="utf-8") as h:
            for line in h:
                if line.strip(): records.append(json.loads(line))
        changed_records += enrich_records(records,samples)
        tmp=f.with_suffix(f.suffix+".tmp")
        with tmp.open("w",encoding="utf-8") as h:
            for r in records: h.write(json.dumps(r,separators=(",",":"))+"\n")
        os.replace(tmp,f); changed_files.append(f)

    if changed_records <= 0:
        shutil.rmtree(overlay,ignore_errors=True)
        raise Dev017ReplayError("no server state records could be aligned to tlog actuator samples")

    index_hits=_update_json_metadata(overlay/"index.json",changed_files,overlay)
    manifest_hits=_update_json_metadata(overlay/"manifest.json",changed_files,overlay)
    idx=overlay/"index.json"
    if idx.is_file(): _update_json_metadata(overlay/"manifest.json",[idx],overlay)

    note={
        "schema":"omni.dev017.replay-overlay.v1",
        "source_evidence":str(package),
        "derived_runtime_copy":str(overlay),
        "source_tlog":str(tlog),
        "state_utc_start_s":start_s,
        "state_utc_end_s":end_s,
        "actuator_samples":len(samples),
        "enriched_state_records":changed_records,
        "index_metadata_hits":index_hits,
        "manifest_metadata_hits":manifest_hits,
        "fields":["srv1","srv2","srv3","srv4","throttle"],
        "immutable_source":True,
        "fdm_rerun":False,
    }
    (overlay/"DEV017_REPLAY_OVERLAY.json").write_text(json.dumps(note,indent=2,sort_keys=True)+"\n")
    return overlay

def main(argv=None) -> int:
    p=argparse.ArgumentParser()
    p.add_argument("evidence")
    p.add_argument("--runtime-root",required=True)
    a=p.parse_args(argv)
    try: out=build_overlay(Path(a.evidence),Path(a.runtime_root))
    except Dev017ReplayError as exc:
        print(f"DEV017_OVERLAY_ERROR: {exc}")
        return 70
    print(out)
    return 0

if __name__=="__main__": raise SystemExit(main())
