import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { CalendarDays, FolderOutput, Heart, Images, LayoutGrid, MonitorPlay, Rows3, SlidersHorizontal, Star, X } from "lucide-react";
import { api, FLAG, gridItems } from "../lib/api";
import { monthName } from "../lib/format";
import { PhotoGrid } from "../components/PhotoGrid";
import { EmptyState, ErrorState, NoLibrary, SkeletonGrid } from "../components/States";
import { useViewer } from "../components/ViewerContext";
import { SelectionBar, SelectToggle, useGridSelect } from "../components/SelectionBar";
import { Slideshow } from "../components/Slideshow";
import { ExportDialog } from "../components/ExportDialog";
import { useTitle, useLocalState } from "../lib/hooks";

export default function Photos() {
  const [params, setParams] = useSearchParams();
  const viewer = useViewer();
  useTitle("Photos");
  const [density, setDensity] = useLocalState<"comfortable" | "compact" | "large">("grid-density", "comfortable");
  const [grouping, setGrouping] = useLocalState<"day" | "month" | "none">("grid-grouping", "day");
  const [showFilters, setShowFilters] = useState(false);
  const [slideshow, setSlideshow] = useState(false);
  const [exporting, setExporting] = useState(false);

  const favorite = params.get("favorite") === "1";
  const source = params.get("source") ?? "";
  const media = params.get("media") ?? "";
  const unfold = params.get("frames") === "all";     // show every file of RAW+JPEG pairs and bursts
  const minRating = Number(params.get("rating")) || 0;
  const order = (params.get("order") as "date_desc" | "date_asc" | "quality") ?? "date_desc";
  // Set by the Timeline's month links. A month without a year means nothing to the API.
  const year = Number(params.get("year")) || undefined;
  const month = year ? Number(params.get("month")) || undefined : undefined;
  const period = year ? (month ? `${monthName(month)} ${year}` : String(year)) : "";
  // Set by the Timeline's month links while it is filtered to one person.
  const person = Number(params.get("person")) || undefined;

  const query = useQuery({
    queryKey: ["photos", { favorite, source, order, year, month, media, unfold, minRating, person }],
    queryFn: () => api.photos({ favorite, source: source || undefined, order, year, month, media: media || undefined, person,
      collapse_stacks: !unfold, min_rating: minRating || undefined, archived: "exclude",
      include_screenshots: source ? true : undefined }),
  });
  const { data: stats } = useQuery({ queryKey: ["stats"], queryFn: api.stats });

  const items = useMemo(() => gridItems(query.data), [query.data]);

  const allIds = useMemo(() => items.map((i) => i.id), [items]);
  const sel = useGridSelect(allIds);

  const targetHeight = density === "compact" ? 150 : density === "large" ? 340 : 230;

  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };

  if (query.isError) return <ErrorState error={query.error} onRetry={() => query.refetch()} />;
  if (!query.isLoading && stats && stats.photos === 0) return <NoLibrary />;

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="display">Photos</h1>
          <p className="dim">
            {query.data ? `${query.data.total.toLocaleString()} photos` : "Loading your library"}
            {period ? ` · ${period}` : ""}
            {media === "video" ? " · videos" : media === "live" ? " · live photos" : ""}
            {favorite ? " · favourites" : ""}
            {source ? ` · ${source}` : ""}
          </p>
          {period && (
            <button className="chip chip-button chip-accent" style={{ marginTop: 8 }}
              onClick={() => {
                const next = new URLSearchParams(params);
                next.delete("year");
                next.delete("month");
                setParams(next, { replace: true });
              }}
              title="Show the whole library">
              {period} <X size={12} />
            </button>
          )}
          {person && (
            <button className="chip chip-button chip-accent" style={{ marginTop: 8, marginLeft: period ? 6 : 0 }}
              onClick={() => setParam("person", null)} title="Show everyone">
              One person <X size={12} />
            </button>
          )}
        </div>
        <div className="toolbar">
          <div className="segmented" role="group" aria-label="Sort order">
            <button className={order === "date_desc" ? "on" : ""} onClick={() => setParam("order", null)}
              title="Newest first"><CalendarDays size={15} /></button>
            <button className={order === "quality" ? "on" : ""} onClick={() => setParam("order", "quality")}
              title="Best first"><Star size={15} /></button>
          </div>
          <div className="segmented" role="group" aria-label="Grid density">
            <button className={density === "compact" ? "on" : ""} onClick={() => setDensity("compact")}
              title="Compact"><LayoutGrid size={15} /></button>
            <button className={density === "comfortable" ? "on" : ""} onClick={() => setDensity("comfortable")}
              title="Comfortable"><Rows3 size={15} /></button>
            <button className={density === "large" ? "on" : ""} onClick={() => setDensity("large")}
              title="Large"><Images size={15} /></button>
          </div>
          {year && !person && (
            <button className="btn btn-ghost btn-sm" onClick={() => setExporting(true)} disabled={!items.length}
              title={`Copy every photo from ${period} somewhere`}>
              <FolderOutput size={14} /> Export {period}
            </button>
          )}
          {exporting && year && <ExportDialog spec={{ year, month }} title={`Export ${period}`}
            onClose={() => setExporting(false)} />}
          <button className="btn btn-ghost btn-sm" onClick={() => setSlideshow(true)} disabled={!items.length}
            title="Slideshow of these photos">
            <MonitorPlay size={14} /> Slideshow
          </button>
          <SelectToggle sel={sel} />
          <button className={`btn btn-ghost btn-icon${showFilters ? " is-on" : ""}`}
            onClick={() => setShowFilters((v) => !v)} aria-label="Filters" title="Filters">
            <SlidersHorizontal size={15} />
          </button>
        </div>
      </div>

      {showFilters && (
        <div className="filter-bar">
          <button className={`chip chip-button${favorite ? " chip-accent" : ""}`}
            onClick={() => setParam("favorite", favorite ? null : "1")}>
            <Heart size={12} /> Favourites
          </button>
          {[1, 3, 5].map((r) => (
            <button key={r} className={`chip chip-button${minRating === r ? " chip-accent" : ""}`}
              onClick={() => setParam("rating", minRating === r ? null : String(r))}>
              {"★".repeat(r)}{r < 5 ? "+" : ""}
            </button>
          ))}
          <button className={`chip chip-button${unfold ? " chip-accent" : ""}`}
            onClick={() => setParam("frames", unfold ? null : "all")}
            title="Stacks fold RAW+JPEG pairs and bursts into one item">
            {unfold ? "every frame" : "stacks folded"}
          </button>
          {[["video", "videos"], ["live", "live photos"]].map(([m, label]) => (
            <button key={m} className={`chip chip-button${media === m ? " chip-accent" : ""}`}
              onClick={() => setParam("media", media === m ? null : m)}>
              {label}
            </button>
          ))}
          {["camera", "phone", "whatsapp", "screenshot", "download", "edited"].map((s) => (
            <button key={s} className={`chip chip-button${source === s ? " chip-accent" : ""}`}
              onClick={() => setParam("source", source === s ? null : s)}>
              {s}
            </button>
          ))}
          <button className={`chip chip-button${grouping === "month" ? " chip-accent" : ""}`}
            onClick={() => setGrouping(grouping === "month" ? "day" : "month")}>
            group by month
          </button>
        </div>
      )}

      <SelectionBar {...sel.barProps} />
      {slideshow && <Slideshow items={items.map((i) => ({ id: i.id, video: (i.flags & FLAG.video) > 0, rot: i.rot }))}
        onClose={() => setSlideshow(false)} />}

      {query.isLoading ? (
        <SkeletonGrid />
      ) : (
        <PhotoGrid
          items={items}
          grouping={grouping}
          targetHeight={targetHeight}
          onOpen={(_, index) => viewer.open(items.map((i) => i.id), index)}
          {...sel.gridProps}
          emptyState={<EmptyState title="No photos match these filters"
            hint="Try clearing the filters or indexing more folders." />}
        />
      )}
    </div>
  );
}
