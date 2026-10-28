/**
 * Server-side metric registry.
 * Maps metricId -> threshold column names, bt_readings column, and unit.
 */

export interface MetricDef {
  thresholdMin: string;
  thresholdMax: string;
  btCol: string | null; // column in bt_readings; null = not available
  unit: string;
}

export const METRIC_REGISTRY: Record<string, MetricDef> = {
  moisture: {
    thresholdMin: "min_soil_moist",
    thresholdMax: "max_soil_moist",
    btCol: "moisture",
    unit: "%",
  },
  temperature: {
    thresholdMin: "min_temp",
    thresholdMax: "max_temp",
    btCol: "temp_c",
    unit: "°C",
  },
  light: {
    thresholdMin: "min_light_lux",
    thresholdMax: "max_light_lux",
    btCol: "light_lux",
    unit: "lx",
  },
  conductivity: {
    thresholdMin: "min_soil_ec",
    thresholdMax: "max_soil_ec",
    btCol: "conductivity_us_cm",
    unit: "µS/cm",
  },
};
