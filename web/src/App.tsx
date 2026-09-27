import { Suspense, lazy, useEffect, useState } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { Sidebar } from "./components/Sidebar";
import { TopBar } from "./components/TopBar";
import { Spinner } from "./components/States";
import { ViewerProvider } from "./components/ViewerContext";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Shortcuts } from "./components/Shortcuts";
import { AuthGate } from "./components/AuthGate";
import { OfflineBanner } from "./components/OfflineBanner";

const Home = lazy(() => import("./pages/Home"));
const Photos = lazy(() => import("./pages/Photos"));
const People = lazy(() => import("./pages/People"));
const PersonDetail = lazy(() => import("./pages/PersonDetail"));
const UnassignedFaces = lazy(() => import("./pages/UnassignedFaces"));
const Events = lazy(() => import("./pages/Events"));
const Albums = lazy(() => import("./pages/Albums"));
const AlbumDetail = lazy(() => import("./pages/AlbumDetail"));
const EventDetail = lazy(() => import("./pages/EventDetail"));
const Places = lazy(() => import("./pages/Places"));
const PlaceDetail = lazy(() => import("./pages/PlaceDetail"));
const MapPage = lazy(() => import("./pages/MapPage"));
const Timeline = lazy(() => import("./pages/Timeline"));
const Duplicates = lazy(() => import("./pages/Duplicates"));
const SearchPage = lazy(() => import("./pages/SearchPage"));
const Settings = lazy(() => import("./pages/Settings"));
const Collections = lazy(() => import("./pages/Collections"));
const CollectionDetail = lazy(() => import("./pages/Collections").then((m) => ({ default: m.CollectionDetail })));
const Folders = lazy(() => import("./pages/Folders"));
const Insights = lazy(() => import("./pages/Insights"));
const Trash = lazy(() => import("./pages/Trash"));
const Upload = lazy(() => import("./pages/Upload"));
const Locked = lazy(() => import("./pages/Locked"));
const Private = lazy(() => import("./pages/Private"));
const Frame = lazy(() => import("./pages/Frame"));
const SharedAlbum = lazy(() => import("./pages/SharedAlbum"));

export default function App() {
  const location = useLocation();
  // A share link is public and has no app around it; the frame is full-screen but private.
  if (location.pathname.startsWith("/s/")) {
    return (
      <Suspense fallback={<Spinner label="Loading" full />}>
        <Routes><Route path="/s/:token" element={<SharedAlbum />} /></Routes>
      </Suspense>
    );
  }
  return (
    <AuthGate>
      {location.pathname === "/frame" ? (
        <Suspense fallback={<Spinner label="Loading" full />}><Frame /></Suspense>
      ) : <Shell />}
    </AuthGate>
  );
}

function Shell() {
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
          <OfflineBanner />
          <main className="content" data-scroll-root>
            <ErrorBoundary key={location.pathname}>
              <Suspense fallback={<Spinner label="Loading" full />}>
              <Routes>
                <Route path="/" element={<Home />} />
                <Route path="/photos" element={<Photos />} />
                <Route path="/people" element={<People />} />
                <Route path="/people/unassigned" element={<UnassignedFaces />} />
                <Route path="/people/:id" element={<PersonDetail />} />
                <Route path="/albums" element={<Albums />} />
                <Route path="/albums/:id" element={<AlbumDetail />} />
                <Route path="/collections" element={<Collections />} />
                <Route path="/collections/:key" element={<CollectionDetail />} />
                <Route path="/folders" element={<Folders />} />
                <Route path="/insights" element={<Insights />} />
                <Route path="/trash" element={<Trash />} />
                <Route path="/upload" element={<Upload />} />
                <Route path="/locked" element={<Locked />} />
                <Route path="/private" element={<Private />} />
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
