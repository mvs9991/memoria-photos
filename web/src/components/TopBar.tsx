import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Loader2, Moon, Search, Sun, X } from "lucide-react";
import { api, faceUrl, thumbUrl } from "../lib/api";
import { useTheme } from "../lib/hooks";
import { JobIndicator } from "./JobIndicator";

const EXAMPLES = [
  "beach photos", "best photos from 2024", "wedding", "show my trips", "photos at a temple",
  "screenshots", "sunset", "group photos",
];

export function TopBar() {
  const navigate = useNavigate();
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const [placeholder] = useState(() => EXAMPLES[Math.floor(Math.random() * EXAMPLES.length)]);
  const boxRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const { theme, toggle } = useTheme();

  const { data, isFetching } = useQuery({
    queryKey: ["suggestions", q],
    queryFn: () => api.suggestions(q),
    enabled: open,
    staleTime: 15_000,
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = ["INPUT", "TEXTAREA"].includes((e.target as HTMLElement)?.tagName);
      if ((e.key === "/" && !typing) || ((e.metaKey || e.ctrlKey) && e.key === "k")) {
        e.preventDefault();
        inputRef.current?.focus();
        setOpen(true);
      }
      if (e.key === "Escape") {
        setOpen(false);
        inputRef.current?.blur();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (!boxRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  const submit = (text: string) => {
    if (!text.trim()) return;
    setOpen(false);
    navigate(`/search?q=${encodeURIComponent(text.trim())}`);
  };

  const suggestions = data?.suggestions ?? [];

  return (
    <header className="topbar">
      <div className="searchbox" ref={boxRef}>
        <Search size={17} className="searchbox-icon" />
        <input
          ref={inputRef}
          className="searchbox-input"
          value={q}
          placeholder={`Search your photos — try “${placeholder}”`}
          onChange={(e) => {
            setQ(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit(q);
          }}
          aria-label="Search photos"
        />
        {isFetching && <Loader2 size={15} className="spin dim" />}
        {q && (
          <button className="btn btn-quiet btn-icon btn-sm" onClick={() => { setQ(""); inputRef.current?.focus(); }}
            aria-label="Clear search">
            <X size={14} />
          </button>
        )}
        <kbd className="searchbox-kbd">/</kbd>

        {open && (
          <div className="suggest">
            {q.trim() && (
              <button className="suggest-row is-primary" onClick={() => submit(q)}>
                <Search size={15} />
                <span>
                  Search for <strong>{q}</strong>
                </span>
              </button>
            )}
            {suggestions.map((s: any, i: number) => (
              <button
                key={`${s.type}-${s.id ?? s.label}-${i}`}
                className="suggest-row"
                onClick={() => {
                  setOpen(false);
                  if (s.type === "person") navigate(`/people/${s.id}`);
                  else if (s.type === "event") navigate(`/events/${s.id}`);
                  else if (s.type === "place") navigate(`/places/${s.id}`);
                  else submit(s.label);
                }}
              >
                <span className="suggest-thumb">
                  {s.cover_face_id ? (
                    <img src={faceUrl(s.cover_face_id, 64)} alt="" />
                  ) : s.cover_photo_id ? (
                    <img src={thumbUrl(s.cover_photo_id, "sm")} alt="" />
                  ) : (
                    <span className="suggest-dot" />
                  )}
                </span>
                <span className="suggest-main">
                  <span className="suggest-label">{s.label}</span>
                  <span className="suggest-detail dim">{s.detail}</span>
                </span>
                <span className="suggest-type dim">{s.type}</span>
              </button>
            ))}
            {!q.trim() && (
              <div className="suggest-examples">
                {EXAMPLES.slice(0, 5).map((ex) => (
                  <button key={ex} className="chip chip-button" onClick={() => submit(ex)}>
                    {ex}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      <div className="topbar-actions">
        <JobIndicator />
        <button className="btn btn-quiet btn-icon" onClick={toggle} title="Toggle theme"
          aria-label="Toggle colour theme">
          {theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
        </button>
      </div>
    </header>
  );
}
