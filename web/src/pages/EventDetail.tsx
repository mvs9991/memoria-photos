import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, MapPin, Pencil, Sparkles, Users, X } from "lucide-react";
import { api, faceUrl, thumbUrl } from "../lib/api";
import { PhotoGrid } from "../components/PhotoGrid";
import { ErrorState, SectionHeader, Spinner } from "../components/States";
import { useViewer } from "../components/ViewerContext";
import { MiniMap } from "../components/MiniMap";
import { useTitle } from "../lib/hooks";

export default function EventDetail() {
  const { id } = useParams();
  const eventId = Number(id);
  const qc = useQueryClient();
  const viewer = useViewer();
  const [renaming, setRenaming] = useState(false);
  const [title, setTitle] = useState("");

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["event", eventId],
    queryFn: () => api.event(eventId),
  });
  useTitle(data?.title);

  const rename = useMutation({
    mutationFn: (value: string | null) => api.renameEvent(eventId, value),
    onSuccess: () => {
      setRenaming(false);
      qc.invalidateQueries({ queryKey: ["event", eventId] });
      qc.invalidateQueries({ queryKey: ["events"] });
    },
  });

  const items = useMemo(() => {
    if (!data?.photos) return [];
    return data.photos.ids.map((pid: number, i: number) => ({
      id: pid, ratio: data.photos.ratio[i], ts: data.photos.ts[i],
    }));
  }, [data]);

  if (isError) return <ErrorState error={error} onRetry={() => refetch()} />;
  if (isLoading || !data) return <Spinner full label="Loading event" />;

  return (
    <div className="page event-page">
      <Link to="/events" className="back-link"><ArrowLeft size={15} /> Events</Link>

      <header className="event-hero">
        {data.cover_photo_id && (
          <div className="event-hero-bg">
            <img src={thumbUrl(data.cover_photo_id, "l")} alt="" />
          </div>
        )}
        <div className="event-hero-inner">
          {renaming ? (
            <form className="rename-form" onSubmit={(e) => { e.preventDefault(); rename.mutate(title.trim() || null); }}>
              <input autoFocus className="field" value={title} onChange={(e) => setTitle(e.target.value)}
                placeholder={data.auto_title} aria-label="Event title" />
              <button className="btn btn-primary" type="submit"><Check size={15} /> Save</button>
              <button className="btn btn-quiet" type="button" onClick={() => setRenaming(false)}><X size={15} /></button>
            </form>
          ) : (
            <h1 className="display event-hero-title">
              {data.title}
              <button className="btn btn-quiet btn-icon btn-sm"
                onClick={() => { setTitle(data.user_title ?? ""); setRenaming(true); }} title="Rename event">
                <Pencil size={14} />
              </button>
            </h1>
          )}
          <p className="event-hero-sub">
            {data.date_label} · {data.photo_count.toLocaleString()} photos
            {data.place && <> · <Link to={`/places/${data.place.id}`} className="link">{data.place.label}</Link></>}
            {data.location_confidence && data.location_confidence !== "high" && (
              <span className="chip chip-sm" style={{ marginLeft: 8 }}>
                location {data.location_confidence}
              </span>
            )}
          </p>
          {data.summary && <p className="event-summary">{data.summary}</p>}
        </div>
      </header>

      <div className="event-facts">
        {data.people?.length > 0 && (
          <div className="fact-card">
            <div className="fact-head"><Users size={13} /> Who was there</div>
            <div className="face-strip">
              {data.people.map((p: any) => (
                <Link key={p.id} to={`/people/${p.id}`} className="face-chip face-chip-sm">
                  <span className="face-chip-img">
                    {p.cover_face_id ? <img src={faceUrl(p.cover_face_id, 120)} alt="" loading="lazy" />
                      : <Users size={16} />}
                  </span>
                  <span className="face-chip-name">{p.label}</span>
                  <span className="face-chip-count dim tnum">{p.count}</span>
                </Link>
              ))}
            </div>
          </div>
        )}
        {data.tags?.length > 0 && (
          <div className="fact-card">
            <div className="fact-head"><Sparkles size={13} /> Scenes</div>
            <div className="fact-chips">
              {data.tags.map((t: any) => (
                <Link key={t.name} to={`/search?q=${encodeURIComponent(t.name)}`} className="chip chip-button">
                  {t.name}
                </Link>
              ))}
            </div>
          </div>
        )}
        {data.map_points?.length > 0 && (
          <div className="fact-card fact-card-map">
            <div className="fact-head"><MapPin size={13} /> Where</div>
            <MiniMap points={data.map_points} height={160} />
          </div>
        )}
      </div>

      {data.children?.length > 0 && (
        <section>
          <SectionHeader title="Days" count={data.children.length} />
          <div className="event-rail">
            {data.children.map((c: any) => (
              <Link key={c.id} to={`/events/${c.id}`} className="event-card event-card-sm">
                <div className="event-card-img">
                  {c.cover_photo_id ? <img src={thumbUrl(c.cover_photo_id, "m")} alt="" loading="lazy" />
                    : <div className="event-card-blank" />}
                  <div className="event-card-grad" />
                  <div className="event-card-text">
                    <span className="event-card-title">{c.title}</span>
                    <span className="event-card-sub">{c.date_label} · {c.photo_count} photos</span>
                  </div>
                </div>
              </Link>
            ))}
          </div>
        </section>
      )}

      {data.highlights?.length > 0 && (
        <section>
          <SectionHeader title="Highlights" sub="The sharpest, best-exposed moments from this event" />
          <div className="highlight-strip">
            {data.highlights.map((pid: number, i: number) => (
              <button key={pid} className="highlight-item"
                onClick={() => viewer.open(data.highlights, i)} aria-label="Open photo">
                <img src={thumbUrl(pid, "m")} alt="" loading="lazy" />
              </button>
            ))}
          </div>
        </section>
      )}

      <section>
        <SectionHeader title="All photos" count={items.length} />
        <PhotoGrid items={items} grouping="day" targetHeight={220}
          onOpen={(_, index) => viewer.open(items.map((i: any) => i.id), index)} />
      </section>
    </div>
  );
}
