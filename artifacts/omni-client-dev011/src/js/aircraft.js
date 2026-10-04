import { CONFIG } from "./config.js";
import { state } from "./state.js";

export function createAircraftEntities() {
  state.entities.aircraft = state.viewer.entities.add({
    position: Cesium.Cartesian3.fromDegrees(0, 0, 1000),
    model: {
      uri: CONFIG.aircraftModelUri,
      scale: 3.0,
      minimumPixelSize: 48,
      maximumScale: 250,
      runAnimations: false,
      heightReference: Cesium.HeightReference.RELATIVE_TO_GROUND
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
  const aircraftPosition = Cesium.Cartesian3.fromDegrees(
    lon,
    lat,
    relAlt + CONFIG.aircraftAltitudeOffset
  );

  state.lastPosition = aircraftPosition;

  state.entities.aircraft.show = true;
  state.entities.fallbackAircraft.show = false;
  state.entities.aircraft.position = aircraftPosition;

  const hpr = new Cesium.HeadingPitchRoll(
    Cesium.Math.toRadians((t.hdg ?? 0) - 90),
    Cesium.Math.toRadians(t.pitch ?? 0),
    Cesium.Math.toRadians(t.roll ?? 0)
  );

  state.entities.aircraft.orientation =
    Cesium.Transforms.headingPitchRollQuaternion(aircraftPosition, hpr);

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
