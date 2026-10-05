/**
 * Full-screen slideshow, also the photo-frame mode (/frame).
 *
 * Two stacked layers crossfade; the next image is decoded before it is shown so a
 * slow disk never flashes a half-loaded frame. Videos play muted and move on when
 * they end (or after a minute). Controls fade out while the mouse is still.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, Maximize, Pause, Play, Shuffle, X } from "lucide-react";
import { Portal } from "./Portal";
import { thumbUrl, videoUrl } from "../lib/api";

export interface SlideItem {
  id: number;
  video?: boolean;
  /** the user's turn: part of the image URL, so a photo rotated since it was cached shows turned */
  rot?: number;
}

const SPEEDS = [3, 5, 8, 15];

function shuffled<T>(list: T[]): T[] {
  const out = [...list];
  for (let i = out.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out;
}

export function Slideshow({ items, start = 0, onClose, frame = false, onExhausted }: {
  items: SlideItem[];
  start?: number;
  onClose?: () => void;
  /** photo-frame mode: a clock, no close button, loops for ever */
  frame?: boolean;
  /** called when the last slide has been shown (the frame fetches a fresh batch) */
  onExhausted?: () => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [order, setOrder] = useState(items);
  const [index, setIndex] = useState(Math.min(start, Math.max(0, items.length - 1)));
  const [playing, setPlaying] = useState(true);
  const [shuffle, setShuffle] = useState(frame);
  const [speed, setSpeed] = useState(() => Number(localStorage.getItem("slideshow-speed")) || 5);
  const [slots, setSlots] = useState<{ items: (SlideItem | null)[]; front: number }>({ items: [null, null], front: 0 });
  const [chrome, setChrome] = useState(true);
  const [now, setNow] = useState(() => new Date());
  const hideTimer = useRef<number | undefined>(undefined);

  // Keyed on what the list holds, not on the array: pages pass `items={list.map(...)}`, a new array on every
  // render of theirs, and any re-render (a job finishing, stats refreshing) reshuffled a running show.
  const itemsRef = useRef(items);
  itemsRef.current = items;
  const signature = items.map((i) => `${i.id}:${i.rot ?? 0}`).join(",");
  useEffect(() => {
    const list = itemsRef.current;
    setOrder(shuffle ? shuffled(list) : list);
    if (frame) setIndex(0);
  }, [signature, shuffle, frame]);

  const current = order[index];

  // Decode the next image off-screen, then swap it in on the back layer.
  useEffect(() => {
    if (!current) return;
    let cancelled = false;
    const show = () => {
      if (cancelled) return;
      setSlots((s) => {
        const items = [...s.items];
        items[1 - s.front] = current;
        return { items, front: 1 - s.front };
      });
    };
    if (current.video) show();
    else {
      const img = new Image();
      img.src = thumbUrl(current.id, "l", current.rot);
      img.decode().then(show, show);
    }
    // Warm the one after, so the next transition is instant.
    const after = order[index + 1];
    if (after && !after.video) new Image().src = thumbUrl(after.id, "l", after.rot);
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [index, order]);

  const go = useCallback((delta: number) => {
    if (!order.length) return;
    const next = index + delta;
    if (next >= order.length) {
      // Keep going round while a frame fetches its next batch: if the batch failed, or came back in the same
      // order (so nothing re-rendered), waiting for it left the frame on its last slide for good.
      onExhausted?.();
      setIndex(0);
    } else setIndex((next + order.length) % order.length);
  }, [index, order.length, onExhausted]);

  // Timed advance (videos advance themselves when they end).
  useEffect(() => {
    if (!playing || !current) return;
    const ms = current.video ? 60_000 : speed * 1000;
    const t = window.setTimeout(() => go(1), ms);
    return () => clearTimeout(t);
  }, [playing, current, speed, go]);

  useEffect(() => {
    if (!frame) return;
    const t = window.setInterval(() => setNow(new Date()), 15_000);
    return () => clearInterval(t);
  }, [frame]);

  const poke = useCallback(() => {
    setChrome(true);
    clearTimeout(hideTimer.current);
    hideTimer.current = window.setTimeout(() => setChrome(false), 2500);
  }, []);
  useEffect(() => {
    poke();
    return () => clearTimeout(hideTimer.current);
  }, [poke]);

  const toggleFullscreen = () => {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else rootRef.current?.requestFullscreen?.().catch(() => {});
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      poke();
      if (e.key === "Escape") {
        if (!document.fullscreenElement) onClose?.();
      } else if (e.key === "ArrowRight") go(1);
      else if (e.key === "ArrowLeft") go(-1);
      else if (e.key === " ") {
        e.preventDefault();
        setPlaying((p) => !p);
      } else if (e.key.toLowerCase() === "f") toggleFullscreen();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [go, onClose, poke]);

  useEffect(() => {
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = "";
      if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    };
  }, []);

  const setSpeedSaved = (s: number) => {
    setSpeed(s);
    try { localStorage.setItem("slideshow-speed", String(s)); } catch { /* private mode */ }
  };

  return (
    <Portal>
      <div ref={rootRef} className={`slideshow${chrome ? "" : " is-idle"}`} onMouseMove={poke}
        onClick={poke} role="dialog" aria-label="Slideshow">
        {slots.items.map((it, n) => {
          if (!it) return null;
          const on = n === slots.front;
          return it.video ? (
            <video key={`${n}-${it.id}`} className={`slide${on ? " on" : ""}`} src={videoUrl(it.id)}
              poster={thumbUrl(it.id, "l", it.rot)} autoPlay={on} muted playsInline
              onEnded={() => on && playing && go(1)} />
          ) : (
            <img key={`${n}-${it.id}`} className={`slide slide-kb${on ? " on" : ""}`} src={thumbUrl(it.id, "l", it.rot)}
              alt="" draggable={false} style={{ animationDuration: `${speed + 2}s` }} />
          );
        })}

        {frame && (
          <div className="frame-clock">
            <span className="frame-time tnum">{now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
            <span className="frame-date">{now.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" })}</span>
          </div>
        )}

        <div className="slideshow-bar" onClick={(e) => e.stopPropagation()}>
          <button className="ss-btn" onClick={() => go(-1)} aria-label="Previous"><ChevronLeft size={20} /></button>
          <button className="ss-btn" onClick={() => setPlaying((p) => !p)} aria-label={playing ? "Pause" : "Play"}>
            {playing ? <Pause size={18} /> : <Play size={18} />}
          </button>
          <button className="ss-btn" onClick={() => go(1)} aria-label="Next"><ChevronRight size={20} /></button>
          <span className="ss-count tnum">{order.length ? index + 1 : 0} / {order.length}</span>
          <div className="ss-speeds" role="group" aria-label="Seconds per photo">
            {SPEEDS.map((s) => (
              <button key={s} className={`ss-speed${speed === s ? " on" : ""}`} onClick={() => setSpeedSaved(s)}>{s}s</button>
            ))}
          </div>
          {!frame && (
            <button className={`ss-btn${shuffle ? " on" : ""}`} onClick={() => { setShuffle((v) => !v); setIndex(0); }}
              aria-label="Shuffle" title="Shuffle"><Shuffle size={17} /></button>
          )}
          <button className="ss-btn" onClick={toggleFullscreen} aria-label="Full screen" title="Full screen (F)">
            <Maximize size={17} />
          </button>
          {onClose && (
            <button className="ss-btn" onClick={onClose} aria-label="Close slideshow"><X size={19} /></button>
          )}
        </div>
      </div>
    </Portal>
  );
}
