import { createRoot } from "react-dom/client";

import App from "@/App";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/500.css";
import "@/index.css";

createRoot(document.getElementById('root')!).render(
  <App />,
);
