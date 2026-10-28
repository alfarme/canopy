/** Panel-scale measurement primitive: value, ideal band + dot on an axis
 * track, min/ideal/max labels, optional 48h sparkline. d3-scale does the
 * math; the SVG is bespoke (brief §11). */

import { scaleLinear } from "d3-scale";
import type { MetricHistory, MetricReading } from "../api";
import { axisLabel, formatValue, oorWord, presentation, rangeLabel } from "../format";

function sparkPath(points: { t: string; v: number }[]): string {
  const ts = points.map((p) => Date.parse(p.t));
  const vs = points.map((p) => p.v);
  const x = scaleLinear().domain([Math.min(...ts), Math.max(...ts)]).range([0, 300]);
  const lo = Math.min(...vs);
  const hi = Math.max(...vs);
  const pad = (hi - lo) * 0.15 || 1;
  const y = scaleLinear().domain([lo - pad, hi + pad]).range([24, 2]);
  return points
    .map((p, i) => `${i === 0 ? "M" : "L"}${x(Date.parse(p.t)).toFixed(1)},${y(p.v).toFixed(1)}`)
    .join(" ");
}

export function MetricRow({
  metric,
  history,
}: {
  metric: MetricReading;
  history?: MetricHistory;
}) {
  const p = presentation(metric);
  const x = scaleLinear().domain([p.axisMin, p.axisMax]).range([0, 100]).clamp(true);
  const { value, unit } = formatValue(metric);
  const ideal = `ideal ${rangeLabel(metric)}`;

  return (
    <div className="m-row">
      <div className="m-head">
        <span className="m-name">{p.label}</span>
        <span className="m-val">
          {value}
          <small>{unit}</small>
        </span>
      </div>
      <div className="m-track">
        <div
          className="m-range"
          style={{ left: `${x(metric.min)}%`, right: `${100 - x(metric.max)}%` }}
        />
        <div
          className={metric.outOfRange ? "m-dot oor" : "m-dot"}
          style={{ left: `${x(metric.value)}%` }}
        />
      </div>
      <div className="m-minmax">
        <span>{axisLabel(p.axisMin)}</span>
        <span>
          {metric.outOfRange ? (
            <span className="oor-word">
              {oorWord(metric)} · {rangeLabel(metric)}
            </span>
          ) : (
            ideal
          )}
        </span>
        <span>{axisLabel(p.axisMax)}</span>
      </div>
      {history && history.points.length > 1 && (
        <svg className="m-spark" viewBox="0 0 300 26" preserveAspectRatio="none">
          <path d={sparkPath(history.points)} fill="none" strokeWidth="1.5" opacity="0.55" />
        </svg>
      )}
    </div>
  );
}
