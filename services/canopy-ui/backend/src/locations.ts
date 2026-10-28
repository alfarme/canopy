/** The only valid room values for a sensor's location — mirrors the
 * frontend's LOCATION_GROUPS (derive.ts). Unlike the frontend, which fuzzy-
 * matches free text for display, writes are validated exactly against this
 * set: there is no "Elsewhere" to fall back into. */
export const LOCATION_GROUPS = ["Terrace", "Upstairs", "Living Room", "Kitchen"] as const;
export type Location = (typeof LOCATION_GROUPS)[number];
