/** Shared type definitions — exact shapes from ATLAS_SCHEMA.md */

export interface MetricReading {
  metricId: string;
  value: number;
  min: number;
  max: number;
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
  sensorName: string | null; // bare sensor name, e.g. "Sensor 03"
  battery: number | null; // percent; null when never read (GATT connects often fail)
  location: string | null; // room, e.g. "Living room"
  lastSeen: string;
  photo: TilePhoto | null;
  metrics: MetricReading[];
  needsAttention: boolean;
}

export interface PlantDetailPhoto {
  uuid: string;
  dominantColor: string;
  blurhash: string;
  urls: { tile: string; hero: string };
}

export interface HistoryPoint {
  t: string; // hour bucket, ISO
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
  photos: PlantDetailPhoto[];
  identifyConfidence: number | null;
  history: MetricHistory[];
  sensorId: number;
  rawThresholds: RawThresholds | null;
}
