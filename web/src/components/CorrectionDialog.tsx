/**
 * Fix a wrong date (set it, or shift by the camera clock's error) or a missing place.
 * Stored in Memoria only; the photo files are never modified.
 */
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, MapPin, X } from "lucide-react";
import { Portal } from "./Portal";
import { api } from "../lib/api";

export function CorrectionDialog({ photoIds, mode, onClose }: {
  photoIds: number[];
  mode: "date" | "place";
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [dateMode, setDateMode] = useState<"set" | "shift">(photoIds.length > 1 ? "shift" : "set");
  const [when, setWhen] = useState("");
  const [hours, setHours] = useState(0);
  const [minutes, setMinutes] = useState(0);
  const [days, setDays] = useState(0);
  const [placeQ, setPlaceQ] = useState("");
  const [coords, setCoords] = useState("");
  const places = useQuery({ queryKey: ["places"], queryFn: api.places, enabled: mode === "place" });

  const matches = useMemo(() => {
    const q = placeQ.trim().toLowerCase();
    const list = places.data?.places ?? [];
    return (q ? list.filter((p: any) => `${p.name} ${p.city ?? ""} ${p.admin1 ?? ""} ${p.country ?? ""}`.toLowerCase().includes(q)) : list)
      .slice(0, 12);
  }, [places.data, placeQ]);

  const done = () => {
    qc.invalidateQueries();
    onClose();
  };
  const fixDate = useMutation({
    mutationFn: () => dateMode === "set"
      ? api.correctDate(photoIds, { taken_local: when.replace("T", " ") })
      : api.correctDate(photoIds, { shift_seconds: ((days * 24 + hours) * 60 + minutes) * 60 }),
    onSuccess: done,
  });
  const fixPlace = useMutation({
    mutationFn: (body: { place_id?: number; lat?: number; lon?: number }) => api.correctLocation(photoIds, body),
    onSuccess: done,
  });
  const parsedCoords = (() => {
    const m = coords.match(/^\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*$/);
    return m ? { lat: Number(m[1]), lon: Number(m[2]) } : null;
  })();
  const error = (fixDate.error || fixPlace.error) as Error | null;
  const count = photoIds.length === 1 ? "this photo" : `${photoIds.length.toLocaleString()} photos`;
  const shiftZero = days === 0 && hours === 0 && minutes === 0;

  return (
    <Portal>
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog"
        aria-label={mode === "date" ? "Fix date" : "Fix place"}>
        <div className="modal-head">
          <h3>{mode === "date" ? <CalendarClock size={16} /> : <MapPin size={16} />}
            {mode === "date" ? `Fix the date of ${count}` : `Set the place of ${count}`}</h3>
          <button className="btn btn-quiet btn-icon btn-sm" onClick={onClose} aria-label="Close"><X size={16} /></button>
        </div>
        <div className="modal-body fix-body">
          {mode === "date" ? (
            <>
              <div className="segmented" role="group" aria-label="How to fix the date">
                <button className={dateMode === "set" ? "on" : ""} onClick={() => setDateMode("set")}>Set a date</button>
                <button className={dateMode === "shift" ? "on" : ""} onClick={() => setDateMode("shift")}>Shift the time</button>
              </div>
              {dateMode === "set" ? (
                <label className="fix-row">
                  <span className="dim">Taken on</span>
                  <input className="field" type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)}
                    aria-label="Date and time taken" />
                </label>
              ) : (
                <>
                  <p className="dim fix-hint">For a camera whose clock was wrong: every photo moves by the same amount, so their order is kept.</p>
                  <div className="fix-shift">
                    {[["days", days, setDays], ["hours", hours, setHours], ["minutes", minutes, setMinutes]].map(
                      ([label, v, set]: any) => (
                        <label key={label} className="fix-row">
                          <span className="dim">{label}</span>
                          <input className="field" type="number" value={v} onChange={(e) => set(Number(e.target.value) || 0)}
                            aria-label={`Shift ${label}`} />
                        </label>
                      ))}
                  </div>
                </>
              )}
            </>
          ) : (
            <>
              <input className="field" placeholder="Search your places" value={placeQ} autoFocus
                onChange={(e) => setPlaceQ(e.target.value)} aria-label="Search places" />
              <ul className="browse-list">
                {matches.map((p: any) => (
                  <li key={p.id}>
                    <button className="browse-row" onClick={() => fixPlace.mutate({ place_id: p.id })}>
                      <span className="ellipsis">{p.name}{p.city && p.city !== p.name ? `, ${p.city}` : ""}</span>
                      <span className="dim">{p.country}</span>
                    </button>
                  </li>
                ))}
                {matches.length === 0 && <li className="dim" style={{ padding: 10 }}>No place of yours matches — enter coordinates below.</li>}
              </ul>
              <label className="fix-row">
                <span className="dim">or coordinates</span>
                <input className="field" placeholder="17.3850, 78.4867" value={coords}
                  onChange={(e) => setCoords(e.target.value)} aria-label="Latitude, longitude" />
              </label>
            </>
          )}
          <p className="dim fix-hint">Saved in Memoria only — your photo files are not changed.</p>
        </div>
        <div className="modal-foot">
          {error && <span className="danger-text">{error.message}</span>}
          <button className="btn btn-quiet" onClick={onClose}>Cancel</button>
          {mode === "date" ? (
            <button className="btn btn-primary" onClick={() => fixDate.mutate()}
              disabled={fixDate.isPending || (dateMode === "set" ? !when : shiftZero)}>Apply</button>
          ) : (
            <button className="btn btn-primary" onClick={() => parsedCoords && fixPlace.mutate(parsedCoords)}
              disabled={!parsedCoords || fixPlace.isPending}>Use coordinates</button>
          )}
        </div>
      </div>
    </div>
    </Portal>
  );
}
