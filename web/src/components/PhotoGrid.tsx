/**
 * Justified, virtualised photo grid.
 *
 * Rows are packed to a target height like a contact sheet, so every photo keeps
 * its true aspect ratio and each row fills the width exactly. Only the rows
 * inside the viewport (plus an overscan band) are mounted, which keeps a
 * 100k-photo library at a steady frame rate.
 */
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Check, Heart, Layers, Play, Star } from "lucide-react";
import { FLAG, thumbUrl } from "../lib/api";
import { clock, formatDay, formatMonth, toDate } from "../lib/format";
import { useDragSelect } from "../lib/dragSelect";

export interface GridItem {
  id: number;
  ratio: number;
  ts: number;
  flags?: number;
  /** video length in seconds */
  dur?: number;
  rating?: number;
  /** stack size on a stack cover */
  stack?: number;
  rot?: number;
  score?: number | null;
}

interface Row {
  top: number;
  height: number;
  items: { id: number; w: number; h: number; index: number; flags: number; dur: number; rating: number; stack: number;
    rot: number }[];
}

interface Section {
  key: string;
  label: string;
  sub?: string;
  top: number;
  height: number;
  headerHeight: number;
  rows: Row[];
  count: number;
  /** every photo in the section, for its select-all control */
  ids: number[];
}

interface Props {
  items: GridItem[];
  grouping?: "day" | "month" | "none";
  targetHeight?: number;
  gap?: number;
  onOpen?: (id: number, index: number) => void;
  selection?: Set<number>;
  onToggleSelect?: (id: number) => void;
  selectable?: boolean;
  /** a plain click selects instead of opening */
  selectMode?: boolean;
  /** shift-click: select everything between the last clicked tile and this one */
  onSelectRange?: (ids: number[]) => void;
  /** replace the whole selection; enables press-and-hold, then drag across tiles to select */
  onSetSelection?: (ids: number[]) => void;
  /** called when a press-and-hold starts selecting, so the page can switch select mode on */
  onBeginSelect?: () => void;
  /** thumbnail URL override (the public share page uses token-scoped URLs) */
  thumbFor?: (id: number, size: "sm" | "m") => string;
  scrubber?: boolean;
  emptyState?: React.ReactNode;
  headerExtra?: React.ReactNode;
}

const HEADER_H = 46;

/** Holds back thumbnail downloads while the grid moves faster than anyone can look at it. Tiles that
 * mount during a fast scroll wait here, and get their image when it slows down or stops; tiles that
 * scroll away first never ask the server for anything. */
interface LoadGate { fast: boolean; set(fast: boolean): void; wait(go: () => void): () => void }
function createGate(): LoadGate {
  const waiting = new Set<() => void>();
  const gate: LoadGate = {
    fast: false,
    set(fast) {
      if (gate.fast === fast) return;
      gate.fast = fast;
      if (!fast) { const all = [...waiting]; waiting.clear(); all.forEach((go) => go()); }
    },
    wait(go) { waiting.add(go); return () => { waiting.delete(go); }; },
  };
  return gate;
}
const NO_SELECTION: Set<number> = new Set();
const noop = () => {};

export function PhotoGrid({
  items,
  grouping = "day",
  targetHeight = 230,
  gap = 5,
  onOpen,
  selection,
  onToggleSelect,
  selectable = false,
  selectMode = false,
  onSelectRange,
  onSetSelection,
  onBeginSelect,
  thumbFor,
  scrubber = true,
  emptyState,
}: Props) {
  // Selecting is "the page's Select mode is on" OR "something is already selected". Without the
  // second half, picking a photo with its check circle (or Ctrl-click) left select mode off, so the
  // next plain click on another photo opened the preview instead of adding it to the selection.
  const selecting = selectMode || (selection?.size ?? 0) > 0;
  const anchorRef = useRef<number | null>(null);
  const handleSelect = useCallback((id: number, index: number, shift: boolean) => {
    if (shift && onSelectRange && anchorRef.current !== null) {
      const [a, b] = [Math.min(anchorRef.current, index), Math.max(anchorRef.current, index)];
      onSelectRange(items.slice(a, b + 1).map((it) => it.id));
    } else {
      onToggleSelect?.(id);
    }
    anchorRef.current = index;
    onBeginSelect?.();           // keep the page's own select mode (its Done button) in step
  }, [items, onSelectRange, onToggleSelect, onBeginSelect]);

  // Select, or deselect, every photo in one day/month section.
  const toggleSection = useCallback((sectionIds: number[]) => {
    const cur = selection ?? NO_SELECTION;
    const allIn = sectionIds.length > 0 && sectionIds.every((id) => cur.has(id));
    onBeginSelect?.();
    if (allIn) {
      const drop = new Set(sectionIds);
      onSetSelection?.([...cur].filter((id) => !drop.has(id)));
    } else if (onSetSelection) {
      onSetSelection([...new Set([...cur, ...sectionIds])]);
    } else {
      onSelectRange?.(sectionIds);
    }
  }, [selection, onSetSelection, onSelectRange, onBeginSelect]);
  const hostRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewport, setViewport] = useState(800);
  const scrollerRef = useRef<HTMLElement | null>(null);
  const gate = useMemo(createGate, []);

  // Press and hold a tile, then drag across others. Works by index into `items`, so
  // it stays correct although most tiles of a big library are not mounted.
  const ids = useMemo(() => items.map((it) => it.id), [items]);
  const drag = useDragSelect({
    ids,
    selection: selection ?? NO_SELECTION,
    selectMode: selecting,
    enabled: selectable && !!onSetSelection,
    setSelection: onSetSelection ?? noop,
    begin: onBeginSelect ?? noop,
    getScroller: () => scrollerRef.current,
  });

  // Measure the width of the grid and the height of its scroll container.
  useLayoutEffect(() => {
    const el = hostRef.current;
    if (!el) return;
    const scroller = el.closest("[data-scroll-root]") as HTMLElement | null;
    scrollerRef.current = scroller;
    const measure = () => {
      setWidth(el.clientWidth);
      setViewport(scroller ? scroller.clientHeight : window.innerHeight);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    if (scroller) ro.observe(scroller);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const scroller = scrollerRef.current;
    if (!scroller) return;
    let raf = 0;
    let lastTop = scroller.scrollTop;
    let lastAt = performance.now();
    let idle = 0;
    let speed = 0;
    const onScroll = () => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        const host = hostRef.current;
        if (!host) return;
        const now = performance.now();
        const top = scroller.scrollTop;
        // px per second, smoothed over a few frames (a wheel or fling delivers uneven steps);
        // "fast" = more than eight screens a second
        speed = speed * 0.6 + (Math.abs(top - lastTop) / Math.max(1, now - lastAt) * 1000) * 0.4;
        lastTop = top;
        lastAt = now;
        clearTimeout(idle);
        if (speed > scroller.clientHeight * 8) {
          gate.set(true);
          idle = window.setTimeout(() => { speed = 0; gate.set(false); }, 150);
        } else {
          gate.set(false);
        }
        const offset = host.offsetTop;
        setScrollTop(Math.max(0, top - offset));
      });
    };
    scroller.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => {
      scroller.removeEventListener("scroll", onScroll);
      if (raf) cancelAnimationFrame(raf);
      clearTimeout(idle);
      gate.set(false);
    };
  }, [items.length, gate]);

  const sections = useMemo<Section[]>(() => {
    if (!width || items.length === 0) return [];
    const groups: { key: string; label: string; items: GridItem[] }[] = [];
    let current: { key: string; label: string; items: GridItem[] } | null = null;
    for (const it of items) {
      let key = "all";
      if (grouping !== "none") {
        const d = toDate(it.ts || 0);
        if (!it.ts) key = "unknown";
        else if (grouping === "day") key = `${d.getUTCFullYear()}-${d.getUTCMonth()}-${d.getUTCDate()}`;
        else key = `${d.getUTCFullYear()}-${d.getUTCMonth()}`;
      }
      if (!current || current.key !== key) {
        // The label is only needed once per section: formatting it for every photo (24k on a real
        // library, each reading the clock) was the grid's biggest JavaScript cost when the page opened.
        const label = grouping === "none" ? "" : !it.ts ? "No date"
          : grouping === "day" ? formatDay(it.ts) : formatMonth(it.ts);
        current = { key, label, items: [] };
        groups.push(current);
      }
      current.items.push(it);
    }

    const out: Section[] = [];
    let top = 0;
    let index = 0;
    for (const grp of groups) {
      const headerHeight = grouping === "none" ? 0 : HEADER_H;
      const rows: Row[] = [];
      let rowTop = top + headerHeight;
      let buf: GridItem[] = [];
      let ratioSum = 0;
      const flush = (isLast: boolean) => {
        if (!buf.length) return;
        const gaps = gap * (buf.length - 1);
        let h = (width - gaps) / ratioSum;
        // A short trailing row shouldn't be blown up to full width.
        if (isLast && h > targetHeight * 1.35) h = targetHeight;
        h = Math.round(h);
        let x = 0;
        const rowItems = buf.map((it, i) => {
          const w = i === buf.length - 1 && !isLast
            ? Math.max(1, width - x)
            : Math.round(it.ratio * h);
          const cell = { id: it.id, w, h, index: index++, flags: it.flags ?? 0, dur: it.dur ?? 0,
            rating: it.rating ?? 0, stack: it.stack ?? 0, rot: it.rot ?? 0 };
          x += w + gap;
          return cell;
        });
        rows.push({ top: rowTop, height: h, items: rowItems });
        rowTop += h + gap;
        buf = [];
        ratioSum = 0;
      };
      for (const it of grp.items) {
        const r = Math.min(Math.max(it.ratio || 1.333, 0.28), 5);
        buf.push({ ...it, ratio: r });
        ratioSum += r;
        const projected = (width - gap * (buf.length - 1)) / ratioSum;
        if (projected < targetHeight) flush(false);
      }
      flush(true);
      const height = (rowTop - top) - (rows.length ? gap : 0);
      out.push({ key: grp.key, label: grp.label, top, height: height + 22, headerHeight, rows,
        count: grp.items.length, ids: grp.items.map((i) => i.id) });
      top += height + 22;
    }
    return out;
  }, [items, width, grouping, targetHeight, gap]);

  const totalHeight = sections.length ? sections[sections.length - 1].top + sections[sections.length - 1].height : 0;
  const overscan = Math.max(600, viewport);
  const visible = useMemo(() => {
    const from = scrollTop - overscan;
    const to = scrollTop + viewport + overscan;
    return sections.filter((s) => s.top + s.height >= from && s.top <= to);
  }, [sections, scrollTop, viewport, overscan]);

  const scrollToRatio = useCallback((ratio: number) => {
    const scroller = scrollerRef.current;
    const host = hostRef.current;
    if (!scroller || !host) return;
    scroller.scrollTo({ top: host.offsetTop + ratio * totalHeight, behavior: "instant" as ScrollBehavior });   // "auto" means the CSS value: smooth, one animation per move
  }, [totalHeight]);

  if (!items.length) {
    return <div ref={hostRef}>{emptyState}</div>;
  }

  return (
    <div className={`grid-host${selectable ? " is-selectable" : ""}${selectable && selecting ? " is-selecting" : ""}`} ref={hostRef}
      onPointerDown={drag.onPointerDown} onContextMenu={drag.onContextMenu}>
      <div className="grid-canvas" style={{ height: totalHeight }}>
        {visible.map((section) => (
          <div key={section.key} className="grid-section" style={{ top: section.top, height: section.height }}>
            {section.headerHeight > 0 && (
              <div className="grid-section-head" style={{ height: section.headerHeight }}>
                <span className="grid-section-title">{section.label}</span>
                <span className="grid-section-count tnum">{section.count}</span>
                {selectable && (onSetSelection || onSelectRange) && (() => {
                  const chosen = section.ids.filter((id) => selection?.has(id)).length;
                  const all = chosen === section.ids.length && chosen > 0;
                  return (
                    <button className={`section-select${all ? " on" : chosen ? " some" : ""}`} data-no-drag-select
                      aria-pressed={all}
                      aria-label={`${all ? "Deselect" : "Select"} all ${section.count} from ${section.label || "this section"}`}
                      onClick={() => toggleSection(section.ids)}>
                      <span className="section-select-dot"><Check size={11} strokeWidth={3} /></span>
                      <span className="section-select-label">
                        {all ? "Deselect all" : chosen ? `Select all (${chosen} of ${section.count})` : "Select all"}
                      </span>
                    </button>
                  );
                })()}
              </div>
            )}
            {section.rows
              .filter((r) => r.top + r.height >= scrollTop - overscan && r.top <= scrollTop + viewport + overscan)
              .map((row) => (
                <div key={row.top} className="grid-row" style={{ top: row.top - section.top, height: row.height }}>
                  {row.items.map((cell) => (
                    <Tile
                      key={cell.id}
                      id={cell.id}
                      w={cell.w}
                      h={cell.h}
                      flags={cell.flags}
                      dur={cell.dur}
                      rating={cell.rating}
                      stack={cell.stack}
                      rot={cell.rot}
                      index={cell.index}
                      selected={selection?.has(cell.id) ?? false}
                      selectable={selectable}
                      selectMode={selecting}
                      onOpen={onOpen}
                      onSelect={handleSelect}
                      suppressClick={drag.shouldSuppressClick}
                      thumbFor={thumbFor}
                      gate={gate}
                    />
                  ))}
                </div>
              ))}
          </div>
        ))}
      </div>
      {scrubber && sections.length > 3 && totalHeight > viewport * 2 && (
        <Scrubber sections={sections} totalHeight={totalHeight} scrollTop={scrollTop} viewport={viewport}
          onSeek={scrollToRatio} />
      )}
    </div>
  );
}

const Tile = memo(function Tile({ id, w, h, flags, dur, rating, stack, rot, index, selected, selectable, selectMode, onOpen, onSelect, suppressClick, thumbFor, gate }: {
  id: number; w: number; h: number; flags: number; dur: number; rating: number; stack: number; rot: number; index: number;
  selected: boolean; selectable: boolean;
  selectMode: boolean;
  onOpen?: (id: number, index: number) => void; onSelect: (id: number, index: number, shift: boolean) => void;
  /** true for a moment after a press-and-hold or drag, so its release does not also open the tile */
  suppressClick: () => boolean;
  thumbFor?: (id: number, size: "sm" | "m") => string;
  gate: LoadGate;
}) {
  const [loaded, setLoaded] = useState(false);
  const [go, setGo] = useState(() => !gate.fast);
  useEffect(() => (go ? undefined : gate.wait(() => setGo(true))), [go, gate]);
  const size = w > 420 || h > 420 ? "m" : "sm";
  // A tile scrolled away before its thumbnail arrived: cancel the download, or a fast scroll leaves
  // hundreds of requests queued ahead of the photos that are on screen when it stops.
  const imgRef = useRef<HTMLImageElement>(null);
  useEffect(() => {
    const img = imgRef.current;          // React clears the ref before this cleanup runs
    return () => { if (img && !img.complete) img.removeAttribute("src"); };
  }, []);
  return (
    // The select button is a SIBLING of the tile, not a child: a focusable control
    // nested inside a focusable control is invalid, and assistive technology can still
    // reach it however it is hidden. The wrapper positions them over one another.
    <div className="tile-wrap" style={{ width: w, height: h }} data-sel-index={index}>
    <div
      className={`tile${selected ? " is-selected" : ""}`}
      style={{ width: w, height: h }}
      onClick={(e) => {
        if (suppressClick()) return;
        if (selectable && (selectMode || e.metaKey || e.ctrlKey || e.shiftKey)) onSelect(id, index, e.shiftKey);
        else onOpen?.(id, index);
      }}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          if (selectable && selectMode) onSelect(id, index, e.shiftKey);
          else onOpen?.(id, index);
        }
      }}
      aria-label={(flags & FLAG.video) ? `Video ${id}` : `Photo ${id}`}
      // In select mode the tile itself is the toggle, so it carries the state.
      aria-pressed={selectable && selectMode ? selected : undefined}
    >
      <img
        ref={imgRef}
        src={!go ? undefined : thumbFor ? thumbFor(id, size) + (rot ? `&r=${rot}` : "") : thumbUrl(id, size, rot)}
        loading="lazy"
        decoding="async"
        alt=""
        className={loaded ? "is-loaded" : ""}
        onLoad={() => setLoaded(true)}
        draggable={false}
      />
      <div className="tile-shade" />
      {(flags & FLAG.favorite) > 0 && <Heart size={14} className="tile-badge tile-fav" fill="currentColor" />}
      {(flags & FLAG.video) > 0 && (
        <span className="tile-media tnum"><Play size={10} fill="currentColor" /> {clock(dur)}</span>
      )}
      {(flags & FLAG.live) > 0 && <span className="tile-media">LIVE</span>}
      {(flags & FLAG.stack) > 0 && (
        <span className="tile-media tile-stack tnum" title={`Stack of ${stack}`}><Layers size={10} /> {stack}</span>
      )}
      {rating > 0 && (
        <span className="tile-stars" aria-label={`${rating} stars`}
          style={(flags & FLAG.favorite) ? { right: 27 } : undefined}>
          {Array.from({ length: rating }).map((_, i) => <Star key={i} size={9} fill="currentColor" />)}
        </span>
      )}
    </div>
      {selectable && (
        <button
          className={`tile-select${selected ? " on" : ""}`}
          data-no-drag-select
          onClick={(e) => {
            e.stopPropagation();
            if (suppressClick()) return;
            onSelect(id, index, e.shiftKey);
          }}
          aria-label={selected ? `Deselect photo ${id}` : `Select photo ${id}`}
          aria-pressed={selected}
        >
          {/* The tick is transparent until selected: drawing an SVG in every tile anyway cost layout and paint while scrolling. */}
          {selected && <Check size={13} strokeWidth={3} />}
        </button>
      )}
    </div>
  );
});

function Scrubber({ sections, totalHeight, scrollTop, viewport, onSeek }: {
  sections: Section[]; totalHeight: number; scrollTop: number; viewport: number;
  onSeek: (ratio: number) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [dragging, setDragging] = useState(false);
  const [hoverY, setHoverY] = useState<number | null>(null);
  const [height, setHeight] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setHeight(el.clientHeight));
    ro.observe(el);
    setHeight(el.clientHeight);
    return () => ro.disconnect();
  }, []);

  const marks = useMemo(() => {
    const seen = new Set<string>();
    const out: { label: string; ratio: number }[] = [];
    for (const s of sections) {
      const label = s.label.split(",").pop()?.trim() ?? s.label;
      const year = label.match(/\d{4}/)?.[0] ?? label;
      if (seen.has(year)) continue;
      seen.add(year);
      out.push({ label: year, ratio: s.top / Math.max(totalHeight, 1) });
    }
    // Years with few photos sit a few pixels apart; keep a label only if it clears the previous one.
    const MIN_GAP = 16;
    const kept: { label: string; ratio: number }[] = [];
    for (const m of out) {
      const last = kept[kept.length - 1];
      if (height > 0 && last && (m.ratio - last.ratio) * height < MIN_GAP) continue;
      kept.push(m);
    }
    return kept.slice(0, 24);
  }, [sections, totalHeight, height]);

  const handle = (clientY: number) => {
    const el = ref.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const ratio = Math.min(1, Math.max(0, (clientY - rect.top) / rect.height));
    onSeek(ratio);
  };

  const labelAt = (ratio: number) => {
    const target = ratio * totalHeight;
    let best = sections[0];
    for (const s of sections) if (s.top <= target) best = s;
    return best?.label ?? "";
  };

  const thumbRatio = totalHeight > 0 ? scrollTop / totalHeight : 0;

  return (
    <div
      ref={ref}
      className={`scrubber${dragging ? " is-dragging" : ""}`}
      onMouseDown={(e) => {
        setDragging(true);
        handle(e.clientY);
      }}
      onMouseMove={(e) => {
        const rect = ref.current!.getBoundingClientRect();
        setHoverY(e.clientY - rect.top);
        if (dragging) handle(e.clientY);
      }}
      onMouseLeave={() => {
        setHoverY(null);
        setDragging(false);
      }}
      onMouseUp={() => setDragging(false)}
    >
      {marks.map((m) => (
        <span key={m.label} className="scrubber-mark" style={{ top: `${m.ratio * 100}%` }}>
          {m.label}
        </span>
      ))}
      <span className="scrubber-thumb" style={{ top: `${thumbRatio * 100}%`,
        height: `${Math.max(6, (viewport / Math.max(totalHeight, 1)) * 100)}%` }} />
      {hoverY !== null && (
        <span className="scrubber-tip" style={{ top: hoverY }}>
          {labelAt(hoverY / (ref.current?.clientHeight || 1))}
        </span>
      )}
    </div>
  );
}
