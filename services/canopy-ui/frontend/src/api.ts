/** API types (mirror backend/src/types.ts) and fetch helpers. */

export interface MetricReading {
  metricId: string;
  value: number;
  min: number; // ideal range lower bound
  max: number; // ideal range upper bound
  unit: string;
  outOfRange: boolean;
}

export interface TilePhoto {
  uuid: string;
  dominantColor: string;
  blurhash: string;
  urls: { tile: string; hero: string };
}

export interface TilePayload {
  id: string;
  plantId: number;
  plantName: string;
  species: string | null;
  sensorLabel: string;
  sensorName: string | null;
  battery: number | null;
  location: string | null;
  lastSeen: string;
  photo: TilePhoto | null;
  metrics: MetricReading[];
  needsAttention: boolean;
}

export interface HistoryPoint {
  t: string;
  v: number;
}

export interface MetricHistory {
  metricId: string;
  points: HistoryPoint[];
}

export interface RawThresholds {
  min_soil_moist: number | null;
  max_soil_moist: number | null;
  min_soil_ec: number | null;
  max_soil_ec: number | null;
  min_light_lux: number | null;
  max_light_lux: number | null;
  min_temp: number | null;
  max_temp: number | null;
}

export interface PlantDetail extends TilePayload {
  photos: TilePhoto[];
  identifyConfidence: number | null;
  history: MetricHistory[];
  sensorId: number;
  rawThresholds: RawThresholds | null;
}

export interface SensorOption {
  id: number;
  name: string;
  label: string;
  assignedPlantId: number | null;
  assignedPlantName: string | null;
}

export interface ThresholdsInput {
  minSoilMoist?: number | null;
  maxSoilMoist?: number | null;
  minSoilEc?: number | null;
  maxSoilEc?: number | null;
  minLightLux?: number | null;
  maxLightLux?: number | null;
  minTemp?: number | null;
  maxTemp?: number | null;
  hysteresisMoist?: number | null;
}

export interface EditPlantInput {
  displayName?: string;
  location?: string;
  sensorId?: number;
  thresholds?: ThresholdsInput;
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

export async function fetchPlants(): Promise<TilePayload[]> {
  const data = await json<{ plants: TilePayload[] }>(await fetch("/api/plants"));
  return data.plants;
}

export function fetchPlant(id: string): Promise<PlantDetail> {
  return fetch(`/api/plants/${id}`).then((r) => json<PlantDetail>(r));
}

export async function uploadPhoto(plantId: number, file: File | Blob): Promise<void> {
  const form = new FormData();
  form.append("file", file, "photo.jpg");
  const res = await fetch(`/api/plants/${plantId}/photo`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.error ?? `Upload failed (${res.status})`);
  }
}

export async function fetchSensors(): Promise<SensorOption[]> {
  const data = await json<{ sensors: SensorOption[] }>(await fetch("/api/sensors"));
  return data.sensors;
}

export async function editPlant(assignmentId: string, input: EditPlantInput): Promise<void> {
  const res = await fetch(`/api/plants/${assignmentId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.error ?? `Update failed (${res.status})`);
  }
}
