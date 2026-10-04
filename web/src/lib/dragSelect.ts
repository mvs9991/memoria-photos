/**
 * Press-and-hold to start selecting, then drag across tiles to select a range,
 * the way Apple Photos and Google Photos do. Shared by the photo grid and the
 * People grid.
 *
 * How it decides what a gesture is:
 *  - Touch / pen: holding still for HOLD_MS starts selection. Moving before that
 *    is a scroll, so the gesture is dropped and the browser scrolls as normal.
 *  - Mouse, not yet selecting: the same hold-still rule.
 *  - Mouse, already selecting: pressing and moving a few pixels drags at once.
 *
 * Once active, the tile under the pointer is found with elementFromPoint and the
 * range from the starting tile to it is applied against a snapshot of the
 * selection taken at the start. That makes dragging back shrink the range again
 * instead of leaving stragglers, and starting on an already-selected tile drags
 * to *deselect*. The range is by index into `ids`, not by what is mounted, so it
 * stays correct in a virtualised grid where most tiles are not in the DOM.
 *
 * Near the top or bottom edge of the scroll container it scrolls for you.
 */
import { useCallback, useEffect, useRef } from "react";

export const HOLD_MS = 650;       // a slow tap (finger resting a moment) lasts ~0.5 s, so 450 turned plain taps into selection
const TOUCH_SLOP = 10;       // px a finger may drift during the hold before it counts as a scroll
const MOUSE_SLOP = 6;        // px of movement that starts a drag when already selecting
const EDGE = 72;             // px from the scroll container's edge where auto-scroll begins
const MAX_SCROLL = 28;       // px per frame

export interface DragSelectOptions {
  /** ids in display order; an item's position here is its index */
  ids: number[];
  selection: Set<number>;
  /** selection has begun, so a plain tap toggles rather than opens */
  selectMode: boolean;
  enabled: boolean;
  /** replace the whole selection */
  setSelection: (ids: number[]) => void;
  /** the page should enter select mode */
  begin: () => void;
  getScroller: () => HTMLElement | null;
}

export function useDragSelect(options: DragSelectOptions) {
  const opts = useRef(options);
  opts.current = options;
  const suppress = useRef(false);
  const stop = useRef<(() => void) | null>(null);
  const lastType = useRef("mouse");
  const running = useRef(false);

  useEffect(() => () => stop.current?.(), []);

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    const o = opts.current;
    lastType.current = e.pointerType;
    if (!o.enabled || e.button !== 0 || !e.isPrimary) return;
    const target = e.target as HTMLElement;
    if (target.closest("[data-no-drag-select]")) return;
    const tile = target.closest<HTMLElement>("[data-sel-index]");
    if (!tile) return;
    stop.current?.();

    // The grid this gesture started in. A page can hold several (People has Named and
    // Discovered, each numbering its tiles from 0), so a tile is only ever looked up
    // inside this one — otherwise dragging over the other grid applies its index here.
    const host = e.currentTarget as HTMLElement;
    const pointerId = e.pointerId;
    const startIndex = Number(tile.dataset.selIndex);
    const touch = e.pointerType !== "mouse";
    const sx = e.clientX;
    const sy = e.clientY;
    let x = sx;
    let y = sy;
    let active = false;
    let timer = 0;
    let raf = 0;
    let lastHover = startIndex;
    let settling = false;             // true while we are holding the pressed tile still under the finger
    let mode: "add" | "remove" = "add";
    let base = new Set<number>();

    const apply = (hover: number) => {
      const ids = opts.current.ids;
      const lo = Math.max(0, Math.min(startIndex, hover));
      const hi = Math.min(ids.length - 1, Math.max(startIndex, hover));
      const next = new Set(base);
      for (let i = lo; i <= hi; i++) {
        if (mode === "add") next.add(ids[i]);
        else next.delete(ids[i]);
      }
      lastHover = hover;
      opts.current.setSelection([...next]);
    };

    const hoverAt = (px: number, py: number): number | null => {
      const el = document.elementFromPoint(px, py)?.closest<HTMLElement>("[data-sel-index]");
      return el && host.contains(el) ? Number(el.dataset.selIndex) : null;
    };

    // While a finger is down and we own the gesture, stop the page scrolling under it.
    const block = (ev: TouchEvent) => { if (ev.cancelable) ev.preventDefault(); };

    const loop = () => {
      if (!active) return;
      const sc = opts.current.getScroller();
      if (sc) {
        const r = sc.getBoundingClientRect();
        let dy = 0;
        if (y < r.top + EDGE) dy = -Math.ceil((r.top + EDGE - y) / 3);
        else if (y > r.bottom - EDGE) dy = Math.ceil((y - (r.bottom - EDGE)) / 3);
        dy = Math.max(-MAX_SCROLL, Math.min(MAX_SCROLL, dy));
        // "instant": the content area has scroll-behavior: smooth, which would turn
        // every frame's nudge into an animation that fights the next one.
        if (dy) sc.scrollBy({ top: dy, behavior: "instant" as ScrollBehavior });
      }
      const h = hoverAt(x, y);
      if (h !== null && h !== lastHover) apply(h);
      raf = requestAnimationFrame(loop);
    };

    // The selection bar appears the moment selection starts and pushes the grid down by its own
    // height, so the tile under the finger slid away mid-gesture and the drag started from the wrong
    // place. For the first few frames, scroll by however far that tile has moved, so it stays put.
    const tileTop = () => {
      const el = host.querySelector<HTMLElement>(`[data-sel-index="${startIndex}"]`);
      return el ? el.getBoundingClientRect().top : null;
    };
    const keepTileUnderFinger = (top0: number) => {
      let frames = 0;
      const tick = () => {
        if (!active || !settling || frames++ > 10) return;
        const now = tileTop();
        const sc = opts.current.getScroller();
        if (now !== null && sc && Math.abs(now - top0) > 1) {
          // "instant": the content area has scroll-behavior: smooth, which would lag a frame behind.
          sc.scrollBy({ top: now - top0, behavior: "instant" as ScrollBehavior });
        }
        requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    };

    const activate = () => {
      if (active) return;
      active = true;
      running.current = true;
      suppress.current = true;          // the click that follows the release must not open the tile
      const cur = opts.current;
      const top0 = tileTop();
      settling = top0 !== null;
      cur.begin();
      base = new Set(cur.selection);
      mode = base.has(cur.ids[startIndex]) ? "remove" : "add";
      apply(startIndex);
      try { navigator.vibrate?.(12); } catch { /* not every browser has it */ }
      document.addEventListener("touchmove", block, { passive: false });
      raf = requestAnimationFrame(loop);
      if (top0 !== null) keepTileUnderFinger(top0);
    };

    const end = () => {
      window.clearTimeout(timer);
      cancelAnimationFrame(raf);
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", end);
      document.removeEventListener("touchmove", block);
      if (active) {
        // The click event follows pointerup; keep swallowing it a moment.
        window.setTimeout(() => { suppress.current = false; running.current = false; }, 80);
      }
      active = false;
      stop.current = null;
    };

    function move(ev: PointerEvent) {
      if (ev.pointerId !== pointerId) return;
      x = ev.clientX;
      y = ev.clientY;
      if (!active) {
        const d = Math.hypot(x - sx, y - sy);
        if (touch) {
          if (d > TOUCH_SLOP) end();                         // the finger moved: that is a scroll
        } else if (opts.current.selectMode) {
          if (d > MOUSE_SLOP) activate();                    // already selecting: drag right away
        } else if (d > TOUCH_SLOP) {
          end();                                             // moved before the hold finished
        }
        return;
      }
      ev.preventDefault();
      if (settling && Math.hypot(x - sx, y - sy) > TOUCH_SLOP) settling = false;
      const h = hoverAt(x, y);
      if (h !== null && h !== lastHover) apply(h);
    }

    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
    stop.current = end;
    // Already selecting with a mouse: no hold needed, movement starts the drag.
    if (touch || !o.selectMode) timer = window.setTimeout(activate, HOLD_MS);
  }, []);

  /** A long press raises the browser's context menu on touch; suppress it for ours. */
  const onContextMenu = useCallback((e: React.MouseEvent) => {
    if (!opts.current.enabled) return;
    if (running.current || lastType.current !== "mouse") e.preventDefault();
  }, []);

  const shouldSuppressClick = useCallback(() => suppress.current, []);

  return { onPointerDown, onContextMenu, shouldSuppressClick };
}
