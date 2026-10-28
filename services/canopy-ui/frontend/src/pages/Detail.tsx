import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { animate, motion, useMotionValue } from "motion/react";
import { usePinch } from "@use-gesture/react";
import { EditModal } from "../components/EditModal";
import { Leafmark } from "../components/Leafmark";
import { MetricRow } from "../components/MetricRow";
import { Topbar } from "../components/Topbar";
import { usePlant } from "../hooks";
import { locationLine, relativeTime } from "../format";

export default function Detail() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { data: plant, isLoading, isError } = usePlant(id);

  const [editing, setEditing] = useState(false);

  // Pinch-in on the hero to morph back to the grid (iOS-Photos style).
  // The hero shrinks under the fingers; past the threshold on release we
  // navigate (the shared layoutId animation takes over), otherwise spring back.
  const heroRef = useRef<HTMLDivElement>(null);
  const heroScale = useMotionValue(1);
  usePinch(
    ({ offset: [s], last }) => {
      const sc = Math.min(1.04, s);
      heroScale.set(sc);
      if (last) {
        if (sc < 0.8) navigate("/");
        else animate(heroScale, 1, { type: "spring", stiffness: 400, damping: 32 });
      }
    },
    {
      target: heroRef,
      eventOptions: { passive: false },
      scaleBounds: { min: 0.55, max: 1.15 },
      rubberband: true,
    }
  );
  // ESC closes the detail view like the back button; Overview restores the
  // grid scroll position stored on tile click (scroll.ts). Skipped while the
  // edit modal is open — its own ESC handler closes the modal instead.
  useEffect(() => {
    if (editing) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") navigate("/");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navigate, editing]);

  const onSaved = async () => {
    // Edits (photo, name, location, sensor, thresholds) all change what
    // detail + overview payloads show — same invalidation as a photo swap.
    await queryClient.invalidateQueries({ queryKey: ["plant", id] });
    await queryClient.invalidateQueries({ queryKey: ["plants"] });
  };

  let body;
  if (isLoading) {
    body = <div className="detail-status">Fetching the latest readings…</div>;
  } else if (isError || !plant) {
    body = (
      <div className="detail-status">
        This plant isn't assigned to a sensor anymore.
      </div>
    );
  } else {
    const hero = plant.photo?.urls.hero;
    body = (
      <div className="detail-wrap">
        <motion.div
          ref={heroRef}
          className={hero ? "detail" : "detail no-photo"}
          layoutId={`photo-${plant.id}`}
          style={{
            scale: heroScale,
            ...(hero
              ? {
                  backgroundImage: `url('${hero}')`,
                  backgroundColor: plant.photo!.dominantColor,
                }
              : {}),
          }}
        >
          {!hero && <Leafmark size={96} />}
          <button className="photo-btn" onClick={() => setEditing(true)}>
            ✎ Edit
          </button>
          <div className="caption">
            <div className="name">{plant.plantName}</div>
            {plant.species && <div className="species">{plant.species}</div>}
            <div className="loc">
              {locationLine(plant)} · updated {relativeTime(plant.lastSeen)}
            </div>
          </div>
          <button className="back-btn" onClick={() => navigate("/")}>
            ← All plants
          </button>
        </motion.div>
        <div className="panel">
          {plant.metrics.length === 0 ? (
            <div className="no-metrics">
              No readings yet — thresholds or sensor data are still missing for
              this plant.
            </div>
          ) : (
            plant.metrics.map((m) => (
              <MetricRow
                key={m.metricId}
                metric={m}
                history={plant.history.find((h) => h.metricId === m.metricId)}
              />
            ))
          )}
        </div>
      </div>
    );
  }

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.2 }}
    >
      <Topbar />
      {body}
      {editing && plant && (
        <EditModal plant={plant} onClose={() => setEditing(false)} onSaved={onSaved} />
      )}
    </motion.div>
  );
}
