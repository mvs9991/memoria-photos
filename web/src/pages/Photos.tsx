import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { CalendarDays, Heart, Images, LayoutGrid, Rows3, SlidersHorizontal, Star } from "lucide-react";
import { api } from "../lib/api";
import { PhotoGrid } from "../components/PhotoGrid";
import { EmptyState, ErrorState, NoLibrary, SkeletonGrid } from "../components/States";
import { useViewer } from "../components/ViewerContext";
import { useTitle, useLocalState } from "../lib/hooks";

export default function Photos() {
  const [params, setParams] = useSearchParams();
  const viewer = useViewer();
  useTitle("Photos");
  const [density, setDensity] = useLocalState<"comfortable" | "compact" | "large">("grid-density", "comfortable");
  const [grouping, setGrouping] = useLocalState<"day" | "month" | "none">("grid-grouping", "day");
  const [showFilters, setShowFilters] = useState(false);

  const favorite = params.get("favorite") === "1";
  const source = params.get("source") ?? "";
  const order = (params.get("order") as "date_desc" | "date_asc" | "quality") ?? "date_desc";

  const query = useQuery({
    queryKey: ["photos", { favorite, source, order }],
    queryFn: () => api.photos({ favorite, source: source || undefined, order,
      include_screenshots: source ? true : undefined }),
  });
  const { data: stats } = useQuery({ queryKey: ["stats"], queryFn: api.stats });

  const items = useMemo(() => {
    if (!query.data) return [];
    const { ids, ratio, ts, flags } = query.data;
    return ids.map((id, i) => ({ id, ratio: ratio[i], ts: ts[i], flags: flags[i] }));
  }, [query.data]);

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
            {favorite ? " · favourites" : ""}
            {source ? ` · ${source}` : ""}
          </p>
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

      {query.isLoading ? (
        <SkeletonGrid />
      ) : (
        <PhotoGrid
          items={items}
          grouping={grouping}
          targetHeight={targetHeight}
          onOpen={(_, index) => viewer.open(items.map((i) => i.id), index)}
          emptyState={<EmptyState title="No photos match these filters"
            hint="Try clearing the filters or indexing more folders." />}
        />
      )}
    </div>
  );
}
