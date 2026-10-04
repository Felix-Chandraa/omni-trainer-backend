const MISSION_UPLOAD_UNAVAILABLE =
  "Mission upload is not implemented in the legacy client. " +
  "Use previewMission() for local visualization; real upload must use the " +
  "authoritative backend mission transaction when available.";

export async function uploadMission(_points) {
  throw new Error(MISSION_UPLOAD_UNAVAILABLE);
}
