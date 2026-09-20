import { Suspense, lazy, useEffect, useState } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { Sidebar } from "./components/Sidebar";
import { TopBar } from "./components/TopBar";
import { Spinner } from "./components/States";
import { ViewerProvider } from "./components/ViewerContext";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Shortcuts } from "./components/Shortcuts";

const Home = lazy(() => import("./pages/Home"));
const Photos = lazy(() => import("./pages/Photos"));
const People = lazy(() => import("./pages/People"));
const PersonDetail = lazy(() => import("./pages/PersonDetail"));
const UnassignedFaces = lazy(() => import("./pages/UnassignedFaces"));
const Events = lazy(() => import("./pages/Events"));
const EventDetail = lazy(() => import("./pages/EventDetail"));
const Places = lazy(() => import("./pages/Places"));
const PlaceDetail = lazy(() => import("./pages/PlaceDetail"));
const MapPage = lazy(() => import("./pages/MapPage"));
const Timeline = lazy(() => import("./pages/Timeline"));
const Duplicates = lazy(() => import("./pages/Duplicates"));
const SearchPage = lazy(() => import("./pages/SearchPage"));
const Settings = lazy(() => import("./pages/Settings"));

export default function App() {
  const location = useLocation();
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("nav-collapsed") === "1");

  useEffect(() => {
    localStorage.setItem("nav-collapsed", collapsed ? "1" : "0");
  }, [collapsed]);

  useEffect(() => {
    const root = document.querySelector("[data-scroll-root]");
    root?.scrollTo({ top: 0 });
  }, [location.pathname]);

  return (
    <ViewerProvider>
      <div className={`shell${collapsed ? " is-collapsed" : ""}`}>
        <Sidebar collapsed={collapsed} onToggle={() => setCollapsed((c) => !c)} />
        <div className="main">
          <TopBar />
          <main className="content" data-scroll-root>
            <ErrorBoundary key={location.pathname}>
              <Suspense fallback={<Spinner label="Loading" full />}>
              <Routes>
                <Route path="/" element={<Home />} />
                <Route path="/photos" element={<Photos />} />
                <Route path="/people" element={<People />} />
                <Route path="/people/unassigned" element={<UnassignedFaces />} />
                <Route path="/people/:id" element={<PersonDetail />} />
                <Route path="/events" element={<Events />} />
                <Route path="/events/:id" element={<EventDetail />} />
                <Route path="/places" element={<Places />} />
                <Route path="/places/:id" element={<PlaceDetail />} />
                <Route path="/map" element={<MapPage />} />
                <Route path="/timeline" element={<Timeline />} />
                <Route path="/duplicates" element={<Duplicates />} />
                <Route path="/search" element={<SearchPage />} />
                <Route path="/settings" element={<Settings />} />
                <Route path="*" element={<Navigate to="/" replace />} />
              </Routes>
              </Suspense>
            </ErrorBoundary>
          </main>
        </div>
        <Shortcuts />
      </div>
    </ViewerProvider>
  );
}
