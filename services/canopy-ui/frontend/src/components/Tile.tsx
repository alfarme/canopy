import type { CSSProperties } from "react";
import { Link } from "react-router-dom";
import { motion } from "motion/react";
import type { TilePayload } from "../api";
import { locationLine, relativeTime } from "../format";
import { saveScroll } from "../scroll";
import { Leafmark } from "./Leafmark";
import { MiniBar } from "./MiniBar";
import { StatusWord, tileMetrics } from "./statusWord";

export type Span = "" | "tall" | "wide" | "featured";

export function SkeletonTile({ span = "" }: { span?: Span }) {
  return (
    <div className={`tile skeleton ${span}`}>
      <div className="photo" />
    </div>
  );
}

export function Tile({
  plant,
  span = "",
  sev,
}: {
  plant: TilePayload;
  span?: Span;
  /** Alerts board: 3=red (water), 2=orange, 1=yellow — colors the frame. */
  sev?: 1 | 2 | 3;
}) {
  const attention = (plant.needsAttention ? " attention" : "") + (sev ? ` sev-${sev}` : "");
  const caption = (
    <div className="caption">
      <div className="name">{plant.plantName}</div>
      <div className="loc">
        {locationLine(plant)} · {relativeTime(plant.lastSeen)}
      </div>
      <div className="minis">
        {tileMetrics(plant.metrics).map((m) => (
          <MiniBar key={m.metricId} metric={m} />
        ))}
      </div>
      <div className="status-line">
        <StatusWord metrics={plant.metrics} />
      </div>
    </div>
  );

  if (!plant.photo) {
    return (
      <Link
        className={`tile no-photo ${span}${attention}`}
        to={`/plants/${plant.id}`}
        onClick={saveScroll}
      >
        <div className="photo" />
        <Leafmark />
        {caption}
      </Link>
    );
  }

  // Portrait crop for 1-col tiles; wide spans switch to the blur-fill hero
  // variant via --photo-w (a portrait cover-crop in a landscape box zooms
  // in far too much — brief §6).
  const style = {
    "--photo-p": `url('${plant.photo.urls.tile}')`,
    "--photo-w": `url('${plant.photo.urls.hero}')`,
    backgroundColor: plant.photo.dominantColor,
  } as CSSProperties;

  return (
    <Link className={`tile ${span}${attention}`} to={`/plants/${plant.id}`} onClick={saveScroll}>
      <motion.div className="photo" layoutId={`photo-${plant.id}`} style={style} />
      <div className="scrim" />
      {caption}
    </Link>
  );
}
