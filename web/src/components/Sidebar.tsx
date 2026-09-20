import { NavLink } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  CalendarRange, Copy, Images, MapPin, PanelLeftClose, PanelLeft, Settings as SettingsIcon,
  Sparkles, Users, Clock, Map as MapIcon,
} from "lucide-react";
import { api } from "../lib/api";

const NAV = [
  { to: "/", label: "Memories", icon: Sparkles, end: true },
  { to: "/photos", label: "Photos", icon: Images },
  { to: "/people", label: "People", icon: Users },
  { to: "/events", label: "Events", icon: CalendarRange },
  { to: "/places", label: "Places", icon: MapPin },
  { to: "/map", label: "Map", icon: MapIcon },
  { to: "/timeline", label: "Timeline", icon: Clock },
  { to: "/duplicates", label: "Duplicates", icon: Copy },
];

export function Sidebar({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  const { data: stats } = useQuery({ queryKey: ["stats"], queryFn: api.stats, refetchInterval: 60_000 });

  const counts: Record<string, number | undefined> = {
    "/photos": stats?.photos,
    "/people": stats?.people,
    "/events": stats ? stats.events + stats.trips : undefined,
    "/places": stats?.places,
    "/duplicates": stats?.duplicate_groups,
  };

  return (
    <aside className="nav">
      <div className="nav-head">
        <NavLink to="/" className="brand" aria-label="Memoria home">
          <span className="brand-mark" aria-hidden>
            <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8">
              <rect x="2.5" y="5" width="19" height="15" rx="3.5" />
              <circle cx="12" cy="12.5" r="4.2" />
              <path d="M8 5l1.4-2.2h5.2L16 5" />
            </svg>
          </span>
          <span className="brand-name display">Memoria</span>
        </NavLink>
        <button className="btn btn-quiet btn-icon btn-sm nav-toggle" onClick={onToggle}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"} title="Toggle sidebar">
          {collapsed ? <PanelLeft size={16} /> : <PanelLeftClose size={16} />}
        </button>
      </div>

      <nav className="nav-links">
        {NAV.map(({ to, label, icon: Icon, end }) => (
          <NavLink key={to} to={to} end={end} className={({ isActive }) => `nav-link${isActive ? " is-active" : ""}`}
            title={collapsed ? label : undefined}>
            <Icon size={18} strokeWidth={1.9} />
            <span className="nav-label">{label}</span>
            {counts[to] !== undefined && counts[to]! > 0 && (
              <span className="nav-count tnum">{counts[to]!.toLocaleString()}</span>
            )}
          </NavLink>
        ))}
      </nav>

      <div className="nav-foot">
        <NavLink to="/settings" className={({ isActive }) => `nav-link${isActive ? " is-active" : ""}`}
          title={collapsed ? "Settings" : undefined}>
          <SettingsIcon size={18} strokeWidth={1.9} />
          <span className="nav-label">Settings</span>
        </NavLink>
        {stats && (
          <div className="nav-stat">
            <span className="tnum">{stats.photos.toLocaleString()}</span> photos
          </div>
        )}
      </div>
    </aside>
  );
}
