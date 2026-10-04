import { state } from "../state.js";
import { setHomePosition } from "../core/home.js";
import { sampleTerrainHeight } from "../core/terrain.js";
import {
  missionAltitudeReference,
  resolveMissionAltitude
} from "./frames.js";

export function normalizeWaypoint(wp) {
  if (Array.isArray(wp)) {
    return {
      seq: Number(wp[3] ?? 0),
      command: null,
      frame: null,
      lat: Number(wp[0]),
      lon: Number(wp[1]),
      alt: Number(wp[2] ?? 0)
    };
  }

  const rawFrame = wp.frame ?? wp.mavFrame ?? null;

  return {
    seq: Number(wp.seq ?? wp.index ?? 0),
    command: wp.command ?? null,
    frame:
      rawFrame === null || rawFrame === undefined
        ? null
        : Number(rawFrame),
    lat: Number(wp.lat ?? wp.latitude ?? wp.y ?? 0),
    lon: Number(wp.lon ?? wp.lng ?? wp.longitude ?? wp.x ?? 0),
    alt: Number(wp.alt ?? wp.altitude ?? wp.z ?? 0)
  };
}

async function resolveWaypointAltitude(point, options) {
  const previewOnly = Boolean(options && options.previewOnly);
  const reference = missionAltitudeReference(point.frame);

  if (reference === "msl") {
    return resolveMissionAltitude(point.frame, point.alt);
  }

  if (reference === "home") {
    const homeAlt = state.telemetry.home_alt ?? state.telemetry.homeAlt ?? null;

    return resolveMissionAltitude(point.frame, point.alt, { homeAlt });
  }

  if (reference === "terrain") {
    const terrainAlt = await sampleTerrainHeight(point.lat, point.lon);

    return resolveMissionAltitude(point.frame, point.alt, { terrainAlt });
  }

  return resolveMissionAltitude(point.frame, point.alt, { previewOnly });
}

export async function setMissionPath(points, options = {}) {
  if (!state.viewer) {
    return {
      rendered: 0,
      unresolved: 0
    };
  }

  if (state.entities.mission) {
    state.viewer.entities.remove(state.entities.mission);
    state.entities.mission = null;
  }

  state.entities.waypoints.forEach((entity) => {
    state.viewer.entities.remove(entity);
  });

  state.entities.waypoints = [];

  if (!points || points.length === 0) {
    return {
      rendered: 0,
      unresolved: 0
    };
  }

  const normalized = points
    .map(normalizeWaypoint)
    .filter((p) => Number.isFinite(p.lat) && Number.isFinite(p.lon));

  if (normalized.length === 0) {
    return {
      rendered: 0,
      unresolved: 0
    };
  }

  normalized.sort((a, b) => a.seq - b.seq);

  const resolved = [];
  const unresolved = [];

  for (const point of normalized) {
    const altitude = await resolveWaypointAltitude(point, options);

    if (!Number.isFinite(altitude)) {
      unresolved.push(point);
      console.warn(
        "Mission waypoint altitude unresolved; not rendering point",
        {
          seq: point.seq,
          frame: point.frame,
          lat: point.lat,
          lon: point.lon,
          alt: point.alt
        }
      );
      continue;
    }

    resolved.push({
      ...point,
      alt: altitude
    });
  }

  if (resolved.length === 0) {
    return {
      rendered: 0,
      unresolved: unresolved.length
    };
  }

  const positions = resolved.map((p) =>
    Cesium.Cartesian3.fromDegrees(p.lon, p.lat, p.alt)
  );

  state.entities.mission = state.viewer.entities.add({
    polyline: {
      positions,
      width: 4,
      material: Cesium.Color.CYAN
    }
  });

  resolved.forEach((p, index) => {
    const position = Cesium.Cartesian3.fromDegrees(p.lon, p.lat, p.alt);

    const entity = state.viewer.entities.add({
      position,
      point: {
        pixelSize: index === 0 ? 12 : 8,
        color: index === 0 ? Cesium.Color.LIME : Cesium.Color.ORANGE,
        outlineColor: Cesium.Color.BLACK,
        outlineWidth: 2
      },
      label: {
        text: index === 0 ? "HOME" : `WP ${p.seq}`,
        font: "12px monospace",
        fillColor: Cesium.Color.WHITE,
        outlineColor: Cesium.Color.BLACK,
        outlineWidth: 2,
        style: Cesium.LabelStyle.FILL_AND_OUTLINE,
        pixelOffset: new Cesium.Cartesian2(0, -20)
      }
    });

    state.entities.waypoints.push(entity);
  });

  if (
    !Number.isFinite(state.home.lat) ||
    !Number.isFinite(state.home.lon)
  ) {
    const first = resolved[0];

    // Visual fallback only. Do not infer authoritative home MSL from a mission
    // item; relative-home conversion uses telemetry.home_alt above.
    setHomePosition(first.lat, first.lon, null, "mission").catch(() => {});
  }

  state.viewer.zoomTo(state.entities.mission).catch(() => {});

  return {
    rendered: resolved.length,
    unresolved: unresolved.length
  };
}
