import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import "./index.css";
import App from "./App";

// mount_spa 在反向代理前缀场景注入 __HERMES_BASE_PATH__；空字符串 = 根路径。
const base = (typeof window !== "undefined" ? window.__HERMES_BASE_PATH__ : "") ?? "";
const basename = base ? (base.startsWith("/") ? base : `/${base}`).replace(/\/+$/, "") : undefined;

createRoot(document.getElementById("root")!).render(
  <BrowserRouter basename={basename}>
    <App />
  </BrowserRouter>,
);
