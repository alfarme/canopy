import { useEffect, useRef, useState } from "react";
import { editPlant, fetchSensors, uploadPhoto } from "../api";
import type { PlantDetail, SensorOption } from "../api";
import { canonicalLocation, FALLBACK_GROUP, LOCATION_GROUPS } from "../derive";
import { PhotoEditor } from "./PhotoEditor";

interface ThresholdField {
  key: "moisture" | "temperature" | "light" | "conductivity";
  label: string;
  minCol: "min_soil_moist" | "min_temp" | "min_light_lux" | "min_soil_ec";
  maxCol: "max_soil_moist" | "max_temp" | "max_light_lux" | "max_soil_ec";
  minKey: "minSoilMoist" | "minTemp" | "minLightLux" | "minSoilEc";
  maxKey: "maxSoilMoist" | "maxTemp" | "maxLightLux" | "maxSoilEc";
}

const THRESHOLD_FIELDS: ThresholdField[] = [
  { key: "moisture", label: "Soil moisture (%)", minCol: "min_soil_moist", maxCol: "max_soil_moist", minKey: "minSoilMoist", maxKey: "maxSoilMoist" },
  { key: "temperature", label: "Temperature (°C)", minCol: "min_temp", maxCol: "max_temp", minKey: "minTemp", maxKey: "maxTemp" },
  { key: "light", label: "Light (lx)", minCol: "min_light_lux", maxCol: "max_light_lux", minKey: "minLightLux", maxKey: "maxLightLux" },
  { key: "conductivity", label: "Conductivity (µS/cm)", minCol: "min_soil_ec", maxCol: "max_soil_ec", minKey: "minSoilEc", maxKey: "maxSoilEc" },
];

type ThreshState = Record<string, string>; // "moisture-min" -> string input value

function initThresholds(plant: PlantDetail): ThreshState {
  const state: ThreshState = {};
  for (const f of THRESHOLD_FIELDS) {
    const min = plant.rawThresholds?.[f.minCol];
    const max = plant.rawThresholds?.[f.maxCol];
    state[`${f.key}-min`] = min == null ? "" : String(min);
    state[`${f.key}-max`] = max == null ? "" : String(max);
  }
  return state;
}

interface Props {
  plant: PlantDetail;
  onClose: () => void;
  onSaved: () => void;
}

/** The room dropdown only ever holds one of the 4 fixed values (or "" for
 * unset) — the raw sensor.location text can be a typo/free-text variant
 * ("terrac", lowercase, etc.), so default it through the same fuzzy match
 * the overview grid uses to bucket tiles, instead of comparing it verbatim
 * against the option values (which silently fell through to "none"). */
function initialLocation(plant: PlantDetail): string {
  if (!plant.location) return "";
  const canon = canonicalLocation(plant.location);
  return canon === FALLBACK_GROUP ? "" : canon;
}

export function EditModal({ plant, onClose, onSaved }: Props) {
  const [name, setName] = useState(plant.plantName);
  const initialLoc = useRef(initialLocation(plant)).current;
  const [location, setLocation] = useState(initialLoc);
  const [sensorId, setSensorId] = useState(plant.sensorId);
  const [sensors, setSensors] = useState<SensorOption[] | null>(null);
  const initialThresholds = useRef(initThresholds(plant)).current;
  const [thresholds, setThresholds] = useState<ThreshState>(initialThresholds);
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [pendingPhoto, setPendingPhoto] = useState<Blob | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchSensors()
      .then(setSensors)
      .catch(() => setSensors([]));
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function save() {
    setSaving(true);
    setError(null);
    try {
      if (pendingPhoto) {
        await uploadPhoto(plant.plantId, pendingPhoto);
      }

      const thresholdsChanged = THRESHOLD_FIELDS.some(
        (f) =>
          thresholds[`${f.key}-min`] !== initialThresholds[`${f.key}-min`] ||
          thresholds[`${f.key}-max`] !== initialThresholds[`${f.key}-max`]
      );
      let thresholdsInput: Record<string, number | null> | undefined;
      if (thresholdsChanged) {
        thresholdsInput = {};
        for (const f of THRESHOLD_FIELDS) {
          const minRaw = thresholds[`${f.key}-min`].trim();
          const maxRaw = thresholds[`${f.key}-max`].trim();
          thresholdsInput[f.minKey] = minRaw === "" ? null : Number(minRaw);
          thresholdsInput[f.maxKey] = maxRaw === "" ? null : Number(maxRaw);
        }
      }

      await editPlant(plant.id, {
        displayName: name.trim() !== plant.plantName ? name.trim() : undefined,
        location: location && location !== initialLoc ? location : undefined,
        sensorId: sensorId !== plant.sensorId ? sensorId : undefined,
        thresholds: thresholdsInput,
      });

      onSaved();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  const photoPreviewUrl = pendingPhoto
    ? URL.createObjectURL(pendingPhoto)
    : plant.photo?.urls.tile;

  return (
    <div className="modal-veil" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>Edit plant</h2>
          <button type="button" className="modal-close" onClick={onClose}>
            ×
          </button>
        </div>

        {pendingFile ? (
          <PhotoEditor
            file={pendingFile}
            onCancel={() => setPendingFile(null)}
            onSave={(blob) => {
              setPendingPhoto(blob);
              setPendingFile(null);
            }}
          />
        ) : (
          <div className="modal-body">
            <div className="edit-field edit-photo">
              {photoPreviewUrl && <img src={photoPreviewUrl} alt="" />}
              <label className="edit-photo-btn">
                {pendingPhoto ? "Photo staged — choose again" : "Change photo"}
                <input
                  type="file"
                  accept="image/jpeg"
                  hidden
                  onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) setPendingFile(f);
                    e.target.value = "";
                  }}
                />
              </label>
            </div>

            <div className="edit-field">
              <label>Name</label>
              <input type="text" value={name} onChange={(e) => setName(e.target.value)} />
            </div>

            <div className="edit-field">
              <label>Location</label>
              <select value={location} onChange={(e) => setLocation(e.target.value)}>
                <option value="">— none —</option>
                {LOCATION_GROUPS.map((g) => (
                  <option key={g} value={g}>
                    {g}
                  </option>
                ))}
              </select>
            </div>

            <div className="edit-field">
              <label>Sensor</label>
              {sensors === null ? (
                <div className="edit-hint">Loading sensors…</div>
              ) : (
                <select value={sensorId} onChange={(e) => setSensorId(Number(e.target.value))}>
                  {sensors.map((s) => (
                    <option
                      key={s.id}
                      value={s.id}
                      disabled={s.assignedPlantId != null && s.assignedPlantId !== plant.plantId}
                    >
                      {s.label}
                      {s.assignedPlantId != null && s.assignedPlantId !== plant.plantId
                        ? ` — assigned to ${s.assignedPlantName}`
                        : ""}
                    </option>
                  ))}
                </select>
              )}
            </div>

            <div className="edit-field edit-thresholds">
              <label>Thresholds</label>
              {THRESHOLD_FIELDS.map((f) => (
                <div className="edit-threshold-row" key={f.key}>
                  <span>{f.label}</span>
                  <input
                    type="number"
                    placeholder="min"
                    value={thresholds[`${f.key}-min`]}
                    onChange={(e) =>
                      setThresholds((t) => ({ ...t, [`${f.key}-min`]: e.target.value }))
                    }
                  />
                  <input
                    type="number"
                    placeholder="max"
                    value={thresholds[`${f.key}-max`]}
                    onChange={(e) =>
                      setThresholds((t) => ({ ...t, [`${f.key}-max`]: e.target.value }))
                    }
                  />
                </div>
              ))}
            </div>

            {error && <div className="edit-error">{error}</div>}

            <div className="modal-actions">
              <button type="button" className="secondary" onClick={onClose} disabled={saving}>
                Cancel
              </button>
              <button type="button" className="primary" onClick={save} disabled={saving}>
                {saving ? "Saving…" : "Save changes"}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
