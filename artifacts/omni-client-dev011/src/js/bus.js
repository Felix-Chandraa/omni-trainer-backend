import { updateHUD } from "./telemetry/telemetry.js";

const DEV011_PROTOCOL = "omni.training.v1";
let socket = null;
let reconnectTimer = null;
let manualClose = false;
let remoteAssignment = null;

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
  return { server, studentId, token };
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
    window.__OMNI_REMOTE_ASSIGNMENT__ = remoteAssignment;
    setRemoteStatus(
      `REMOTE: ${remoteAssignment.student_id} / ${remoteAssignment.aircraft_id}`,
      "connected"
    );
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

  socket.addEventListener("open", () => {
    if (cfg) {
      setRemoteStatus("REMOTE: AUTHENTICATING", "connecting");
      socket.send(JSON.stringify({
        type: "hello",
        protocol: DEV011_PROTOCOL,
        role: "student",
        student_id: cfg.studentId,
        token: cfg.token,
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
    socket.send(JSON.stringify(message));
    return true;
  }
  return false;
}

export function getRemoteAssignment() {
  return remoteAssignment ? { ...remoteAssignment } : null;
}
