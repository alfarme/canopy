/** Shared photo path resolution (DB file_path → on-disk path under the mount). */

import path from "node:path";

export const PHOTO_BASE_PATH = process.env.PHOTO_BASE_PATH ?? "/data/photos";

/**
 * The DB stores the absolute path from the writer's mount view (the bot's
 * PHOTO_STORAGE_DIR or canopy's own PHOTO_BASE_PATH). If it isn't already
 * under PHOTO_BASE_PATH, remap: PHOTO_BASE_PATH/<YYYY-MM-DD>/<uuid>.jpg.
 */
export function resolvePhotoPath(filePath: string): string {
  if (filePath.startsWith(PHOTO_BASE_PATH)) {
    return filePath;
  }
  const parentDir = path.basename(path.dirname(filePath));
  const fileName = path.basename(filePath);
  return path.join(PHOTO_BASE_PATH, parentDir, fileName);
}

/** file_path like /dir/YYYY-MM-DD/<uuid4hex>.jpg → uuid (basename sans ext). */
export function photoUuidFromPath(filePath: string): string {
  const base = filePath.split("/").pop() ?? "";
  return base.replace(/\.[^.]+$/, "");
}
