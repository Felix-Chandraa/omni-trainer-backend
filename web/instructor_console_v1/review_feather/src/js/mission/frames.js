export const MAV_FRAME_GLOBAL = 0;
export const MAV_FRAME_GLOBAL_RELATIVE_ALT = 3;
export const MAV_FRAME_GLOBAL_INT = 5;
export const MAV_FRAME_GLOBAL_RELATIVE_ALT_INT = 6;
export const MAV_FRAME_GLOBAL_TERRAIN_ALT = 10;
export const MAV_FRAME_GLOBAL_TERRAIN_ALT_INT = 11;

const ABSOLUTE_MSL_FRAMES = new Set([
  MAV_FRAME_GLOBAL,
  MAV_FRAME_GLOBAL_INT
]);

const RELATIVE_HOME_FRAMES = new Set([
  MAV_FRAME_GLOBAL_RELATIVE_ALT,
  MAV_FRAME_GLOBAL_RELATIVE_ALT_INT
]);

const RELATIVE_TERRAIN_FRAMES = new Set([
  MAV_FRAME_GLOBAL_TERRAIN_ALT,
  MAV_FRAME_GLOBAL_TERRAIN_ALT_INT
]);

function optionalFiniteNumber(value) {
  if (value === null || value === undefined) {
    return null;
  }

  if (typeof value === "string" && value.trim() === "") {
    return null;
  }

  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export function missionAltitudeReference(frame) {
  const nFrame = optionalFiniteNumber(frame);

  if (nFrame === null) {
    return null;
  }

  if (ABSOLUTE_MSL_FRAMES.has(nFrame)) {
    return "msl";
  }

  if (RELATIVE_HOME_FRAMES.has(nFrame)) {
    return "home";
  }

  if (RELATIVE_TERRAIN_FRAMES.has(nFrame)) {
    return "terrain";
  }

  return null;
}

export function resolveMissionAltitude(
  frame,
  altitude,
  { homeAlt = null, terrainAlt = null, previewOnly = false } = {}
) {
  const alt = optionalFiniteNumber(altitude);

  if (alt === null) {
    return null;
  }

  const normalizedFrame = optionalFiniteNumber(frame);
  const reference = missionAltitudeReference(frame);

  // CR-005 local preview compatibility: data without a MAVLink frame may be
  // displayed locally, but it is explicitly not an authoritative upload or
  // frame conversion. The altitude is treated as display-only input.
  if (reference === null && previewOnly && normalizedFrame === null) {
    return alt;
  }

  if (reference === "msl") {
    return alt;
  }

  if (reference === "home") {
    const base = optionalFiniteNumber(homeAlt);
    return base !== null ? base + alt : null;
  }

  if (reference === "terrain") {
    const base = optionalFiniteNumber(terrainAlt);
    return base !== null ? base + alt : null;
  }

  return null;
}
