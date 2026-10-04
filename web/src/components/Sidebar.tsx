import { useEffect, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  BookImage, CalendarRange, Copy, Images, MapPin, PanelLeftClose, PanelLeft, Settings as SettingsIcon,
  Sparkles, Users, Clock, Map as MapIcon, LayoutGrid, FolderTree, ChartColumn, Trash2, Upload, Lock, CircleUser, EyeOff,
  Ellipsis, X,
} from "lucide-react";
import { api } from "../lib/api";

const NAV = [
  { to: "/", label: "Memories", icon: Sparkles, end: true },
  { to: "/photos", label: "Photos", icon: Images },
  { to: "/people", label: "People", icon: Users },
  { to: "/albums", label: "Albums", icon: BookImage },
  { to: "/collections", label: "Collections", icon: LayoutGrid },
  { to: "/events", label: "Events", icon: CalendarRange },
  { to: "/places", label: "Places", icon: MapPin },
  { to: "/map", label: "Map", icon: MapIcon },
  { to: "/timeline", label: "Timeline", icon: Clock },
  { to: "/folders", label: "Folders", icon: FolderTree },
  { to: "/insights", label: "Insights", icon: ChartColumn },
  { to: "/duplicates", label: "Duplicates", icon: Copy },
  { to: "/upload", label: "Upload", icon: Upload },
];

export function Sidebar({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  const { data: stats } = useQuery({ queryKey: ["stats"], queryFn: api.stats, refetchInterval: 60_000 });
  const { data: auth } = useQuery({ queryKey: ["auth"], queryFn: api.authStatus, staleTime: Infinity });
  const role = auth?.user?.role ?? "owner";
  const nav = NAV.filter((n) => !(n.to === "/upload" && role === "guest"));

  const counts: Record<string, number | undefined> = {
    "/photos": stats?.photos,
    "/people": stats?.people,
    "/events": stats ? stats.events + stats.trips : undefined,
    "/places": stats?.places,
    "/albums": stats?.albums,
    "/duplicates": stats?.duplicate_groups,
  };

  // The same destinations as the sidebar, for the phone's "More" sheet.
  const more: NavItem[] = [
    ...nav.filter((n) => !PHONE_TABS.includes(n.to)),
    ...(auth?.accounts && auth.user?.username && role !== "guest" ? [{ to: "/private", label: "Private", icon: EyeOff }] : []),
    ...(role === "owner" ? [{ to: "/locked", label: "Locked", icon: Lock }] : []),
    ...(role === "owner" && (stats?.trash ?? 0) > 0 ? [{ to: "/trash", label: "Trash", icon: Trash2 }] : []),
    { to: "/settings", label: "Settings", icon: SettingsIcon },
  ];

  return (
    <>
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
        {nav.map(({ to, label, icon: Icon, end }) => (
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
        {auth?.accounts && auth.user?.username && role !== "guest" && (
          <NavLink to="/private" className={({ isActive }) => `nav-link${isActive ? " is-active" : ""}`}
            title={collapsed ? "Private" : undefined}>
            <EyeOff size={18} strokeWidth={1.9} />
            <span className="nav-label">Private</span>
          </NavLink>
        )}
        {role === "owner" && (
          <NavLink to="/locked" className={({ isActive }) => `nav-link${isActive ? " is-active" : ""}`}
            title={collapsed ? "Locked folder" : undefined}>
            <Lock size={18} strokeWidth={1.9} />
            <span className="nav-label">Locked</span>
          </NavLink>
        )}
        {role === "owner" && (stats?.trash ?? 0) > 0 && (
          <NavLink to="/trash" className={({ isActive }) => `nav-link${isActive ? " is-active" : ""}`}
            title={collapsed ? "Trash" : undefined}>
            <Trash2 size={18} strokeWidth={1.9} />
            <span className="nav-label">Trash</span>
            <span className="nav-count tnum">{stats!.trash.toLocaleString()}</span>
          </NavLink>
        )}
        <NavLink to="/settings" className={({ isActive }) => `nav-link${isActive ? " is-active" : ""}`}
          title={collapsed ? "Settings" : undefined}>
          <SettingsIcon size={18} strokeWidth={1.9} />
          <span className="nav-label">Settings</span>
        </NavLink>
        {auth?.accounts && auth.user?.username && (
          <NavLink to="/settings" className="nav-link nav-user" title={collapsed ? auth.user.username : `Signed in as ${auth.user.username}`}>
            <CircleUser size={18} strokeWidth={1.9} />
            <span className="nav-label">{auth.user.username}</span>
          </NavLink>
        )}
        {stats && (
          <div className="nav-stat">
            <span className="tnum">{stats.photos.toLocaleString()}</span> photos
          </div>
        )}
      </div>
    </aside>
    <PhoneNav tabs={nav.filter((n) => PHONE_TABS.includes(n.to))} more={more} />
    </>
  );
}

/** The four places a phone goes most; everything else is under "More". */
const PHONE_TABS = ["/", "/photos", "/people", "/albums"];

type NavItem = { to: string; label: string; icon: React.ComponentType<{ size?: number; strokeWidth?: number }>; end?: boolean };

/** On a phone the left rail is hidden (see layout.css) and this bar sits under the page instead. */
function PhoneNav({ tabs, more }: { tabs: NavItem[]; more: NavItem[] }) {
  const [open, setOpen] = useState(false);
  const { pathname } = useLocation();
  useEffect(() => setOpen(false), [pathname]);
  return (
    <>
      {open && (
        <div className="tabbar-sheet-wrap" onClick={() => setOpen(false)}>
          <div className="tabbar-sheet" role="dialog" aria-label="More pages" onClick={(e) => e.stopPropagation()}>
            <div className="tabbar-sheet-head">
              <strong>More</strong>
              <button className="btn btn-quiet btn-icon btn-sm" onClick={() => setOpen(false)} aria-label="Close"><X size={16} /></button>
            </div>
            <div className="tabbar-sheet-grid">
              {more.map(({ to, label, icon: Icon }) => (
                <NavLink key={to} to={to} className={({ isActive }) => `tabbar-more-link${isActive ? " is-active" : ""}`}>
                  <Icon size={20} strokeWidth={1.9} />
                  <span>{label}</span>
                </NavLink>
              ))}
            </div>
          </div>
        </div>
      )}
      <nav className="tabbar" aria-label="Main">
        {tabs.map(({ to, label, icon: Icon, end }) => (
          <NavLink key={to} to={to} end={end} className={({ isActive }) => `tabbar-link${isActive ? " is-active" : ""}`}>
            <Icon size={20} strokeWidth={1.9} />
            <span>{label}</span>
          </NavLink>
        ))}
        <button className={`tabbar-link${open ? " is-active" : ""}`} onClick={() => setOpen((v) => !v)}
          aria-expanded={open} aria-label="More pages">
          <Ellipsis size={20} strokeWidth={1.9} />
          <span>More</span>
        </button>
      </nav>
    </>
  );
}
