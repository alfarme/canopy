/** Tile-scale metric bar: ideal band + reading dot on a 4px track. */

import { scaleLinear } from "d3-scale";
import type { MetricReading } from "../api";
import { oorRangeLabel, presentation } from "../format";

export function MiniBar({ metric }: { metric: MetricReading }) {
  const p = presentation(metric);
  const x = scaleLinear().domain([p.axisMin, p.axisMax]).range([0, 100]).clamp(true);
  return (
    <div className="mini">
      <span className="m-label">{p.short}</span>
      <span className="m-track">
        <span
          className="m-range"
          style={{ left: `${x(metric.min)}%`, right: `${100 - x(metric.max)}%` }}
        />
        <span
          className={metric.outOfRange ? "m-dot oor" : "m-dot"}
          style={{ left: `${x(metric.value)}%` }}
        />
      </span>
      {metric.outOfRange && <span className="m-word">{oorRangeLabel(metric)}</span>}
    </div>
  );
}
