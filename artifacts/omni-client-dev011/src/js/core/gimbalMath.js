/**
 * Under-fuselage virtual turret in aircraft body coordinates.
 * Aircraft axes: +X forward, +Y left, +Z up. Positive UI pan = right.
 * Tilt: 0 forward, 90 straight down, 180 backward.
 * Do not apply visual GLB meshOffset/pitchOffset to the camera.
 */
export function normalizePanDeg(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 0;
  const wrapped = n % 360;
  return wrapped < 0 ? wrapped + 360 : wrapped;
}

export function clampTiltDeg(value, minDeg = 0, maxDeg = 180) {
  const n = Number(value);
  const lo = Number.isFinite(Number(minDeg)) ? Number(minDeg) : 0;
  const hi = Number.isFinite(Number(maxDeg)) ? Number(maxDeg) : 180;
  return Math.min(Math.max(Number.isFinite(n) ? n : 90, lo), hi);
}

export function gimbalBasis(panDeg, tiltDeg) {
  const pan = normalizePanDeg(panDeg) * Math.PI / 180;
  const tilt = clampTiltDeg(tiltDeg) * Math.PI / 180;
  const cp = Math.cos(pan), sp = Math.sin(pan);
  const ct = Math.cos(tilt), st = Math.sin(tilt);
  return {
    direction: { x: ct * cp, y: -ct * sp, z: -st },
    up: { x: st * cp, y: -st * sp, z: ct },
    right: { x: -sp, y: -cp, z: 0 }
  };
}
