import { useMemo } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, MapPin, Users } from "lucide-react";
import { api, faceUrl } from "../lib/api";
import { PhotoGrid } from "../components/PhotoGrid";
import { ErrorState, SectionHeader, Spinner } from "../components/States";
import { MiniMap } from "../components/MiniMap";
import { useViewer } from "../components/ViewerContext";
import { EventCard } from "./Events";
import { useTitle } from "../lib/hooks";

export default function PlaceDetail() {
  const { id } = useParams();
  const placeId = Number(id);
  const viewer = useViewer();
  const place = useQuery({ queryKey: ["place", placeId], queryFn: () => api.place(placeId) });
  const photos = useQuery({
    queryKey: ["photos", { place: placeId }],
    queryFn: () => api.photos({ place: placeId }),
  });
  useTitle(place.data?.name);

  const items = useMemo(() => {
    if (!photos.data) return [];
    const { ids, ratio, ts, flags } = photos.data;
    return ids.map((pid, i) => ({ id: pid, ratio: ratio[i], ts: ts[i], flags: flags[i] }));
  }, [photos.data]);

  if (place.isError) return <ErrorState error={place.error} onRetry={() => place.refetch()} />;
  if (place.isLoading || !place.data) return <Spinner full label="Loading place" />;
  const p = place.data;

  return (
    <div className="page">
      <Link to="/places" className="back-link"><ArrowLeft size={15} /> Places</Link>
      <div className="page-head">
        <div>
          <h1 className="display">{p.name}</h1>
          <p className="dim"><MapPin size={13} /> {p.label} · {p.photo_count.toLocaleString()} photos</p>
        </div>
      </div>

      {p.lat != null && (
        <div className="card places-map">
          <MiniMap points={[{ lat: p.lat, lon: p.lon, label: p.name }]} height={200} zoomOverride={11} />
        </div>
      )}

      {p.people?.length > 0 && (
        <section>
          <SectionHeader title="People seen here" count={p.people.length} />
          <div className="face-strip">
            {p.people.map((c: any) => (
              <Link key={c.id} to={`/people/${c.id}`} className="face-chip face-chip-sm">
                <span className="face-chip-img">
                  {c.cover_face_id ? <img src={faceUrl(c.cover_face_id, 120)} alt="" loading="lazy" />
                    : <Users size={16} />}
                </span>
                <span className="face-chip-name">{c.label}</span>
                <span className="face-chip-count dim tnum">{c.count}</span>
              </Link>
            ))}
          </div>
        </section>
      )}

      {p.events?.length > 0 && (
        <section>
          <SectionHeader title="Events here" count={p.events.length} />
          <div className="event-grid">
            {p.events.map((e: any) => <EventCard key={e.id} event={e} size="sm" />)}
          </div>
        </section>
      )}

      <section>
        <SectionHeader title="Photos" count={items.length} />
        {photos.isLoading ? <Spinner label="Loading photos" /> : (
          <PhotoGrid items={items} grouping="month" targetHeight={210}
            onOpen={(_, index) => viewer.open(items.map((i) => i.id), index)} />
        )}
      </section>
    </div>
  );
}
