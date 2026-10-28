/** Featured 2×2 tile hosting TWO portrait subjects on one shared blur canvas
 * (photo:dead-space ratio ≥ 3 rule, brief §5). On phones the wide canvas
 * would crop badly, so each half falls back to its own portrait crop (CSS). */

import type { CSSProperties } from "react";
import { Link } from "react-router-dom";
import type { TilePayload } from "../api";
import { locationLine, relativeTime } from "../format";
import { saveScroll } from "../scroll";
import { MiniBar } from "./MiniBar";
import { StatusWord, tileMetrics } from "./statusWord";

export function DuoTile({ a, b }: { a: TilePayload; b: TilePayload }) {
  const attention = a.needsAttention || b.needsAttention ? " attention" : "";
  const canvas = `/photos/duo/${a.photo!.uuid}/${b.photo!.uuid}`;
  const canvasStyle = {
    "--photo-p": `url('${canvas}')`,
    "--photo-w": `url('${canvas}')`,
    backgroundColor: a.photo!.dominantColor,
  } as CSSProperties;

  const half = (p: TilePayload) => (
    <Link
      key={p.id}
      className="half"
      to={`/plants/${p.id}`}
      onClick={saveScroll}
      style={{ "--photo-p": `url('${p.photo!.urls.tile}')` } as CSSProperties}
    >
      <div className="caption">
        <div className="name">{p.plantName}</div>
        <div className="loc">
          {locationLine(p)} · {relativeTime(p.lastSeen)}
        </div>
        <div className="minis">
          {tileMetrics(p.metrics).map((m) => (
            <MiniBar key={m.metricId} metric={m} />
          ))}
        </div>
        {/* phones hide the half's minis (too narrow) and fall back to this */}
        <div className="status-line">
          <StatusWord metrics={p.metrics} />
        </div>
      </div>
    </Link>
  );

  return (
    <div className={`tile featured duo${attention}`}>
      <div className="photo" style={canvasStyle} />
      <div className="halves">
        {half(a)}
        {half(b)}
      </div>
    </div>
  );
}
