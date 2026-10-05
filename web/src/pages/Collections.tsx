/**
 * Collections: the library by kind of media, plus clean-up queues to review.
 *
 * The clean-up queues are heuristics (blur below a threshold, the tagger's "meme",
 * files over 20 MB, …), not measured detectors — each one is a list to look through,
 * and the only action is *hide*, which leaves the file where it is.
 */
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft, CalendarX, Camera, Clock, Copy, Eye, EyeOff, FileText, Film, Focus, GalleryHorizontal,
  Archive, HardDrive, Layers, MapPinOff, MessageSquareText, Monitor, PawPrint, ScanFace, Sparkle,
} from "lucide-react";
import { api, thumbUrl, type Collection, gridItems } from "../lib/api";
import { PhotoGrid } from "../components/PhotoGrid";
import { EmptyState, ErrorState, Spinner } from "../components/States";
import { SelectionBar, SelectToggle, useGridSelect } from "../components/SelectionBar";
import { useViewer } from "../components/ViewerContext";
import { invalidateGrids, useTitle } from "../lib/hooks";

const ICONS: Record<string, React.ComponentType<{ size?: number }>> = {
  videos: Film, live: Sparkle, panoramas: GalleryHorizontal, selfies: ScanFace, raw: Camera, stacks: Layers,
  screenshots: Monitor, documents: FileText, memes: MessageSquareText, blurry: Focus, large: HardDrive,
  no_location: MapPinOff, no_date: CalendarX, hidden: EyeOff, pets: PawPrint, archive: Archive,
};

const HINTS: Record<string, string> = {
  screenshots: "Screenshots are kept out of the main timeline; review the ones you no longer need.",
  documents: "Receipts, documents and whiteboards the tagger or text reader found.",
  memes: "Images the tagger thinks are memes or forwards. A guess — look before hiding.",
  blurry: "Photos with a low sharpness score. Intentional motion blur and night shots land here too.",
  large: "Files of 20 MB or more, largest first. Hiding does not free space — the file stays on disk.",
  no_location: "No GPS and nothing to place them by. Select some and use “Set place”.",
  no_date: "No reliable date. Select some and use “Fix date”.",
  hidden: "Everything you hid, here and in Duplicates. Select and “Show again” to bring photos back.",
};

export default function Collections() {
  useTitle("Collections");
  const { data, isLoading, isError, error, refetch } = useQuery({ queryKey: ["collections"], queryFn: api.collections });
  if (isError) return <ErrorState error={error} onRetry={() => refetch()} />;
  if (isLoading || !data) return <Spinner full label="Loading collections" />;
  const media = data.collections.filter((c) => c.group === "media");
  const cleanup = data.collections.filter((c) => c.group === "cleanup");

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="display">Collections</h1>
          <p className="dim">Your library by kind, and lists worth a clean-up pass</p>
        </div>
      </div>

      <section className="event-year">
        <div className="event-year-head"><h2 className="display">Media</h2></div>
        <div className="collection-grid">
          <Tile to="/collections/recent" title="Recently added" icon={Clock} cover={data.recently_added_cover} />
          {media.map((c) => <CollectionTile key={c.key} c={c} />)}
        </div>
      </section>

      <section className="event-year">
        <div className="event-year-head">
          <h2 className="display">Clean up</h2>
          <span className="dim">review lists — hiding never deletes a file</span>
        </div>
        <div className="collection-grid">
          <Tile to="/duplicates" title="Duplicates" icon={Copy} count={data.duplicate_groups} countLabel="groups" />
          {cleanup.map((c) => <CollectionTile key={c.key} c={c} />)}
        </div>
      </section>
    </div>
  );
}

function CollectionTile({ c }: { c: Collection }) {
  return <Tile to={`/collections/${c.key}`} title={c.title} icon={ICONS[c.key] ?? Eye} cover={c.cover_photo_id}
    count={c.count} />;
}

function Tile({ to, title, icon: Icon, cover, count, countLabel }: {
  to: string; title: string; icon: React.ComponentType<{ size?: number }>; cover?: number | null;
  count?: number; countLabel?: string;
}) {
  const empty = count === 0;
  return (
    <Link to={to} className={`collection-tile${empty ? " is-empty" : ""}`}>
      <div className="collection-img">
        {cover ? <img src={thumbUrl(cover, "m")} alt="" loading="lazy" /> : <div className="collection-blank"><Icon size={26} /></div>}
      </div>
      <div className="collection-body">
        <span className="collection-icon"><Icon size={15} /></span>
        <span className="collection-title">{title}</span>
        {count !== undefined && (
          <span className="dim tnum collection-count">{empty ? "none" : `${count.toLocaleString()}${countLabel ? ` ${countLabel}` : ""}`}</span>
        )}
      </div>
    </Link>
  );
}

export function CollectionDetail() {
  const { key = "" } = useParams();
  const qc = useQueryClient();
  const viewer = useViewer();
  const list = useQuery({ queryKey: ["collections"], queryFn: api.collections });
  const recent = key === "recent";
  const spec = list.data?.collections.find((c) => c.key === key);
  const title = recent ? "Recently added" : spec?.title ?? "";
  useTitle(title || "Collection");
  const cleanup = spec?.group === "cleanup";
  const hiddenView = key === "hidden";

  const params = recent ? { order: "added", limit: 2000 } : { collection: key, order: key === "large" ? "size" : undefined };
  const query = useQuery({ queryKey: ["photos", params], queryFn: () => api.photos(params) });
  const items = useMemo(() => gridItems(query.data), [query.data]);
  const allIds = useMemo(() => items.map((i) => i.id), [items]);
  const sel = useGridSelect(allIds);

  const hide = useMutation({
    mutationFn: (ids: number[]) => api.hide(ids, !hiddenView),
    onSuccess: () => {
      sel.clear();
      invalidateGrids(qc);
      qc.invalidateQueries({ queryKey: ["collections"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
    },
  });

  if (query.isError) return <ErrorState error={query.error} onRetry={() => query.refetch()} />;
  if (query.isLoading) return <Spinner full label="Loading" />;

  return (
    <div className="page">
      <Link to="/collections" className="back-link"><ArrowLeft size={15} /> Collections</Link>
      <div className="page-head">
        <div>
          <h1 className="display">{title}</h1>
          <p className="dim">
            {recent ? `The last ${items.length.toLocaleString()} items indexed, newest first`
              : `${(query.data?.total ?? 0).toLocaleString()} items`}
          </p>
          {HINTS[key] && <p className="dim collection-hint">{HINTS[key]}</p>}
        </div>
        <div className="toolbar">
          <SelectToggle sel={sel} />
        </div>
      </div>

      <SelectionBar {...sel.barProps}
        extra={(cleanup || hiddenView) && (
          <button className="btn btn-ghost btn-sm" onClick={() => hide.mutate([...sel.selected])}
            title={hiddenView ? "Back into every view" : "Out of every view — the files are not touched"}>
            {hiddenView ? <><Eye size={14} /> Show again</> : <><EyeOff size={14} /> Hide</>}
          </button>
        )} />

      <PhotoGrid items={items} grouping={recent || key === "large" ? "none" : "month"} targetHeight={210}
        onOpen={(_, index) => viewer.open(allIds, index)}
        {...sel.gridProps}
        emptyState={<EmptyState title={hiddenView ? "Nothing is hidden" : "Nothing here"}
          hint={cleanup ? "Nothing to review in this list right now." : undefined} />} />
    </div>
  );
}
