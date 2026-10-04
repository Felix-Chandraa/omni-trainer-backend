import { CONFIG } from "../config.js";
import { els } from "../dom.js";
import { state } from "../state.js";
import { fmtCoord, fmtNumber } from "../utils/format.js";

export function setLoading(visible, text) {
  if (typeof text === "string") {
    els.loadingText.textContent = text;
    els.terrainStatusText.textContent = text;
  }

  els.loadingOverlay.classList.toggle("hidden", !visible);
}

export function showToast(message, isError = false) {
  els.toast.textContent = message;
  els.toast.className = "toast show" + (isError ? " error" : "");

  clearTimeout(showToast._timer);

  showToast._timer = setTimeout(() => {
    els.toast.className = "toast";
  }, CONFIG.ui.toastDurationMs);
}

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

/* Bar level (fuel/battery): warna jujur per ambang. */
function setLevelBar(barEl, pct) {
  if (!barEl) {
    return;
  }
  if (!Number.isFinite(pct)) {
    barEl.style.width = "0%";
    barEl.className = "";
    return;
  }
  const clamped = Math.max(0, Math.min(100, pct));
  barEl.style.width = `${clamped}%`;
  barEl.className = clamped > 40 ? "lv-good" : clamped > 20 ? "lv-warn" : "lv-bad";
}

/**
 * Dedup UI v2: SATU metrik SATU rumah.
 * - Teks/status: panel kiri (mode, armed, posisi, altitude dual, GS, AS,
 *   sat, fuel, battery).
 * - Instrumen: panel kanan (ADI milik attitudeIndicator.js;
 *   HSI heading milik headingTape.js; tape milik altitudeTape.js).
 * Bottom-bar dihapus (seluruh isinya duplikat).
 */
export function updateTelemetryUI() {
  const t = state.telemetry;

  // Indikator sumber pose: trainee harus tahu sedang di kanal high-rate
  // (FG 30 Hz) atau fallback MAVLink (~5 Hz).
  if (els.poseBadge) {
    const fgActive = t.pose_source === "fg";
    els.poseBadge.textContent = fgActive
      ? "POSE: FG 30 Hz"
      : t.pose_source
        ? "POSE: MAVLINK"
        : "POSE: ---";
    els.poseBadge.classList.toggle("fg", fgActive);
  }

  els.modeValue.textContent = t.mode ?? "---";

  els.armedText.textContent =
    t.armed === true
      ? "ARMED"
      : t.armed === false
        ? "DISARMED"
        : "---";

  els.armedDot.style.background =
    t.armed === true
      ? "var(--good)"
      : t.armed === false
        ? "var(--bad)"
        : "var(--warn)";

  els.latLonValue.textContent = `${fmtCoord(t.lat)}, ${fmtCoord(t.lon)}`;
  els.satValue.textContent = t.sat ?? "---";

  // --- Altitude dual (doktrin hud lama dipertahankan utk baris AGL) ---
  const aglNum = optionalFiniteNumber(t.agl);
  const relAltNum = optionalFiniteNumber(t.alt);
  const fgAlt = t.pose_source === "fg" && aglNum !== null;
  const aglShown = fgAlt ? aglNum : relAltNum; // fallback REL MAVLink
  els.altAglLabel.textContent = fgAlt ? "AGL" : "REL";
  els.altAglValue.textContent =
    aglShown !== null ? `${fmtNumber(aglShown, 1)} m` : "---";

  const mslNum = optionalFiniteNumber(t.alt_msl);
  els.altMslValue.textContent = mslNum !== null
    ? `${fmtNumber(mslNum, 1)} m`
    : "---";

  els.gsValue.textContent =
    t.gs !== null && t.gs !== undefined ? `${fmtNumber(t.gs, 1)}` : "---";

  els.asValue.textContent =
    t.as !== null && t.as !== undefined ? `${fmtNumber(t.as, 1)}` : "---";

  // --- Fuel (propulsi bensin) & Avionics battery: pisah, label jujur ---
  const fuelPct = optionalFiniteNumber(t.fuel_pct);
  els.fuelValue.textContent = fuelPct !== null
    ? `${Math.round(fuelPct)}%`
    : "---";
  setLevelBar(els.fuelBar, fuelPct);

  const batPct = optionalFiniteNumber(t.bat_pct);
  els.batPctValue.textContent = batPct !== null
    ? `${Math.round(batPct)}%`
    : "---";
  setLevelBar(els.batBar, batPct);

  const batVolt = optionalFiniteNumber(t.bat);
  els.batVoltValue.textContent = batVolt !== null
    ? `${fmtNumber(batVolt, 1)} V`
    : "--- V";
}
