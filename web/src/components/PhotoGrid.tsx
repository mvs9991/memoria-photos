/**
 * Justified, virtualised photo grid.
 *
 * Rows are packed to a target height like a contact sheet, so every photo keeps
 * its true aspect ratio and each row fills the width exactly. Only the rows
 * inside the viewport (plus an overscan band) are mounted, which keeps a
 * 100k-photo library at a steady frame rate.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Check, Heart, Users } from "lucide-react";
import { thumbUrl } from "../lib/api";
import { formatDay, formatMonth, toDate } from "../lib/format";

export interface GridItem {
  id: number;
  ratio: number;
  ts: number;
  flags?: number;
  score?: number | null;
}

interface Row {
  top: number;
  height: number;
  items: { id: number; w: number; h: number; index: number; flags: number }[];
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
  scrubber?: boolean;
  emptyState?: React.ReactNode;
  headerExtra?: React.ReactNode;
}

const HEADER_H = 46;

export function PhotoGrid({
  items,
  grouping = "day",
  targetHeight = 230,
  gap = 5,
  onOpen,
  selection,
  onToggleSelect,
  selectable = false,
  scrubber = true,
  emptyState,
}: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewport, setViewport] = useState(800);
  const scrollerRef = useRef<HTMLElement | null>(null);

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
    const onScroll = () => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        const host = hostRef.current;
        if (!host) return;
        const offset = host.offsetTop;
        setScrollTop(Math.max(0, scroller.scrollTop - offset));
      });
    };
    scroller.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => {
      scroller.removeEventListener("scroll", onScroll);
      if (raf) cancelAnimationFrame(raf);
    };
  }, [items.length]);

  const sections = useMemo<Section[]>(() => {
    if (!width || items.length === 0) return [];
    const groups: { key: string; label: string; items: GridItem[] }[] = [];
    let current: { key: string; label: string; items: GridItem[] } | null = null;
    for (const it of items) {
      let key = "all";
      let label = "";
      if (grouping !== "none") {
        const d = toDate(it.ts || 0);
        if (!it.ts) {
          key = "unknown";
          label = "No date";
        } else if (grouping === "day") {
          key = `${d.getUTCFullYear()}-${d.getUTCMonth()}-${d.getUTCDate()}`;
          label = formatDay(it.ts);
        } else {
          key = `${d.getUTCFullYear()}-${d.getUTCMonth()}`;
          label = formatMonth(it.ts);
        }
      }
      if (!current || current.key !== key) {
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
          const cell = { id: it.id, w, h, index: index++, flags: it.flags ?? 0 };
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
        count: grp.items.length });
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
    scroller.scrollTo({ top: host.offsetTop + ratio * totalHeight, behavior: "auto" });
  }, [totalHeight]);

  if (!items.length) {
    return <div ref={hostRef}>{emptyState}</div>;
  }

  return (
    <div className="grid-host" ref={hostRef}>
      <div className="grid-canvas" style={{ height: totalHeight }}>
        {visible.map((section) => (
          <div key={section.key} className="grid-section" style={{ top: section.top, height: section.height }}>
            {section.headerHeight > 0 && (
              <div className="grid-section-head" style={{ height: section.headerHeight }}>
                <span className="grid-section-title">{section.label}</span>
                <span className="grid-section-count tnum">{section.count}</span>
              </div>
            )}
            {section.rows
              .filter((r) => r.top + r.height >= scrollTop - overscan && r.top <= scrollTop + viewport + overscan)
              .map((row, ri) => (
                <div key={ri} className="grid-row" style={{ top: row.top - section.top, height: row.height }}>
                  {row.items.map((cell) => (
                    <Tile
                      key={cell.id}
                      id={cell.id}
                      w={cell.w}
                      h={cell.h}
                      flags={cell.flags}
                      index={cell.index}
                      selected={selection?.has(cell.id) ?? false}
                      selectable={selectable}
                      onOpen={onOpen}
                      onToggleSelect={onToggleSelect}
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

function Tile({ id, w, h, flags, index, selected, selectable, onOpen, onToggleSelect }: {
  id: number; w: number; h: number; flags: number; index: number; selected: boolean; selectable: boolean;
  onOpen?: (id: number, index: number) => void; onToggleSelect?: (id: number) => void;
}) {
  const [loaded, setLoaded] = useState(false);
  const size = w > 420 || h > 420 ? "m" : "sm";
  return (
    <div
      className={`tile${selected ? " is-selected" : ""}`}
      style={{ width: w, height: h }}
      onClick={(e) => {
        if (selectable && (e.metaKey || e.ctrlKey || e.shiftKey)) onToggleSelect?.(id);
        else onOpen?.(id, index);
      }}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen?.(id, index);
        }
      }}
      aria-label={`Photo ${id}`}
    >
      <img
        src={thumbUrl(id, size)}
        loading="lazy"
        decoding="async"
        alt=""
        className={loaded ? "is-loaded" : ""}
        onLoad={() => setLoaded(true)}
        draggable={false}
      />
      <div className="tile-shade" />
      {(flags & 1) > 0 && <Heart size={14} className="tile-badge tile-fav" fill="currentColor" />}
      {selectable && (
        <button
          className={`tile-select${selected ? " on" : ""}`}
          onClick={(e) => {
            e.stopPropagation();
            onToggleSelect?.(id);
          }}
          aria-label={selected ? "Deselect" : "Select"}
        >
          <Check size={13} strokeWidth={3} />
        </button>
      )}
    </div>
  );
}

function Scrubber({ sections, totalHeight, scrollTop, viewport, onSeek }: {
  sections: Section[]; totalHeight: number; scrollTop: number; viewport: number;
  onSeek: (ratio: number) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [dragging, setDragging] = useState(false);
  const [hoverY, setHoverY] = useState<number | null>(null);

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
    return out.slice(0, 24);
  }, [sections, totalHeight]);

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
