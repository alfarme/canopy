import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { motion } from "motion/react";
import type { TilePayload } from "../api";
import { DuoTile } from "../components/DuoTile";
import { SkeletonTile, Tile, type Span } from "../components/Tile";
import { Topbar } from "../components/Topbar";
import { alertingPlants, groupByLocation, type LocationGroup } from "../derive";
import { usePlants } from "../hooks";
import { restoreScroll } from "../scroll";

function metaLine(plants: TilePayload[]): string {
  const n = plants.filter((p) => p.needsAttention).length;
  return (
    `${plants.length} plant${plants.length === 1 ? "" : "s"} · ` +
    (n ? `${n} need${n === 1 ? "s" : ""} attention` : "all readings in range")
  );
}

/** Layout plan: photo tiles alternate tall spans for editorial rhythm. All
 * corpus photos are portrait — the wide 2×1 span stays reserved for future
 * landscape photos (brief §5). */
function planSpans(rest: TilePayload[]): { plant: TilePayload; span: Span }[] {
  let photoIdx = 0;
  const planned = rest.map((plant) => ({
    plant,
    span: plant.photo && photoIdx++ % 2 === 0 ? "tall" : ("" as Span),
  }));
  // Tall tiles first (stable): dense grid flow backfills around them, so any
  // leftover empty cell lands at the end of the grid instead of mid-flow.
  return planned.sort((a, b) => Number(b.span === "tall") - Number(a.span === "tall"));
}

/** One location's grid. Groups large enough to carry it (≥4 plants with
 * ≥2 photos) open with the featured duo canvas; small groups stay a plain
 * rhythm grid so a two-plant room doesn't become one giant tile. */
function GroupGrid({ plants }: { plants: TilePayload[] }) {
  const withPhotos = plants.filter((p) => p.photo);
  const duo = plants.length >= 4 && withPhotos.length >= 2 ? withPhotos.slice(0, 2) : [];
  const duoIds = new Set(duo.map((p) => p.id));
  const rest = plants.filter((p) => !duoIds.has(p.id));
  return (
    <div className="grid">
      {duo.length === 2 && <DuoTile a={duo[0]} b={duo[1]} />}
      {planSpans(rest).map(({ plant, span }) => (
        <Tile key={plant.id} plant={plant} span={span} />
      ))}
    </div>
  );
}

const ALL_TAB = "All";

export default function Overview() {
  const { data: plants, isLoading, isError } = usePlants();
  // Mobile/tablet tab selection; desktop shows every group and ignores it.
  const [tab, setTab] = useState<string | null>(null);
  const [autoScroll, setAutoScroll] = useState(false);
  const scrollDir = useRef<1 | -1>(1);

  // Coming back from a detail view: jump to where the user left the grid.
  const ready = !!plants;
  useLayoutEffect(() => {
    if (ready) restoreScroll();
  }, [ready]);

  // Slow ambient pan, only offered on the "All" tab (the one view long
  // enough to be worth auto-scrolling). Any manual scroll/touch stops it —
  // it's a kiosk convenience, not something that should fight the viewer.
  useEffect(() => {
    if (!autoScroll) return;
    const SPEED = 0.025; // px/ms ≈ 25px/s — slow enough to read while it moves
    let raf = 0;
    let last = performance.now();
    // Tracked as our own float, not read back from window.scrollY: the
    // browser rounds scrollTo to whole pixels, and each frame only moves a
    // fraction of a pixel — reading scrollY back each frame would round that
    // fraction away and the scroll would never accumulate past 0.
    let y = window.scrollY;
    const step = (now: number) => {
      const dt = now - last;
      last = now;
      const maxY = document.documentElement.scrollHeight - window.innerHeight;
      if (maxY > 0) {
        y += scrollDir.current * SPEED * dt;
        if (y >= maxY) {
          y = maxY;
          scrollDir.current = -1;
        } else if (y <= 0) {
          y = 0;
          scrollDir.current = 1;
        }
        window.scrollTo(0, y);
      }
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    const stop = () => setAutoScroll(false);
    window.addEventListener("wheel", stop, { passive: true });
    window.addEventListener("touchstart", stop, { passive: true });
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("wheel", stop);
      window.removeEventListener("touchstart", stop);
    };
  }, [autoScroll]);

  let body;
  let meta;
  if (isLoading) {
    meta = "waking the sensors…";
    body = (
      <main className="grid">
        <SkeletonTile span="featured" />
        {Array.from({ length: 5 }, (_, i) => (
          <SkeletonTile key={i} span={i % 3 === 0 ? "tall" : ""} />
        ))}
      </main>
    );
  } else if (isError || !plants) {
    meta = "connection trouble";
    body = (
      <main className="grid">
        <div className="grid-empty">Couldn't reach the greenhouse — retrying…</div>
      </main>
    );
  } else if (plants.length === 0) {
    meta = "0 plants";
    body = (
      <main className="grid">
        <div className="grid-empty">
          No plants with active sensor assignments yet. Assign one via the Telegram
          bot's /identify flow and it will appear here.
        </div>
      </main>
    );
  } else {
    meta = metaLine(plants);
    const groups: LocationGroup[] = groupByLocation(plants);
    const active =
      tab && (tab === ALL_TAB || groups.some((g) => g.name === tab)) ? tab : groups[0]?.name;
    const alerts = alertingPlants(plants).length;
    body = (
      <main className={active === ALL_TAB ? "all-rooms" : undefined}>
        <div className="controls">
          <nav className="loc-tabs">
            <button
              className={`tab${active === ALL_TAB ? " active" : ""}`}
              onClick={() => setTab(ALL_TAB)}
            >
              {ALL_TAB}
            </button>
            {groups.map((g) => (
              <button
                key={g.name}
                className={`tab${g.name === active ? " active" : ""}`}
                onClick={() => {
                  setTab(g.name);
                  setAutoScroll(false);
                }}
              >
                {g.name}
              </button>
            ))}
          </nav>
          <div className="spacer" />
          {active === ALL_TAB && (
            <button
              className={`autoscroll-btn${autoScroll ? " active" : ""}`}
              onClick={() => setAutoScroll((a) => !a)}
              title="Slow ambient pan up and down"
            >
              {autoScroll ? "⏸ Auto-scroll" : "▾▴ Auto-scroll"}
            </button>
          )}
          <Link className={`alerts-btn${alerts ? " has-alerts" : ""}`} to="/alerts">
            Alerts{alerts ? ` · ${alerts}` : ""}
          </Link>
        </div>
        {groups.map((g) => (
          <section
            key={g.name}
            className={`loc-group${g.name === active || active === ALL_TAB ? " active" : ""}`}
          >
            <h2 className="loc-title">{g.name}</h2>
            <GroupGrid plants={g.plants} />
          </section>
        ))}
      </main>
    );
  }

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.2 }}
    >
      <Topbar meta={meta} />
      {body}
    </motion.div>
  );
}
