import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, FolderOutput, ImageMinus, Link2, Lock, MonitorPlay, Users, Pencil, Search, Star, Trash2, X } from "lucide-react";
import { api, FLAG, gridItems } from "../lib/api";
import { PhotoGrid } from "../components/PhotoGrid";
import { EmptyState, ErrorState, Spinner } from "../components/States";
import { SelectionBar, SelectToggle, useGridSelect } from "../components/SelectionBar";
import { Slideshow } from "../components/Slideshow";
import { HtmlExportDialog, ShareDialog } from "../components/ShareDialog";
import { ExportDialog } from "../components/ExportDialog";
import { useViewer } from "../components/ViewerContext";
import { useRole, useTitle } from "../lib/hooks";

export default function AlbumDetail() {
  const { id } = useParams();
  const albumId = Number(id);
  const qc = useQueryClient();
  const navigate = useNavigate();
  const viewer = useViewer();
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState("");
  const role = useRole();
  const [dialog, setDialog] = useState<"share" | "export" | "web" | "slideshow" | null>(null);

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["album", albumId],
    queryFn: () => api.album(albumId),
  });
  useTitle(data?.name);
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["album", albumId] });
    qc.invalidateQueries({ queryKey: ["albums"] });
  };
  const rename = useMutation({
    mutationFn: () => api.updateAlbum(albumId, { name: name.trim() }),
    onSuccess: () => { setRenaming(false); refresh(); },
  });
  const remove = useMutation({
    mutationFn: (ids: number[]) => api.removeFromAlbum(albumId, ids),
    onSuccess: () => { sel.clear(); refresh(); },
  });
  const cover = useMutation({
    mutationFn: (pid: number) => api.updateAlbum(albumId, { cover_photo_id: pid }),
    onSuccess: () => { sel.clear(); refresh(); },
  });
  const del = useMutation({
    mutationFn: () => api.deleteAlbum(albumId),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["albums"] }); navigate("/albums"); },
  });

  const items = useMemo(() => gridItems(data?.photos), [data]);

  const allIds = useMemo(() => items.map((i) => i.id), [items]);
  const sel = useGridSelect(allIds);

  if (isError) return <ErrorState error={error} onRetry={() => refetch()} />;
  if (isLoading || !data) return <Spinner full label="Loading album" />;

  return (
    <div className="page">
      <Link to="/albums" className="back-link"><ArrowLeft size={15} /> Albums</Link>
      <div className="page-head">
        <div>
          {renaming ? (
            <form className="rename-form" onSubmit={(e) => { e.preventDefault(); if (name.trim()) rename.mutate(); }}>
              <input className="field" autoFocus value={name} onChange={(e) => setName(e.target.value)}
                maxLength={120} aria-label="Album name" />
              <button className="btn btn-primary" type="submit"><Check size={15} /> Save</button>
              <button className="btn btn-quiet" type="button" onClick={() => setRenaming(false)} aria-label="Cancel renaming"><X size={15} /></button>
            </form>
          ) : (
            <h1 className="display person-title">
              {data.name}
              <button className="btn btn-quiet btn-icon btn-sm" title="Rename album"
                onClick={() => { setName(data.name); setRenaming(true); }}><Pencil size={14} /></button>
            </h1>
          )}
          {data.kind === "smart" && <SmartQuery albumId={albumId} query={data.query ?? ""} onSaved={refresh} />}
          <p className="dim">
            {data.photo_count.toLocaleString()} {data.photo_count === 1 ? "item" : "items"}
            {data.kind === "smart" ? " · updates itself as your library changes" : ""}
            {data.source === "takeout" ? " · imported from Google Photos" : ""}
            {data.description ? ` · ${data.description}` : ""}
          </p>
        </div>
        <div className="toolbar">
          <button className="btn btn-ghost btn-sm" onClick={() => setDialog("slideshow")} disabled={!items.length}>
            <MonitorPlay size={14} /> Slideshow
          </button>
          {data.mine && (
            <button className={`btn btn-ghost btn-sm${data.private ? " is-on" : ""}`}
              onClick={() => api.updateAlbum(albumId, { private: !data.private }).then(refresh)}
              title={data.private ? "Only you can see this album — click to share it with the family"
                : "Make this album visible to you only (the photos stay in the shared library)"}>
              {data.private ? <><Lock size={14} /> Private</> : <><Users size={14} /> Family</>}
            </button>
          )}
          {role === "owner" && (
            <button className="btn btn-ghost btn-sm" onClick={() => setDialog("share")}><Link2 size={14} /> Share</button>
          )}
          <button className="btn btn-ghost btn-sm" onClick={() => setDialog("export")} disabled={!items.length}
            title="Copy the original files, or make a web gallery"><FolderOutput size={14} /> Export</button>
          <SelectToggle sel={sel} />
          <button className="btn btn-quiet btn-sm" onClick={() => {
            if (confirm(`Delete the album “${data.name}”?\n\nOnly the album goes — every photo stays in your library.`))
              del.mutate();
          }}><Trash2 size={14} /> Delete album</button>
        </div>
      </div>

      {dialog === "share" && <ShareDialog albumId={albumId} albumName={data.name} onClose={() => setDialog(null)} />}
      {dialog === "export" && (
        <ExportDialog spec={{ album_id: albumId }} title={`Export “${data.name}”`} onClose={() => setDialog(null)}
          footer={<p className="dim">Or <button className="link-button" onClick={() => setDialog("web")}>make a web
            gallery</button> — resized copies with an index.html that opens in any browser.</p>} />
      )}
      {dialog === "web" && <HtmlExportDialog albumId={albumId} albumName={data.name} onClose={() => setDialog(null)} />}
      {dialog === "slideshow" && <Slideshow items={items.map((i) => ({ id: i.id, video: (i.flags & FLAG.video) > 0, rot: i.rot }))}
        onClose={() => setDialog(null)} />}

      <SelectionBar {...sel.barProps}
        extra={<>
        {sel.selected.size === 1 && (
          <button className="btn btn-ghost btn-sm" onClick={() => cover.mutate([...sel.selected][0])}>
            <Star size={14} /> Use as cover
          </button>
        )}
        {data.kind !== "smart" && (
          <button className="btn btn-ghost btn-sm" onClick={() => remove.mutate([...sel.selected])}>
            <ImageMinus size={14} /> Remove from album
          </button>
        )}
      </>} />

      <PhotoGrid items={items} grouping="day" targetHeight={220}
        onOpen={(_, index) => viewer.open(items.map((i) => i.id), index)}
        {...sel.gridProps}
        emptyState={<EmptyState title="This album is empty"
          hint="Select photos anywhere in your library and choose “Add to album”." />} />
    </div>
  );
}

function SmartQuery({ albumId, query, onSaved }: { albumId: number; query: string; onSaved: () => void }) {
  const [text, setText] = useState(query);
  const save = useMutation({ mutationFn: () => api.updateAlbum(albumId, { query: text.trim() }), onSuccess: onSaved });
  return (
    <form className="smart-query-form" onSubmit={(e) => { e.preventDefault(); if (text.trim()) save.mutate(); }}>
      <Search size={14} className="dim" />
      <input className="field field-sm" value={text} onChange={(e) => setText(e.target.value)}
        aria-label="Smart album search" />
      {text.trim() !== query && <button className="btn btn-primary btn-sm" type="submit">Update</button>}
    </form>
  );
}
