import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, CheckSquare, ImageMinus, Pencil, Search, Star, Trash2, X } from "lucide-react";
import { api } from "../lib/api";
import { PhotoGrid } from "../components/PhotoGrid";
import { EmptyState, ErrorState, Spinner } from "../components/States";
import { SelectionBar, useSelection } from "../components/SelectionBar";
import { useViewer } from "../components/ViewerContext";
import { useTitle } from "../lib/hooks";

export default function AlbumDetail() {
  const { id } = useParams();
  const albumId = Number(id);
  const qc = useQueryClient();
  const navigate = useNavigate();
  const viewer = useViewer();
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState("");
  const [selecting, setSelecting] = useState(false);
  const selection = useSelection();

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
    onSuccess: () => { selection.clear(); refresh(); },
  });
  const cover = useMutation({
    mutationFn: (pid: number) => api.updateAlbum(albumId, { cover_photo_id: pid }),
    onSuccess: () => { selection.clear(); refresh(); },
  });
  const del = useMutation({
    mutationFn: () => api.deleteAlbum(albumId),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["albums"] }); navigate("/albums"); },
  });

  const items = useMemo(() => {
    if (!data?.photos) return [];
    const { ids, ratio, ts, flags, dur, rating } = data.photos;
    return ids.map((pid, i) => ({ id: pid, ratio: ratio[i], ts: ts[i], flags: flags[i], dur: dur?.[i] ?? 0,
      rating: rating?.[i] ?? 0 }));
  }, [data]);

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
              <button className="btn btn-quiet" type="button" onClick={() => setRenaming(false)}><X size={15} /></button>
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
          <button className={`btn btn-ghost btn-sm${selecting ? " is-on" : ""}`}
            onClick={() => { setSelecting((v) => !v); selection.clear(); }}>
            <CheckSquare size={14} /> {selecting ? "Done" : "Select"}
          </button>
          <button className="btn btn-quiet btn-sm" onClick={() => {
            if (confirm(`Delete the album “${data.name}”?\n\nOnly the album goes — every photo stays in your library.`))
              del.mutate();
          }}><Trash2 size={14} /> Delete album</button>
        </div>
      </div>

      <SelectionBar selected={selection.selected} onClear={selection.clear} extra={<>
        {selection.selected.size === 1 && (
          <button className="btn btn-ghost btn-sm" onClick={() => cover.mutate([...selection.selected][0])}>
            <Star size={14} /> Use as cover
          </button>
        )}
        {data.kind !== "smart" && (
          <button className="btn btn-ghost btn-sm" onClick={() => remove.mutate([...selection.selected])}>
            <ImageMinus size={14} /> Remove from album
          </button>
        )}
      </>} />

      <PhotoGrid items={items} grouping="day" targetHeight={220}
        onOpen={(_, index) => viewer.open(items.map((i) => i.id), index)}
        selectable selectMode={selecting} selection={selection.selected} onToggleSelect={selection.toggle}
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
