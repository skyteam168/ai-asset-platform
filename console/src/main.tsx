import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { HashRouter } from "react-router-dom";
import { AppShell } from "./App";
import { AuthProvider } from "./auth";
import { PLATFORM_NAME } from "./brand";
import { lang } from "./i18n";
import { ThemeProvider } from "./theme";

document.title = PLATFORM_NAME;
document.documentElement.lang = lang();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
      <AuthProvider>
        <HashRouter>
          <AppShell />
        </HashRouter>
      </AuthProvider>
    </ThemeProvider>
  </StrictMode>,
);
