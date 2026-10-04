import { updateHUD } from "./telemetry/telemetry.js";

const DEV011_PROTOCOL = "omni.training.v1";
let socket = null;
let reconnectTimer = null;
let manualClose = false;
let remoteAssignment = null;
let remoteEvidencePrincipal = null;
let evidenceHooksInstalled = false;
let clockTimer = null;
let lastTelemetryAckMs = 0;
const DEV013_EVIDENCE = true;
const DEV015_FLIGHT_CONTROL = true;
let flightAuthority = null;
let flightHandback = null; // DEV018_R4A_ACCEPT_CONTROL
let flightCommandSeq = 0;
let flightControlEnabled = false;
let flightControlTimer = null;
let flightControlPanel = null;
let flightControlStatusEl = null;
let flightThrottleEl = null;
const flightAxes = {roll:0, pitch:0, yaw:0, throttle:0};

function commandId(prefix) {
  return `${prefix}-${Date.now()}-${++flightCommandSeq}`;
}
function sendFlightCommand(name, payload = {}) {
  if (!flightAuthority || !remoteAssignment || !socket || socket.readyState !== WebSocket.OPEN) return null;
  const id = commandId(name);
  const mono = clientMonoNs();
  const command = {
    type:"cmd",
    protocol:DEV011_PROTOCOL,
    command_id:id,
    name,
    generation:flightAuthority.generation,
    authority_epoch:flightAuthority.epoch,
    sequence:flightCommandSeq,
    client_mono_ns:mono,
    expiry_ms:250,
    payload
  };

  // DEV016_FLIGHT_INPUT_INTENT
  // Non-authoritative evidence of what this Feather client intended
  // to send. Same command id/epoch/sequence/time as the real cmd.
  socket.send(JSON.stringify({
    type:"client_event",
    action:"command_intent",
    scope:"flight",
    client_mono_ns:mono,
    client_utc_ms:Date.now(),
    data:{
      command_id:id,
      name,
      generation:flightAuthority.generation,
      authority_epoch:flightAuthority.epoch,
      sequence:flightCommandSeq,
      expiry_ms:250,
      payload
    }
  }));

  socket.send(JSON.stringify(command));
  return id;
}
function updateFlightPanel(message = null) {
  if (!flightControlStatusEl) return;
  const held = !!flightAuthority?.held;
  const acceptBtn = document.getElementById("omniFcAccept");
  if (acceptBtn) {
    acceptBtn.style.display =
      (flightHandback?.pending && !held) ? "" : "none";
  }
  flightControlStatusEl.textContent = message || (
    held
      ? `FLIGHT AUTHORITY • epoch ${flightAuthority.epoch} • ${flightControlEnabled ? "CONTROL ENABLED" : "control released"}`
      : "NO FLIGHT AUTHORITY"
  );
  if (flightThrottleEl) flightThrottleEl.textContent = `Throttle ${Math.round(flightAxes.throttle*100)}%`;
}
function sendFlightAxes() {
  if (!flightControlEnabled || !flightAuthority?.held) return;
  sendFlightCommand("flight_axes", {...flightAxes});
}
function startFlightAxisLoop() {
  clearInterval(flightControlTimer);
  flightControlTimer = setInterval(sendFlightAxes, 50);
}
function disableFlightControl(reason = "released") {
  const was = flightControlEnabled;
  flightControlEnabled = false;
  clearInterval(flightControlTimer);
  flightControlTimer = null;
  flightAxes.roll = 0; flightAxes.pitch = 0; flightAxes.yaw = 0; flightAxes.throttle = 0;
  if (flightAuthority?.held && socket && socket.readyState === WebSocket.OPEN) {
    sendFlightCommand("release_axes", {});
  }
  updateFlightPanel(was ? `CONTROL RELEASED • ${reason}` : null);
}
function enableFlightControl() {
  if (!flightAuthority?.held) {
    updateFlightPanel("Cannot enable: no Flight authority");
    return;
  }
  flightAxes.roll = 0; flightAxes.pitch = 0; flightAxes.yaw = 0; flightAxes.throttle = 0;
  flightControlEnabled = true;
  startFlightAxisLoop();
  updateFlightPanel("CONTROL ENABLED • throttle starts at 0%");
}
function installFlightControlPanel() {
  if (flightControlPanel) return;
  const panel = document.createElement("div");
  panel.id = "omniDev015FlightControl";
  Object.assign(panel.style, {
    position:"fixed", right:"16px", bottom:"16px", zIndex:"100000",
    width:"290px", padding:"12px", borderRadius:"8px",
    background:"rgba(8,12,18,0.92)", color:"#fff",
    fontFamily:"system-ui,sans-serif", fontSize:"12px",
    boxShadow:"0 4px 24px rgba(0,0,0,.45)"
  });
  panel.innerHTML = `
    <div style="font-weight:700;margin-bottom:6px">OMNI Flight Control — DEV-015</div>
    <div id="omniFcStatus" style="margin-bottom:6px">Waiting for authority…</div>
    <div id="omniFcThrottle" style="margin-bottom:8px">Throttle 0%</div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px">
      <button id="omniFcArm">ARM</button>
      <button id="omniFcAccept" style="display:none">ACCEPT CONTROL</button>
      <button id="omniFcEnable">ENABLE CONTROL</button>
      <button id="omniFcRelease">RELEASE</button>
      <button id="omniFcDisarm">DISARM</button>
    </div>
    <div style="opacity:.8;line-height:1.35">
      W/S throttle • ←/→ roll • ↑/↓ pitch axis • A/D yaw • X neutral/throttle 0<br>
      Window blur/hidden → automatic release. Server deadman: 350 ms.
    </div>`;
  document.body.appendChild(panel);
  flightControlPanel = panel;
  flightControlStatusEl = panel.querySelector("#omniFcStatus");
  flightThrottleEl = panel.querySelector("#omniFcThrottle");
  panel.querySelectorAll("button").forEach((b)=>{
    Object.assign(b.style,{cursor:"pointer",padding:"5px 7px"});
  });
  panel.querySelector("#omniFcArm").addEventListener("click", ()=>{
    disableFlightControl("arming");
    sendFlightCommand("arm", {});
    updateFlightPanel("ARM requested…");
  });

  panel.querySelector("#omniFcAccept").addEventListener("click", ()=>{
    if (!flightHandback?.pending || !flightHandback?.handback_id) {
      updateFlightPanel("No pending Flight handback");
      return;
    }
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      updateFlightPanel("Cannot accept: Flight link disconnected");
      return;
    }
    socket.send(JSON.stringify({
      type:"cmd",
      protocol:DEV011_PROTOCOL,
      command_id:`accept-control-${Date.now()}`,
      name:"accept_control",
      handback_id:flightHandback.handback_id,
      client_mono_ns:clientMonoNs()
    }));
    updateFlightPanel("ACCEPT CONTROL sent…");
  });
  panel.querySelector("#omniFcEnable").addEventListener("click", enableFlightControl);
  panel.querySelector("#omniFcRelease").addEventListener("click", ()=>disableFlightControl("operator release"));
  panel.querySelector("#omniFcDisarm").addEventListener("click", ()=>{
    disableFlightControl("disarming");
    sendFlightCommand("disarm", {});
    updateFlightPanel("DISARM requested…");
  });
  updateFlightPanel();
}
function isTypingTarget(x) {
  return x instanceof HTMLInputElement || x instanceof HTMLTextAreaElement || (x instanceof HTMLElement && x.isContentEditable);
}
function onFlightKeyDown(e) {
  if (!flightControlEnabled || isTypingTarget(e.target)) return;
  let used = true;
  switch (e.code) {
    case "KeyW": flightAxes.throttle = Math.min(1, flightAxes.throttle + 0.05); break;
    case "KeyS": flightAxes.throttle = Math.max(0, flightAxes.throttle - 0.05); break;
    case "ArrowLeft": flightAxes.roll = -1; break;
    case "ArrowRight": flightAxes.roll = 1; break;
    case "ArrowUp": flightAxes.pitch = -1; break;
    case "ArrowDown": flightAxes.pitch = 1; break;
    case "KeyA": flightAxes.yaw = -1; break;
    case "KeyD": flightAxes.yaw = 1; break;
    case "KeyX":
      flightAxes.roll=0; flightAxes.pitch=0; flightAxes.yaw=0; flightAxes.throttle=0; break;
    default: used = false;
  }
  if (used) {
    e.preventDefault();
    updateFlightPanel();
    sendFlightAxes();
  }
}
function onFlightKeyUp(e) {
  if (!flightControlEnabled) return;
  if (e.code === "ArrowLeft" || e.code === "ArrowRight") flightAxes.roll = 0;
  if (e.code === "ArrowUp" || e.code === "ArrowDown") flightAxes.pitch = 0;
  if (e.code === "KeyA" || e.code === "KeyD") flightAxes.yaw = 0;
}
document.addEventListener("keydown", onFlightKeyDown, true);
document.addEventListener("keyup", onFlightKeyUp, true);
window.addEventListener("blur", ()=>disableFlightControl("window blur"));
document.addEventListener("visibilitychange", ()=>{
  if (document.visibilityState !== "visible") disableFlightControl("window hidden");
});
window.addEventListener("beforeunload", ()=>disableFlightControl("window closing"));


function clientMonoNs() { return Math.round(performance.now() * 1e6); }
function sendRemoteEvidence(action, data = {}, scope = null) {
  const cfg = remoteConfig();
  if (!cfg || cfg.error || !remoteAssignment || !socket || socket.readyState !== WebSocket.OPEN) return false;
  const msg = {type:"client_event", action, client_mono_ns:clientMonoNs(), client_utc_ms:Date.now(), data};
  if (scope) msg.scope = scope;
  socket.send(JSON.stringify(msg));
  return true;
}
function sendClockPing() {
  if (!socket || socket.readyState !== WebSocket.OPEN || !remoteAssignment) return;
  socket.send(JSON.stringify({type:"clock_ping", ping_id:`p-${Date.now()}-${Math.floor(Math.random()*1000000)}`, client_send_mono_ns:clientMonoNs(), client_utc_ms:Date.now()}));
}
function startClockSync() { clearInterval(clockTimer); sendClockPing(); clockTimer=setInterval(sendClockPing,10000); }
function installEvidenceHooks() {
  if (evidenceHooksInstalled) return;
  evidenceHooksInstalled=true;
  document.addEventListener("click", (e)=>{
    const x=e.target instanceof Element ? e.target : null;
    sendRemoteEvidence("ui_click", {target_id:x?.id||null,data_widget:x?.getAttribute("data-widget")||null,tag:x?.tagName||null,button:e.button}, "ui");
  }, true);
  document.addEventListener("keydown", (e)=>{
    const x=e.target;
    if (x instanceof HTMLInputElement || x instanceof HTMLTextAreaElement || (x instanceof HTMLElement && x.isContentEditable)) return;
    sendRemoteEvidence("key_down", {key:e.key,code:e.code,repeat:e.repeat}, "ui");
  }, true);
  document.addEventListener("visibilitychange", ()=>sendRemoteEvidence("visibility_changed", {visibility_state:document.visibilityState}, "ui"));
}


function remoteConfig() {
  const hash = window.location.hash.startsWith("#")
    ? window.location.hash.slice(1)
    : window.location.hash;
  const q = new URLSearchParams(hash);
  if (q.get("omni_remote") !== "1") return null;
  const server = q.get("server") || "";
  const studentId = q.get("student_id") || "";
  const token = q.get("token") || "";
  if (!server.startsWith("ws://") && !server.startsWith("wss://")) {
    return { error: "invalid_server" };
  }
  if (!studentId || !token) return { error: "missing_credentials" };
  return { server, studentId, token, stationId: q.get("station_id") || "", trainingRole: (q.get("training_role") || "").toUpperCase() };
}

function statusElement() {
  return document.getElementById("remoteStatusBadge");
}

function setRemoteStatus(text, state = "") {
  const el = statusElement();
  if (!el) return;
  el.textContent = text;
  el.dataset.state = state;
  el.title = text;
}

function busUrl(cfg) {
  if (cfg && !cfg.error) return cfg.server;
  const host = window.location.hostname || "127.0.0.1";
  const port = window.__OMNI_BUS_PORT__ || 8765;
  return `ws://${host}:${port}`;
}

function routingMatches(routing) {
  if (!remoteAssignment || !routing) return false;
  return (
    routing.student_id === remoteAssignment.student_id &&
    routing.session_id === remoteAssignment.session_id &&
    routing.attempt_id === remoteAssignment.attempt_id &&
    routing.aircraft_id === remoteAssignment.aircraft_id &&
    Number(routing.generation) === Number(remoteAssignment.generation)
  );
}

function handleMessage(event, cfg) {
  let msg;
  try {
    msg = JSON.parse(event.data);
  } catch (err) {
    console.error("[bus] payload bukan JSON:", err);
    return;
  }

  if (!cfg) {
    if (msg && msg.type === "telemetry" && msg.data) updateHUD(msg.data);
    return;
  }

  if (msg.protocol && msg.protocol !== DEV011_PROTOCOL) {
    setRemoteStatus("REMOTE: PROTOCOL ERROR", "error");
    return;
  }

  if (msg.type === "welcome" && msg.assignment) {
    remoteAssignment = msg.assignment;
    remoteEvidencePrincipal = msg.evidence_principal || null;
    window.__OMNI_REMOTE_ASSIGNMENT__ = remoteAssignment;
    window.__OMNI_REMOTE_EVIDENCE_PRINCIPAL__ = remoteEvidencePrincipal;
    setRemoteStatus(
      `REMOTE: ${remoteAssignment.student_id} / ${remoteAssignment.aircraft_id}`,
      "connected"
    );
    return;
  }

  if (msg.type === "authority" && msg.scope === "flight") {
    flightAuthority = msg;
    installFlightControlPanel();
    updateFlightPanel();
    return;
  }

  
  if (msg.type === "authority_handback") {
    flightHandback = msg.pending ? {
      pending:true,
      handback_id:msg.handback_id,
      expires_in_ms:msg.expires_in_ms
    } : null;
    updateFlightPanel(
      flightHandback
        ? `INSTRUCTOR OFFERS CONTROL • click ACCEPT CONTROL`
        : null
    );
    return;
  }

  if (msg.type === "authority_handback_result") {
    if (msg.status === "applied") {
      flightHandback = null;
      updateFlightPanel(`CONTROL ACCEPTED • epoch ${msg.epoch}`);
    } else {
      updateFlightPanel(
        `ACCEPT CONTROL rejected: ${msg.reason || "unknown"}`
      );
    }
    return;
  }

if (msg.type === "command_result") {
    if (msg.status === "rejected" || msg.status === "timeout") {
      updateFlightPanel(`${msg.name || "command"} ${msg.status}: ${msg.reason || "unknown"}`);
    } else if (msg.name === "arm" && msg.status === "applied") {
      updateFlightPanel("ARMED • click ENABLE CONTROL when ready");
    } else if (msg.name === "disarm" && msg.status === "applied") {
      updateFlightPanel("DISARMED");
    } else if (msg.name === "release_axes" && msg.status === "applied") {
      updateFlightPanel("CONTROL RELEASED");
    }
    return;
  }

  if (msg.type === "clock_pong") {
    socket.send(JSON.stringify({type:"clock_sample", ping_id:msg.ping_id, client_recv_mono_ns:clientMonoNs()}));
    return;
  }

  if (msg.type === "telemetry" && msg.data) {
    if (!remoteAssignment) return;
    if (!routingMatches(msg.routing)) {
      console.error("[remote] routing mismatch ignored:", msg.routing);
      setRemoteStatus("REMOTE: ROUTING ERROR", "error");
      return;
    }
    updateHUD(msg.data);
    // DEV016_RECEIVED_STATE_ACK
    try {
      const d = (msg.data) || {};
      sendBus({
        type: "client_event",
        action: "telemetry_received",
        scope: "observation",
        client_mono_ns: Math.round(performance.now() * 1000000),
        client_utc_ms: Date.now(),
        data: {
          seq: Number.isInteger(d.seq) ? d.seq : null,
          lat: d.lat, lon: d.lon, alt_msl: d.alt_msl,
          roll: d.roll, pitch: d.pitch, yaw: d.yaw,
        },
      });
    } catch (e) {
      console.warn("[evidence] received-state ack failed", e);
    }

    const nowMs = performance.now();
    if (nowMs - lastTelemetryAckMs >= 500) {
      lastTelemetryAckMs = nowMs;
      sendRemoteEvidence("telemetry_received", {seq:msg.seq??null,lat:msg.data.lat??null,lon:msg.data.lon??null,alt_msl:msg.data.alt_msl??null,roll:msg.data.roll??null,pitch:msg.data.pitch??null,yaw:msg.data.yaw??null}, "observation");
    }
    return;
  }

  if (msg.type === "event") {
    if (msg.routing && !routingMatches(msg.routing)) return;
    console.log("[remote event]", msg.name, msg);
    return;
  }

  if (msg.type === "error") {
    console.error("[remote] server error:", msg.code);
    setRemoteStatus(`REMOTE ERROR: ${msg.code || "unknown"}`, "error");
  }
}


// DEV016_CLOCK_SYNC_V2
// Independent clock listener; existing telemetry/control handler remains untouched.
let omniClockTimer = null;
let omniClockSeq = 0;
let omniClockReady = false;

function omniMonoNs() {
  return Math.round(performance.now() * 1000000);
}

function omniSendClockPing() {
  if (!socket || socket.readyState !== WebSocket.OPEN) return false;
  const pingId = `clock-${Date.now()}-${++omniClockSeq}`;
  return sendBus({
    type: "clock_ping",
    ping_id: pingId,
    client_send_mono_ns: omniMonoNs(),
  });
}

function omniStartClockSync() {
  omniClockReady = false;
  if (omniClockTimer !== null) {
    clearInterval(omniClockTimer);
    omniClockTimer = null;
  }
  omniSendClockPing();
  setTimeout(omniSendClockPing, 150);
  setTimeout(omniSendClockPing, 400);
  omniClockTimer = setInterval(omniSendClockPing, 2000);
}

function omniStopClockSync() {
  omniClockReady = false;
  if (omniClockTimer !== null) {
    clearInterval(omniClockTimer);
    omniClockTimer = null;
  }
}

function omniClockMessageListener(event) {
  let msg;
  try {
    msg = JSON.parse(event.data);
  } catch (_) {
    return;
  }

  if (msg && msg.type === "welcome") {
    omniStartClockSync();
    return;
  }

  // DEV016_CLOCK_SINGLE_OWNER
  // Existing Feather handler owns clock_pong -> clock_sample.
  // This listener only starts/refreshed clock_ping.
  if (msg && msg.type === "clock_pong") {
    return;
  }
}
export function connectBus() {
  const cfg = remoteConfig();
  if (cfg && cfg.error) {
    setRemoteStatus(`REMOTE CONFIG ERROR: ${cfg.error}`, "error");
    return null;
  }

  if (!cfg && window.__OMNI_PYQT_HOST__) {
    console.log("[bus] host PyQt lokal — bus pasif (anti double-feed).");
    return null;
  }

  manualClose = false;
  remoteAssignment = null;
  const url = busUrl(cfg);
  if (cfg) setRemoteStatus("REMOTE: CONNECTING", "connecting");
  socket = new WebSocket(url);

  // DEV016_CLOCK_SYNC_V2 socket hooks
  socket.addEventListener("message", omniClockMessageListener);
  socket.addEventListener("close", omniStopClockSync);

  socket.addEventListener("open", () => {
    if (cfg) {
      setRemoteStatus("REMOTE: AUTHENTICATING", "connecting");
      socket.send(JSON.stringify({
        type: "hello",
        protocol: DEV011_PROTOCOL,
        role: "student",
        student_id: cfg.studentId,
        token: cfg.token,
        station_id: cfg.stationId || undefined,
        training_role: cfg.trainingRole || undefined,
        client: { name: "Feather-Cesium", version: "DEV-011" }
      }));
    }
  });

  socket.addEventListener("message", (event) => handleMessage(event, cfg));
  socket.addEventListener("close", () => {
    remoteAssignment = null;
    if (cfg) setRemoteStatus("REMOTE: DISCONNECTED", "disconnected");
    if (manualClose) return;
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connectBus, 1000);
  });
  socket.addEventListener("error", () => {
    if (cfg) setRemoteStatus("REMOTE: CONNECTION ERROR", "error");
  });
  return socket;
}

export function disconnectBus() {
  manualClose = true;
  clearTimeout(reconnectTimer);
  if (socket) {
    socket.close();
    socket = null;
  }
}

export function sendBus(message) {
  const cfg = remoteConfig();
  if (cfg && message && message.type === "cmd") {
    console.warn("[remote] commands disabled in DEV-011");
    return false;
  }
  if (socket && socket.readyState === WebSocket.OPEN) {
    // DEV016_COMMAND_INTENT_SEND
    if (message && message.type === "cmd") {
      const clientMono = Number.isInteger(message.client_mono_ns)
        ? message.client_mono_ns
        : Math.round(performance.now() * 1000000);
      socket.send(JSON.stringify({
        type: "client_event",
        action: "command_intent",
        client_mono_ns: clientMono,
        client_utc_ms: Date.now(),
        data: {
          command_id: message.command_id,
          name: message.name,
          generation: message.generation,
          authority_epoch: message.authority_epoch,
          sequence: message.sequence,
          expiry_ms: message.expiry_ms,
          payload: message.payload,
        },
      }));
    }
    socket.send(JSON.stringify(message));
    return true;
  }
  return false;
}

export function getRemoteAssignment() {
  return remoteAssignment ? { ...remoteAssignment } : null;
}
