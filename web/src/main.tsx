import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import "@fontsource-variable/inter";
import "@fontsource/instrument-serif";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/layout.css";
import "./styles/photos.css";
import "./styles/people.css";
import "./styles/events.css";
import "./styles/viewer.css";
import "./styles/misc.css";
import "leaflet/dist/leaflet.css";

import App from "./App";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
