import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import "./styles/global.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchInterval: 60_000,
    },
  },
});

// Theme: explicit choice (localStorage) wins, otherwise follow the OS.
const stored = localStorage.getItem("canopy-ui-theme");
const theme =
  stored ??
  (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
document.documentElement.dataset.theme = theme;
// Zen (kiosk) mode survives reloads — a wall-mounted tablet shouldn't
// regrow its controls after a refresh.
if (localStorage.getItem("canopy-ui-zen") === "zen") {
  document.documentElement.dataset.ui = "zen";
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>
);
