import { CONFIG } from "../config.js";
import { flattenHeight, updateGeoidOffset } from "./runway.js";
import { updateControlSurfaces, surfaceDebugSnapshot } from "./surfaces.js";
import { state } from "../state.js";

export function createAircraftEntities() {
  const m = CONFIG.aircraftModel;
  state.entities.aircraft = state.viewer.entities.add({
    position: Cesium.Cartesian3.fromDegrees(0, 0, 1000),
    model: {
      uri: m.uri ?? CONFIG.aircraftModelUri,
      scale: m.scale,
      minimumPixelSize: m.minimumPixelSize,
      maximumScale: m.maximumScale,
      runAnimations: false
      // TANPA heightReference: posisi entity absolut (terrain+AGL dihitung
      // sinkron di updateAircraftOnMap). RELATIVE_TO_GROUND meng-clamp
      // ASINKRON -> model tertinggal dari kamera (rasa "pegas") dan tinggi
      // render tidak konsisten dengan perhitungan kita.
    }
  });

  state.entities.fallbackAircraft = state.viewer.entities.add({
    position: Cesium.Cartesian3.fromDegrees(0, 0, 1000),
    point: {
      pixelSize: 12,
      color: Cesium.Color.YELLOW,
      outlineColor: Cesium.Color.BLACK,
      outlineWidth: 2,
      heightReference: Cesium.HeightReference.CLAMP_TO_GROUND
    },
    label: {
      text: "AIRCRAFT",
      font: "14px sans-serif",
      fillColor: Cesium.Color.WHITE,
      outlineColor: Cesium.Color.BLACK,
      outlineWidth: 2,
      style: Cesium.LabelStyle.FILL_AND_OUTLINE,
      pixelOffset: new Cesium.Cartesian2(0, -24),
      heightReference: Cesium.HeightReference.CLAMP_TO_GROUND
    }
  });

  state.entities.fallbackAircraft.show = false;
}

/**
 * Fallback tinggi visual saat tile terrain BELUM termuat.
 *
 * BUG P5.5 (diperbaiki): dulu memakai alt_msl MENTAH. SITL memakai MSL
 * (EGM96, spawn 349 m) sedangkan Cesium memakai DEM-nya sendiri (357 m di
 * titik yang sama) -> pesawat spawn melayang / terkubur ~8 m.
 *
 * Aturan: posisi visual TIDAK PERNAH digerakkan MSL mentah. Urutan sumber:
 *   1. slab runway (bila sudah terukur)  -> paling akurat di lapangan
 *   2. MSL + offset geoid (bila terukur) -> di luar area runway
 *   3. MSL apa adanya                    -> upaya terakhir
 */
function fallbackHeight(mslAlt, agl) {
  const slab = state.runway && state.runway.elevM;
  if (Number.isFinite(slab)) {
    return slab + (Number.isFinite(agl) ? agl : 0);
  }

  const off = state.geoid && state.geoid.offsetM;
  if (Number.isFinite(off) && Number.isFinite(mslAlt)) {
    return mslAlt + off;
  }

  return mslAlt;
}

function updateRendererDiagnostic(t, groundH, heightAboveGround, visualHeight) {
  // DEV017_REV6_RENDER_DIAGNOSTIC
  let box = document.getElementById("omni-render-diagnostic");
  if (!box) {
    box = document.createElement("pre");
    box.id = "omni-render-diagnostic";
    Object.assign(box.style, {
      position: "fixed",
      right: "8px",
      bottom: "8px",
      zIndex: "999999",
      margin: "0",
      padding: "7px 9px",
      maxWidth: "390px",
      background: "rgba(0,0,0,0.72)",
      color: "#8fffa0",
      font: "11px/1.35 monospace",
      pointerEvents: "none",
      whiteSpace: "pre-wrap"
    });
    document.body.appendChild(box);
  }

  const s = surfaceDebugSnapshot();
  const clearance = CONFIG.aircraftModel.groundClearanceMeters;
  const mo = CONFIG.aircraftModel.meshOffsetMeters || {};
  box.textContent =
    "OMNI RENDER DEV-017\n" +
    `source=${t.pose_source} armed=${t.armed}\n` +
    `alt=${Number(t.alt).toFixed(3)} agl=${Number(t.agl).toFixed(3)} ` +
      `msl=${Number(t.alt_msl).toFixed(3)}\n` +
    `ground=${Number(groundH).toFixed(3)} hAGL=${Number(heightAboveGround).toFixed(3)}\n` +
    `visual=${Number(visualHeight).toFixed(3)} clearance=${Number(clearance).toFixed(3)}\n` +
    `meshOffset up=${Number(mo.up || 0).toFixed(3)}\n` +
    `model=${s.primitiveFound ? "FOUND" : "MISSING"} ` +
      `ready=${s.primitiveReady ? "YES" : "NO"} ` +
      `nodes=${s.nodesAcquired.length}/6\n` +
    `srv=${s.srv1},${s.srv2},${s.srv3},${s.srv4} thr=${s.throttle}`;
}


/*
 * DEV017_REV8_VERTICAL_DATUM
 *
 * FG/JSBSim state uses the simulator field MSL datum (~349 m at
 * Wiriadinata), while the current Cesium/runway visual surface is around
 * 359 m. This correction is RENDER-ONLY: telemetry/evidence/physics remain
 * untouched.
 *
 * The offset is latched only near the known initial field datum while the
 * aircraft is unarmed. Once latched, the same constant vertical translation
 * is retained throughout flight; it does NOT follow terrain elevation.
 */
const DEV017_SIM_FIELD_MSL_M = 349.0;
let dev017VisualDatumOffsetM = null; // REV9: legacy REV8 latch disabled

function dev017ResolveVisualHeight(t, groundH, legacyHeightAboveGround) {
  // DEV017_REV10_SAFE_HELPER
  // Compatibility-only helper. REV8 datum latching is permanently disabled.
  dev017VisualDatumOffsetM = null;

  const clearance = Number(
    CONFIG.aircraftModel.groundClearanceMeters || 0
  );
  const ground = Number(groundH);
  const agl = Number(legacyHeightAboveGround);

  if (Number.isFinite(ground) && Number.isFinite(agl)) {
    return ground + agl + clearance;
  }

  return Number(t.alt_msl);
}

function dev017RendererDiagnostic(t, groundH, heightAboveGround, visualHeight) {
  // DEV017_REV7_RENDER_DIAGNOSTIC
  let box = document.getElementById("omni-render-diagnostic");
  if (!box) {
    box = document.createElement("pre");
    box.id = "omni-render-diagnostic";
    Object.assign(box.style, {
      position: "fixed",
      right: "8px",
      bottom: "8px",
      zIndex: "999999",
      margin: "0",
      padding: "7px 9px",
      background: "rgba(0,0,0,0.75)",
      color: "#8fffa0",
      font: "11px/1.35 monospace",
      pointerEvents: "none",
      whiteSpace: "pre-wrap"
    });
    document.body.appendChild(box);
  }

  const s = surfaceDebugSnapshot();
  const clearance = Number(CONFIG.aircraftModel.groundClearanceMeters || 0);
  box.textContent =
    "OMNI RENDER DEV-017 rev7\n" +
    `source=${t.pose_source ?? "?"} armed=${t.armed ?? "?"}\n` +
    `alt=${Number(t.alt).toFixed(3)} agl=${Number(t.agl).toFixed(3)} ` +
      `msl=${Number(t.alt_msl).toFixed(3)}\n` +
    `ground=${Number(groundH).toFixed(3)} ` +
      `hAGL=${Number(heightAboveGround).toFixed(3)}\n` +
    `visual=${Number(visualHeight).toFixed(3)} ` +
      `clearance=${clearance.toFixed(3)}\n` +
    `model=${s.primitiveFound ? "FOUND" : "MISSING"} ` +
      `ready=${s.primitiveReady ? "YES" : "NO"} ` +
      `nodes=${s.nodesAcquired.length}/6\n` +
    `srv=${s.srv1},${s.srv2},${s.srv3},${s.srv4} ` +
      `thr=${s.throttle}`;
}

// DEV017_REV11_RENDER_AGL
// JSBSim/FG raw AGL in this setup is referenced to zero-elevation terrain,
// so it can equal MSL altitude.  The actual simulator field datum is 349.0 m.
// This constant is RENDER-ONLY; authoritative telemetry/evidence is untouched.
const DEV017_RENDER_FIELD_MSL_M = 349.0;

function dev017ResolveRenderAgl(t, relAlt) {
  const rawAgl = Number(t.agl);
  const altMsl = Number(t.alt_msl);
  const fgLike =
    t.pose_source === "fg" || t.pose_source === "replay_fg";

  // Detect the known bad FG convention: raw AGL approximately equals MSL.
  if (
    fgLike &&
    Number.isFinite(altMsl) &&
    (!Number.isFinite(rawAgl) || Math.abs(rawAgl - altMsl) < 5.0)
  ) {
    return Math.max(altMsl - DEV017_RENDER_FIELD_MSL_M, 0);
  }

  if (Number.isFinite(rawAgl)) {
    return Math.max(rawAgl, 0);
  }

  return Math.max(Number(relAlt) || 0, 0);
}

export function updateAircraftOnMap() {
  if (
    !state.viewer ||
    state.telemetry.lat === null ||
    state.telemetry.lon === null
  ) {
    return;
  }

  if (!state.entities.aircraft || !state.entities.fallbackAircraft) {
    return;
  }

  const t = state.telemetry;

  const lat = Number(t.lat);
  const lon = Number(t.lon);

  if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
    return;
  }

  const relAlt = Math.max(Number(t.alt ?? 0), 0);
  const mslAlt = Number(t.alt_msl ?? relAlt);

  // Kanal vertikal: saat pose_source == "fg", pakai AGL dari FG stream
  // (30 Hz). t.alt = relative alt MAVLink (5 Hz) -> hanya fallback.
  const agl = Number(t.agl);
  const heightAboveGround = dev017ResolveRenderAgl(t, relAlt);

  // POSISI TUNGGAL (sumber kebenaran satu): tinggi terrain (sinkron via
  // globe.getHeight) + AGL + clearance. Entity ditempatkan ABSOLUT di sini
  // (tanpa heightReference) dan kamera memakai Cartesian yang SAMA —
  // model & kamera rigid lockstep, bebas lag clamp asinkron.
  // P5.5: tinggi MENTAH dari mesh, lalu diratakan bila di area runway
  // (fungsi murni, dipakai bersama jalur async di core/terrain.js).
  const rawGroundH = state.viewer.scene.globe.getHeight(
    Cesium.Cartographic.fromDegrees(lon, lat)
  );
  const groundH = flattenHeight(rawGroundH, lon, lat);

  // Kalibrasi offset geoid: BERPENJAGA di dalam updateGeoidOffset (hanya saat
  // di darat, diam, pose FG, hasil masuk akal). Bug P5.5b: menerima telemetri
  // sampah pasca-crash -> offset -10783 m.
  if (Number.isFinite(rawGroundH)) {
    updateGeoidOffset(rawGroundH, t);
  }
  const visualHeight = Number.isFinite(groundH)
    ? groundH + heightAboveGround + CONFIG.aircraftModel.groundClearanceMeters
    : fallbackHeight(mslAlt, heightAboveGround) +
      CONFIG.aircraftModel.groundClearanceMeters; // tile belum termuat
  const visualPosition = Cesium.Cartesian3.fromDegrees(lon, lat, visualHeight);

  dev017RendererDiagnostic(t, groundH, heightAboveGround, visualHeight);

  updateRendererDiagnostic(t, groundH, heightAboveGround, visualHeight);
  state.lastPosition = visualPosition;

  const hpr = new Cesium.HeadingPitchRoll(
    Cesium.Math.toRadians((t.hdg ?? 0) + CONFIG.enuHeadingOffsetDeg),
    Cesium.Math.toRadians(t.pitch ?? 0),
    Cesium.Math.toRadians(t.roll ?? 0)
  );

  // Kompensasi origin glb: geser ENTITY berlawanan arah offset mesh,
  // dihitung di frame badan pesawat (ikut berotasi -> orbit hilang).
  // Kamera/trail TIDAK ikut digeser: visualPosition = posisi pesawat sejati.
  const mo = CONFIG.aircraftModel.meshOffsetMeters;
  let entityPosition = visualPosition;
  if (mo && (mo.forward || mo.right || mo.up)) {
    const frame = Cesium.Transforms.headingPitchRollToFixedFrame(
      visualPosition,
      hpr
    );
    entityPosition = Cesium.Matrix4.multiplyByPoint(
      frame,
      new Cesium.Cartesian3(-mo.forward, mo.right, -mo.up),
      new Cesium.Cartesian3()
    );
  }

  state.entities.aircraft.show = true;
  updateControlSurfaces();
  state.entities.fallbackAircraft.show = false;
  state.entities.aircraft.position = entityPosition;

  // Kalibrasi sumbu model (C3): komposisi quaternion di body frame.
  // Penjumlahan Euler mentah SALAH utk offset pitch/roll saat manuver.
  // Kamera FPV/tail tetap pakai pose mentah — fix ini murni kosmetik model.
  const poseQuat = Cesium.Transforms.headingPitchRollQuaternion(
    entityPosition,
    hpr
  );
  const mc = CONFIG.aircraftModel;
  if (mc.headingOffsetDeg || mc.pitchOffsetDeg || mc.rollOffsetDeg) {
    const fix = Cesium.Quaternion.fromHeadingPitchRoll(
      new Cesium.HeadingPitchRoll(
        Cesium.Math.toRadians(mc.headingOffsetDeg),
        Cesium.Math.toRadians(mc.pitchOffsetDeg),
        Cesium.Math.toRadians(mc.rollOffsetDeg)
      )
    );
    state.entities.aircraft.orientation = Cesium.Quaternion.multiply(
      poseQuat,
      fix,
      new Cesium.Quaternion()
    );
  } else {
    state.entities.aircraft.orientation = poseQuat;
  }

  state.lastCameraPose = {
    position: visualPosition,
    headingDeg: t.hdg ?? 0,
    pitchDeg: t.pitch ?? 0,
    rollDeg: t.roll ?? 0
  };
}

export function setVehiclePosition(lat, lon, alt, heading, roll, pitch) {
  if (lat === null || lon === null || alt === null) {
    return;
  }

  if (!state.viewer) {
    return;
  }

  state.telemetry.lat = Number(lat);
  state.telemetry.lon = Number(lon);
  state.telemetry.alt = Number(alt);
  state.telemetry.hdg = heading ?? state.telemetry.hdg ?? 0;
  state.telemetry.roll = roll ?? state.telemetry.roll ?? 0;
  state.telemetry.pitch = pitch ?? state.telemetry.pitch ?? 0;
  state.telemetry.yaw = heading ?? state.telemetry.yaw ?? state.telemetry.hdg ?? 0;

  updateAircraftOnMap();
}
