import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { CalendarRange, MapPin, Plane, Users } from "lucide-react";
import { api, thumbUrl } from "../lib/api";
import { EmptyState, ErrorState, Spinner } from "../components/States";
import { useTitle } from "../lib/hooks";

export default function Events() {
  useTitle("Events");
  const [kind, setKind] = useState<"all" | "event" | "trip">("all");
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["events", kind],
    queryFn: () => api.events(kind === "all" ? {} : { kind }),
  });

  const grouped = useMemo(() => {
    const byYear = new Map<number, any[]>();
    for (const e of data?.events ?? []) {
      if (kind === "all" && e.parent_id) continue; // trips already show their children
      const list = byYear.get(e.year) ?? [];
      list.push(e);
      byYear.set(e.year, list);
    }
    return [...byYear.entries()].sort((a, b) => b[0] - a[0]);
  }, [data, kind]);

  if (isError) return <ErrorState error={error} onRetry={() => refetch()} />;
  if (isLoading) return <Spinner full label="Loading events" />;

  const total = data?.events.length ?? 0;

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="display">Events</h1>
          <p className="dim">Moments grouped automatically from time, place, people and scenery</p>
        </div>
        <div className="segmented" role="group" aria-label="Event type">
          <button className={kind === "all" ? "on" : ""} onClick={() => setKind("all")}>All</button>
          <button className={kind === "trip" ? "on" : ""} onClick={() => setKind("trip")}>Trips</button>
          <button className={kind === "event" ? "on" : ""} onClick={() => setKind("event")}>Events</button>
        </div>
      </div>

      {total === 0 ? (
        <EmptyState icon={<CalendarRange size={26} />} title="No events yet"
          hint="Events appear once photos with dates are indexed." />
      ) : (
        grouped.map(([year, events]) => (
          <section key={year} className="event-year">
            <div className="event-year-head">
              <h2 className="display">{year}</h2>
              <span className="dim tnum">{events.length} events</span>
            </div>
            <div className="event-grid">
              {events.map((e: any) => <EventCard key={e.id} event={e} />)}
            </div>
          </section>
        ))
      )}
    </div>
  );
}

export function EventCard({ event: e, size = "md" }: { event: any; size?: "md" | "sm" }) {
  return (
    <Link to={`/events/${e.id}`} className={`event-tile event-tile-${size}`}>
      <div className="event-tile-img">
        {e.cover_photo_id ? (
          <img src={thumbUrl(e.cover_photo_id, "m")} alt="" loading="lazy" />
        ) : (
          <div className="event-card-blank" />
        )}
        <div className="event-card-grad" />
        {e.kind === "trip" && <span className="event-tile-badge"><Plane size={12} /> Trip</span>}
      </div>
      <div className="event-tile-body">
        <h3 className="event-tile-title">{e.title}</h3>
        <p className="dim event-tile-sub">{e.date_label}</p>
        <div className="event-tile-meta dim">
          <span className="tnum">{e.photo_count.toLocaleString()} photos</span>
          {e.people_count > 0 && <span><Users size={12} /> {e.people_count}</span>}
          {e.place && <span className="ellipsis"><MapPin size={12} /> {e.place.city ?? e.place.label}</span>}
        </div>
      </div>
    </Link>
  );
}
