import { Route, Routes, useLocation } from "react-router-dom";
import { AnimatePresence } from "motion/react";
import Overview from "./pages/Overview";
import Alerts from "./pages/Alerts";
import Detail from "./pages/Detail";
import "./styles/app.css";

export default function App() {
  const location = useLocation();
  return (
    <AnimatePresence mode="popLayout" initial={false}>
      <Routes location={location} key={location.pathname}>
        <Route path="/" element={<Overview />} />
        <Route path="/alerts" element={<Alerts />} />
        <Route path="/plants/:id" element={<Detail />} />
      </Routes>
    </AnimatePresence>
  );
}
