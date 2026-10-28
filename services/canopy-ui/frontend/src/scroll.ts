/** Overview scroll restoration: a tile click stores the grid's scroll
 * offset; returning (back button, ESC, pinch-close) restores it once the
 * plant data — and therefore the grid's full height — is back. Grid rows
 * are fixed-height, so layout is stable before images finish loading. */

const KEY = "canopy-ui-scroll";

export function saveScroll(): void {
  sessionStorage.setItem(KEY, String(window.scrollY));
}

export function restoreScroll(): void {
  const stored = sessionStorage.getItem(KEY);
  if (stored === null) return;
  sessionStorage.removeItem(KEY);
  const y = Number(stored);
  // During the route transition the document height fluctuates (popLayout
  // pops views out of flow; the shared-photo morph collapses the grid for
  // a beat near its end), and any short moment clamps the scroll position.
  // So don't just set it once — pin it until the animations settle.
  const deadline = performance.now() + 1200;
  const pin = () => {
    if (Math.abs(window.scrollY - y) > 1) window.scrollTo(0, y);
    if (performance.now() < deadline) requestAnimationFrame(pin);
  };
  pin();
}
