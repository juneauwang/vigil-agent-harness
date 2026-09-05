import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import "./index.css";
import "@/i18n";
import App from "./App";

// mount_spa injects __VIGIL_BASE_PATH__ behind reverse-proxy prefixes; empty string = root path.
const base = (typeof window !== "undefined" ? window.__VIGIL_BASE_PATH__ : "") ?? "";
const basename = base ? (base.startsWith("/") ? base : `/${base}`).replace(/\/+$/, "") : undefined;

createRoot(document.getElementById("root")!).render(
  <BrowserRouter basename={basename}>
    <App />
  </BrowserRouter>,
);
