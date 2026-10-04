import { validateMission } from "./validation.js";
import { setMissionPath } from "./mission.js";

export async function previewMission(points) {
  const result = validateMission(points);

  if (!result.valid) {
    throw new Error(result.errors.join("\n"));
  }

  await setMissionPath(points, { previewOnly: true });

  return {
    previewed: true,
    uploaded: false,
    count: points.length
  };
}
