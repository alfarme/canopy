import { useState } from "react";
import { Link } from "react-router-dom";
import { motion } from "motion/react";
import { Tile } from "../components/Tile";
import { Topbar } from "../components/Topbar";
import { alertingPlants } from "../derive";
import { usePlants } from "../hooks";

const REFRESH_KEY = "canopy-ui-alert-refresh-min";
const REFRESH_CHOICES = [1, 2, 5, 10, 15, 30];

function storedRefresh(): number {
  const v = Number(localStorage.getItem(REFRESH_KEY));
  return REFRESH_CHOICES.includes(v) ? v : 5;
}

/** Alerts board: only out-of-range plants, most critical first (water
 * outranks everything — see derive.ts), frame color by severity tier.
 * Meant to be left open (wall tablet), hence the visible auto-refresh. */
export default function Alerts() {
  const [mins, setMins] = useState(storedRefresh);
  const { data: plants, isLoading, dataUpdatedAt } = usePlants(mins * 60_000);

  const alerts = alertingPlants(plants ?? []);
  const updated = dataUpdatedAt
    ? new Date(dataUpdatedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : null;
  const meta = isLoading
    ? "waking the sensors…"
    : `${alerts.length} alert${alerts.length === 1 ? "" : "s"}` +
      (updated ? ` · updated ${updated}` : "");

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.2 }}
    >
      <Topbar meta={meta} />
      <main>
        <div className="controls">
          <Link className="alerts-btn" to="/">
            ← all plants
          </Link>
          <div className="spacer" />
          <label className="refresh-picker">
            refresh every
            <select
              value={mins}
              onChange={(e) => {
                setMins(Number(e.target.value));
                localStorage.setItem(REFRESH_KEY, e.target.value);
              }}
            >
              {REFRESH_CHOICES.map((m) => (
                <option key={m} value={m}>
                  {m} min
                </option>
              ))}
            </select>
          </label>
        </div>
        {!isLoading && alerts.length === 0 ? (
          <div className="grid-empty">
            Nothing needs attention — every reading is in range. 🌿
          </div>
        ) : (
          <div className="grid alerts-grid">
            {alerts.map(({ plant, severity }) => (
              <Tile key={plant.id} plant={plant} sev={severity.level} />
            ))}
          </div>
        )}
      </main>
    </motion.div>
  );
}
