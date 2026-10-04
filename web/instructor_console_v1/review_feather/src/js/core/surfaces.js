import { state } from "../state.js";

/**
 * Animasi control surface + propeller (kontrak: SPEK_ANIMASI_CONTROL_SURFACE).
 *
 * Sumber data: SERVO_OUTPUT_RAW (PWM keluaran autopilot, sudah termasuk
 * efek SERVO*_REVERSED) + VFR_HUD.throttle — keduanya di bus telemetri.
 *
 * Node GLB (nama persis, hasil ukur Blender 14 Jul 2026):
 *   aileron_L / aileron_R : engsel sumbu X lokal
 *   ruddervator_L / _R    : engsel sumbu Y lokal (Empty)
 *   prop                  : poros sumbu Y lokal
 *   prop_disc             : tidak diputar; show saat throttle tinggi
 *
 * Mixing V-tail dilakukan DI SINI (model menyediakan dua panel independen):
 *   panel_L = elev*mixE + rud*mixR ; panel_R = elev*mixE - rud*mixR
 *
 * TANDA (+/-) tiap engsel belum diverifikasi visual — silakan uji dari
 * console: testSurface("aileron_L", 20), lalu kalau arah terbalik ubah
 * SURF.sign.aileron_L = -1 (live). Setelah pas, laporkan tabel tanda agar
 * dikunci sebagai default.
 */

// Konfigurasi live-editable dari console (window.SURF).
export const SURF = {
  maxDeg: { aileron: 60, ruddervator: 60 },
  axis: {
    aileron_L: "x",
    aileron_R: "x",
    ruddervator_L: "z",
    ruddervator_R: "z",
    prop: "z"
  },
  sign: {
    aileron_L: -1,
    aileron_R: -1,
    ruddervator_L: -1,
    ruddervator_R: -1,
    prop: 1
  },
  mixE: 0.5,          // porsi elevator di panel V
  mixR: 0.5,          // porsi rudder di panel V
  discThresholdPct: 15, // throttle >= ini: bilah -> disc blur
  rpmVisualFactor: 22   // deg per frame pada 100% throttle (murni visual)
};

const NODE_NAMES = [
  "aileron_L", "aileron_R",
  "ruddervator_L", "ruddervator_R",
  "prop", "prop_disc"
];

let model = null;        // Cesium.Model primitive di balik entity
let nodes = null;        // { name: { node, orig } }
let propAngleDeg = 0;
let lastTs = null;
let announced = false;
const testOverride = {}; // dari testSurface()

let renderHookInstalled = false;

function ensureSurfaceRenderHook() {
  if (renderHookInstalled || !state.viewer || !state.viewer.scene) {
    return;
  }
  renderHookInstalled = true;
  // DEV017_REV6_RENDER_HOOK
  state.viewer.scene.preRender.addEventListener(() => {
    updateControlSurfaces();
  });
}

function findModelPrimitive() {
  const ent = state.entities && state.entities.aircraft;
  if (!ent || !state.viewer || !state.viewer.scene) {
    return null;
  }

  // DEV017_REV7_MODEL_BINDING
  const scanCollection = (collection) => {
    if (!collection || typeof collection.length !== "number" ||
        typeof collection.get !== "function") {
      return null;
    }
    for (let i = 0; i < collection.length; i++) {
      const p = collection.get(i);
      if (!p) continue;

      const isModel =
        typeof Cesium.Model !== "undefined" && p instanceof Cesium.Model;
      if (isModel && (p.id === ent || p.id === ent.id)) {
        return p;
      }

      const nested = scanCollection(p);
      if (nested) return nested;
    }
    return null;
  };

  const direct = scanCollection(state.viewer.scene.primitives);
  if (direct) {
    return direct;
  }

  const displays = [
    state.viewer.dataSourceDisplay,
    state.viewer._dataSourceDisplay
  ].filter(Boolean);

  for (const dsd of displays) {
    const dataSources = [
      dsd.defaultDataSource,
      dsd._defaultDataSource
    ].filter(Boolean);

    for (const ds of dataSources) {
      const visualizers = ds._visualizers;
      if (!Array.isArray(visualizers)) continue;

      for (const v of visualizers) {
        const table = v && v._modelHash;
        const entry = table && table[ent.id];
        const primitive = entry && (
          entry.modelPrimitive ||
          entry.model ||
          entry.primitive
        );
        if (primitive) {
          return primitive;
        }
      }
    }
  }

  return null;
}

function acquireNodes() {
  // DEV017_REV7_STALE_MODEL_RESET
  if (typeof model !== "undefined" && model &&
      typeof model.isDestroyed === "function" && model.isDestroyed()) {
    model = null;
    if (typeof nodes !== "undefined") nodes = null;
    if (typeof announced !== "undefined") announced = false;
  }

  if (nodes) {
    return true;
  }

  if (model && typeof model.isDestroyed === "function" && model.isDestroyed()) {
    model = null;
    nodes = null;
    announced = false;
  }

  if (!model) {
    model = findModelPrimitive();
  }
  if (!model || !model.ready) {
    return false;
  }

  const found = {};
  const missing = [];

  for (const name of NODE_NAMES) {
    const node = model.getNode(name);
    if (node) {
      found[name] = {
        node,
        orig: Cesium.Matrix4.clone(node.originalMatrix || node.matrix)
      };
    } else {
      missing.push(name);
    }
  }

  nodes = found;

  if (!announced) {
    announced = true;
    console.log(
      `[surfaces] node ditemukan: ${Object.keys(found).join(", ") || "(nol)"}`
    );
    if (missing.length) {
      console.warn(
        `[surfaces] node TIDAK ada di GLB: ${missing.join(", ")} - ` +
        "periksa nama di Blender (grep gerbang)"
      );
    }
  }

  return true;
}

const scratchRot = new Cesium.Matrix3();
const scratchM = new Cesium.Matrix4();

function rotFor(axis, rad, result) {
  if (axis === "x") {
    return Cesium.Matrix3.fromRotationX(rad, result);
  }
  if (axis === "z") {
    return Cesium.Matrix3.fromRotationZ(rad, result);
  }
  return Cesium.Matrix3.fromRotationY(rad, result);
}

function setNodeAngle(name, deg) {
  const entry = nodes[name];
  if (!entry) {
    return;
  }

  const rad = Cesium.Math.toRadians(deg);
  rotFor(SURF.axis[name] || "y", rad, scratchRot);
  Cesium.Matrix4.clone(entry.orig, scratchM);
  Cesium.Matrix4.multiplyByMatrix3(scratchM, scratchRot, scratchM);
  entry.node.matrix = Cesium.Matrix4.clone(scratchM);
}

function setNodeShow(name, show) {
  const entry = nodes[name];
  if (entry && entry.node.show !== show) {
    entry.node.show = show;
  }
}

/** PWM 1000..2000 -> -1..1 (netral 1500). */
function norm(pwm) {
  const n = Number(pwm);
  if (!Number.isFinite(n) || n <= 0) {
    return 0;
  }
  return Math.max(-1, Math.min(1, (n - 1500) / 450));
}

let dev017RenderHookInstalled = false;

function ensureDev017SurfaceRenderHook() {
  if (dev017RenderHookInstalled || !state.viewer || !state.viewer.scene) {
    return;
  }
  dev017RenderHookInstalled = true;
  // DEV017_REV7_RENDER_HOOK
  state.viewer.scene.preRender.addEventListener(() => {
    updateControlSurfaces();
  });
}

export function updateControlSurfaces() {
  ensureDev017SurfaceRenderHook();

  ensureSurfaceRenderHook();
  if (!acquireNodes()) {
    return;
  }

  const t = state.telemetry;

  const ail = "aileron" in testOverride
    ? testOverride.aileron : norm(t.srv1);
  const elev = "elevator" in testOverride
    ? testOverride.elevator : norm(t.srv2);
  const rud = "rudder" in testOverride
    ? testOverride.rudder : norm(t.srv4);

  // DEV017_REV14_PROP_FROM_SERVO3
  // Use actual ArduPilot throttle servo output when available.
  // Current Omni contract: SERVO3_MIN=1000, SERVO3_MAX=2000.
  const servo3Pwm = Number(t.srv3);
  const servo3ThrottlePct = Number.isFinite(servo3Pwm) && servo3Pwm >= 900
    ? Math.max(0, Math.min(100, (servo3Pwm - 1000) / 10))
    : null;
  const throttle = "throttle" in testOverride
    ? testOverride.throttle
    : (
        servo3ThrottlePct !== null
          ? servo3ThrottlePct
          : (Number.isFinite(Number(t.throttle)) ? Number(t.throttle) : 0)
      );

  // Aileron: berlawanan arah satu sama lain (diferensial standar).
  setNodeAngle("aileron_L", ail * SURF.maxDeg.aileron * SURF.sign.aileron_L);
  setNodeAngle("aileron_R", -ail * SURF.maxDeg.aileron * SURF.sign.aileron_R);

  // V-tail mixing.
  const pL = (elev * SURF.mixE + rud * SURF.mixR) * SURF.maxDeg.ruddervator;
  const pR = (elev * SURF.mixE - rud * SURF.mixR) * SURF.maxDeg.ruddervator;
  setNodeAngle("ruddervator_L", pL * SURF.sign.ruddervator_L);
  setNodeAngle("ruddervator_R", pR * SURF.sign.ruddervator_R);

  // Propeller: putaran visual + swap ke disc blur saat throttle tinggi.
  const now = performance.now();
  const dt = lastTs === null ? 16 : Math.min(100, now - lastTs);
  lastTs = now;

  propAngleDeg =
    (propAngleDeg +
      (throttle / 100) * SURF.rpmVisualFactor * (dt / 16.7) * SURF.sign.prop) %
    360;
  setNodeAngle("prop", propAngleDeg);

  const discOn = throttle >= SURF.discThresholdPct;
  setNodeShow("prop", true);
  setNodeShow("prop_disc", discOn);
}



export function surfaceDebugSnapshot() {
  const ent = state.entities && state.entities.aircraft;
  const primitive = model || findModelPrimitive();
  const t = state.telemetry || {};
  return {
    entityId: ent ? ent.id : null,
    primitiveFound: Boolean(primitive),
    primitiveReady: Boolean(primitive && primitive.ready),
    nodesAcquired: nodes ? Object.keys(nodes) : [],
    srv1: t.srv1 ?? null,
    srv2: t.srv2 ?? null,
    srv3: t.srv3 ?? null,
    srv4: t.srv4 ?? null,
    throttle: t.throttle ?? null
  };
}

/* ================= Utilitas uji dari console ================= */

function testSurface(channel, deg) {
  // channel: "aileron" | "elevator" | "rudder" (deg -max..max, norm otomatis)
  const max = channel === "aileron"
    ? SURF.maxDeg.aileron : SURF.maxDeg.ruddervator;
  testOverride[channel] = Math.max(-1, Math.min(1, deg / max));
  console.log(`[surfaces] uji ${channel} = ${deg} deg (clearSurfaceTest() utk hapus)`);
}

function testThrottle(pct) {
  testOverride.throttle = Math.max(0, Math.min(100, pct));
  console.log(`[surfaces] uji throttle = ${pct}%`);
}

function clearSurfaceTest() {
  for (const k of Object.keys(testOverride)) {
    delete testOverride[k];
  }
  console.log("[surfaces] uji dihapus - kembali ke telemetri");
}

if (typeof window !== "undefined") {
  window.SURF = SURF;
  window.testSurface = testSurface;
  window.testThrottle = testThrottle;
  window.clearSurfaceTest = clearSurfaceTest;
}
