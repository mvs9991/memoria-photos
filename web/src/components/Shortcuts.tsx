import { useEffect, useState } from "react";
import { Keyboard, X } from "lucide-react";

const GROUPS: { title: string; keys: [string, string][] }[] = [
  {
    title: "Anywhere",
    keys: [
      ["/  or  Ctrl K", "Search"],
      ["?", "This list"],
      ["g then p", "Photos"],
      ["g then e", "Events"],
      ["g then u", "People"],
      ["g then t", "Timeline"],
    ],
  },
  {
    title: "Photo viewer",
    keys: [
      ["← →", "Previous / next photo"],
      ["i", "Show or hide details"],
      ["f", "Favourite"],
      ["+ / −", "Zoom in / out"],
      ["0", "Reset zoom"],
      ["double-click", "Zoom to 250%"],
      ["Esc", "Close"],
    ],
  },
  {
    title: "Grids",
    keys: [
      ["Ctrl-click", "Select a photo"],
      ["Enter", "Open the focused photo"],
      ["drag the right edge", "Jump through time"],
    ],
  },
];

export function Shortcuts() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let pendingG = false;
    let timer: number | undefined;
    const onKey = (e: KeyboardEvent) => {
      const typing = ["INPUT", "TEXTAREA", "SELECT"].includes((e.target as HTMLElement)?.tagName);
      if (typing) return;
      if (e.key === "?") {
        e.preventDefault();
        setOpen((v) => !v);
        return;
      }
      if (e.key === "Escape") setOpen(false);
      if (e.key === "g") {
        pendingG = true;
        window.clearTimeout(timer);
        timer = window.setTimeout(() => (pendingG = false), 1200);
        return;
      }
      if (pendingG) {
        const go: Record<string, string> = { p: "/photos", e: "/events", u: "/people", t: "/timeline",
          m: "/map", d: "/duplicates", s: "/search", h: "/" };
        const dest = go[e.key];
        pendingG = false;
        if (dest) window.history.pushState({}, "", dest), window.dispatchEvent(new PopStateEvent("popstate"));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.clearTimeout(timer);
    };
  }, []);

  if (!open) return null;
  return (
    <div className="modal-backdrop" onClick={() => setOpen(false)}>
      <div className="modal shortcuts" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h3><Keyboard size={16} /> Keyboard shortcuts</h3>
          <button className="btn btn-quiet btn-icon btn-sm" onClick={() => setOpen(false)} aria-label="Close">
            <X size={16} />
          </button>
        </div>
        <div className="modal-body shortcuts-body">
          {GROUPS.map((g) => (
            <div key={g.title} className="shortcut-group">
              <div className="fact-head">{g.title}</div>
              {g.keys.map(([k, label]) => (
                <div key={k} className="shortcut-row">
                  <kbd>{k}</kbd>
                  <span className="dim">{label}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
