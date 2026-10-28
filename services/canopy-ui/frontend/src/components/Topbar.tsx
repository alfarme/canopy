import { useState } from "react";
import { Link } from "react-router-dom";

function currentTheme(): string {
  return document.documentElement.dataset.theme ?? "light";
}

function currentZen(): boolean {
  return document.documentElement.dataset.ui === "zen";
}

export function Topbar({ meta }: { meta?: string }) {
  const [theme, setTheme] = useState(currentTheme);
  const [zen, setZen] = useState(currentZen);
  const toggle = () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("canopy-ui-theme", next);
    setTheme(next);
  };
  // "Zen"/full-screen: hides tabs, buttons and refresh controls via CSS
  // (html[data-ui="zen"]); the topbar itself stays, so the exit toggle is
  // always reachable.
  const toggleZen = () => {
    const next = !currentZen();
    document.documentElement.dataset.ui = next ? "zen" : "";
    localStorage.setItem("canopy-ui-zen", next ? "zen" : "");
    setZen(next);
  };
  return (
    <header className="topbar">
      <Link to="/" style={{ color: "inherit", textDecoration: "none" }}>
        <h1 className="wordmark">Canopy</h1>
      </Link>
      {meta && <div className="meta">{meta}</div>}
      <div className="spacer" />
      <button
        className="theme-toggle zen-toggle"
        title={zen ? "Show controls" : "Hide controls (kiosk view)"}
        onClick={toggleZen}
      >
        {zen ? "⛶ exit" : "⛶"}
      </button>
      <button className="theme-toggle" onClick={toggle}>
        Theme: {theme}
      </button>
    </header>
  );
}
