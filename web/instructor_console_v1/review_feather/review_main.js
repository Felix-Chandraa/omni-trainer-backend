import { state } from "./src/js/state.js";
import { els } from "./src/js/dom.js";

import { initMap } from "./src/js/core/map.js";
import { updateHomeMarker } from "./src/js/core/home.js";
import { goHome, topView } from "./src/js/core/camera.js";
import {
  VIEWS,
  setTrainingView,
  getTrainingView,
  syncViewButtons
} from "./src/js/core/views.js";

import { updateHUD } from "./src/js/telemetry/telemetry.js";
import { loadProfile } from "./src/js/profile.js";
import { initSettings } from "./src/js/widgets/settings.js";
import { initLayoutMode } from "./src/js/widgets/layoutMode.js";
import { updateTelemetryUI, showToast } from "./src/js/widgets/hud.js";
import { loadLocation } from "./src/js/location.js";

const KEY_TO_VIEW = {
  "1": VIEWS.FPV,
  "2": VIEWS.TAIL,
  "3": VIEWS.FOLLOW,
  "4": VIEWS.GROUND,
  "5": VIEWS.FREE
};

let ready = false;
let pendingFrame = null;
let terrainPrimeTimer = null;

function bindCamera() {
  els.cameraToggleBtn?.addEventListener("click", () => {
    setTrainingView(
      getTrainingView() === VIEWS.FOLLOW ? VIEWS.FREE : VIEWS.FOLLOW
    );
  });
  els.homeBtn?.addEventListener("click", () => {
    setTrainingView(VIEWS.FREE, true);
    goHome();
  });
  els.topViewBtn?.addEventListener("click", () => {
    setTrainingView(VIEWS.FREE, true);
    topView();
  });
  els.viewButtons.forEach((btn) => {
    btn.addEventListener("click", () => setTrainingView(btn.dataset.view));
  });
  window.addEventListener("keydown", (event) => {
    const tag = event.target && event.target.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA") return;
    const view = KEY_TO_VIEW[event.key];
    if (view) setTrainingView(view);
  });
}

function toTelemetry(frame) {
  const p = frame?.pose || {};
  const a = frame?.actuator || {};
  const s = frame?.aircraft_state || {};
  const agl = Number(p.agl);
  const altMsl = Number(p.alt_msl);
  const relative = Number.isFinite(agl) ? Math.max(agl, 0) : 0;

  return {
    mode: s.mode || "REPLAY",
    armed: Boolean(s.armed),
    lat: p.lat,
    lon: p.lon,

    // Preserve the exact old Feather altitude contract:
    // pose_source=fg => AGL is authoritative visual height above terrain.
    alt_msl: Number.isFinite(altMsl) ? altMsl : null,
    agl: Number.isFinite(agl) ? agl : null,
    alt: relative,
    pose_source: "fg",

    gs: s.groundspeed ?? p.gs ?? null,
    as: s.airspeed ?? p.as ?? null,
    hdg: p.hdg ?? p.yaw ?? 0,
    yaw: p.yaw ?? p.hdg ?? 0,
    roll: p.roll ?? 0,
    pitch: p.pitch ?? 0,

    srv1: a.srv1 ?? null,
    srv2: a.srv2 ?? null,
    srv3: a.srv3 ?? null,
    srv4: a.srv4 ?? null,
    throttle: a.throttle ?? null
  };
}

function onMessage(event) {
  if (event.source !== window.parent) return;
  if (event.origin !== window.location.origin) return;

  const msg = event.data || {};
  if (msg.type === "omni-review-frame" && msg.frame && ready) {
    // Preserve normal playback flow. Before Feather terrain is ready, keep
    // only the newest historical frame. Once terrain is ready, apply it.
    if (!state.terrainReady) {
      pendingFrame = msg.frame;
    } else {
      pendingFrame = null;
      updateHUD(toTelemetry(msg.frame));
    }
    return;
  }

  if (msg.type !== "omni-review-command" || !ready) return;

  if (msg.command === "free") {
    setTrainingView(VIEWS.FREE, true);
  } else if (msg.command === "follow") {
    setTrainingView(VIEWS.FOLLOW, true);
  } else if (msg.command === "top") {
    setTrainingView(VIEWS.FREE, true);
    topView();
  } else if (msg.command === "recenter") {
    setTrainingView(VIEWS.FOLLOW, true);
  } else if (msg.command === "fpv") {
    setTrainingView(VIEWS.FPV, true);
  } else if (msg.command === "tail") {
    setTrainingView(VIEWS.TAIL, true);
  } else if (msg.command === "ground") {
    setTrainingView(VIEWS.GROUND, true);
  } else if (msg.command === "home") {
    setTrainingView(VIEWS.FREE, true);
    goHome();
  }
}

async function boot() {
  bindCamera();

  // This order is intentionally identical to the proven Feather main.js
  // through map initialization. No live bus/MAVLink connection is started.
  await loadProfile();
  initSettings();
  initLayoutMode();
  updateTelemetryUI();
  await loadLocation();
  await initMap();

  syncViewButtons();
  updateHomeMarker();

  // Keep the proven Feather behavior: announce Review ready immediately
  // after initMap() so PLAY/seek frames continue to flow normally.
  ready = true;

  // Separately prime the visual placement when original Feather terrain
  // becomes ready. This never pauses or blocks playback.
  terrainPrimeTimer = window.setInterval(() => {
    if (!state.terrainReady) return;
    window.clearInterval(terrainPrimeTimer);
    terrainPrimeTimer = null;

    if (pendingFrame) {
      const frame = pendingFrame;
      pendingFrame = null;
      updateHUD(toTelemetry(frame));
    }
  }, 50);

  window.__omniReviewLegacyParity = {
    ready: true,
    transport: "parent-postMessage",
    legacyBusLoaded: false,
    mavlinkLoaded: false,
    terrainPath: "original-feather"
  };

  window.parent.postMessage(
    { type: "omni-review-feather-ready" },
    window.location.origin
  );
}

window.addEventListener("message", onMessage);
window.addEventListener("error", (event) => {
  console.error("[review] viewer error:", event.error || event.message);
  showToast("Review viewer error: " + (event.message || "unknown"), true);
});
window.addEventListener("unhandledrejection", (event) => {
  console.error("[review] promise error:", event.reason);
  const message = event.reason?.message || String(event.reason || "unknown");
  showToast("Review viewer promise failed: " + message, true);
});

boot().catch((error) => {
  console.error("[review] boot failed:", error);
  showToast("Review viewer failed: " + (error?.message || error), true);
});
