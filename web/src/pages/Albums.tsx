import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookImage, Plus } from "lucide-react";
import { api, thumbUrl, type Album } from "../lib/api";
import { EmptyState, ErrorState, Spinner } from "../components/States";
import { formatRange } from "../lib/format";
import { useTitle } from "../lib/hooks";

export default function Albums() {
  useTitle("Albums");
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const { data, isLoading, isError, error, refetch } = useQuery({ queryKey: ["albums"], queryFn: api.albums });
  const create = useMutation({
    mutationFn: () => api.createAlbum(name.trim()),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["albums"] });
      navigate(`/albums/${r.id}`);
    },
  });

  if (isError) return <ErrorState error={error} onRetry={() => refetch()} />;
  if (isLoading) return <Spinner full label="Loading albums" />;
  const albums = data?.albums ?? [];
  const mine = albums.filter((a) => a.source === "user" && a.kind !== "smart");
  const smart = albums.filter((a) => a.kind === "smart");
  const imported = albums.filter((a) => a.source === "takeout");
  const icloud = albums.filter((a) => a.source === "icloud");

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="display">Albums</h1>
          <p className="dim">Collections you put together{imported.length ? ", plus albums brought over from Google Photos" : ""}</p>
        </div>
        {creating ? (
          <form className="rename-form" onSubmit={(e) => { e.preventDefault(); if (name.trim()) create.mutate(); }}>
            <input className="field" autoFocus value={name} placeholder="Album name" maxLength={120}
              onChange={(e) => setName(e.target.value)} aria-label="Album name" />
            <button className="btn btn-primary" type="submit" disabled={!name.trim()}>Create</button>
            <button className="btn btn-quiet" type="button" onClick={() => setCreating(false)}>Cancel</button>
          </form>
        ) : (
          <button className="btn btn-primary" onClick={() => setCreating(true)}><Plus size={15} /> New album</button>
        )}
      </div>

      {albums.length === 0 ? (
        <EmptyState icon={<BookImage size={26} />} title="No albums yet"
          hint="Create one here, select photos in any grid and choose “Add to album”, or save a search as a smart album. Albums in a Google Takeout export are imported automatically." />
      ) : (
        <>
          {smart.length > 0 && <AlbumGrid albums={smart} title="Smart albums" />}
          {mine.length > 0 && <AlbumGrid albums={mine} title={imported.length || smart.length ? "Your albums" : undefined} />}
          {imported.length > 0 && <AlbumGrid albums={imported} title="From Google Photos" />}
          {icloud.length > 0 && <AlbumGrid albums={icloud} title="From iCloud" />}
        </>
      )}
    </div>
  );
}

function AlbumGrid({ albums, title }: { albums: Album[]; title?: string }) {
  return (
    <section className="event-year">
      {title && (
        <div className="event-year-head">
          <h2 className="display">{title}</h2>
          <span className="dim tnum">{albums.length}</span>
        </div>
      )}
      <div className="event-grid">
        {albums.map((a) => (
          <Link key={a.id} to={`/albums/${a.id}`} className="event-tile event-tile-md">
            <div className="event-tile-img">
              {a.cover_photo_id ? <img src={thumbUrl(a.cover_photo_id, "m")} alt="" loading="lazy" />
                : <div className="event-card-blank" />}
              <div className="event-card-grad" />
            </div>
            <div className="event-tile-body">
              <h3 className="event-tile-title">{a.name}</h3>
              <p className="dim event-tile-sub">
                {a.kind === "smart" && <span className="smart-query">“{a.query}” · </span>}
                {a.photo_count.toLocaleString()} {a.photo_count === 1 ? "item" : "items"}
                {a.start_ts && a.end_ts ? ` · ${formatRange(a.start_ts, a.end_ts)}` : ""}
              </p>
            </div>
          </Link>
        ))}
      </div>
    </section>
  );
}
