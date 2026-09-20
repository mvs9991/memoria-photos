import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Globe2, MapPin, Users } from "lucide-react";
import { api, thumbUrl } from "../lib/api";
import { EmptyState, ErrorState, Spinner } from "../components/States";
import { MiniMap } from "../components/MiniMap";
import { useTitle } from "../lib/hooks";

export default function Places() {
  useTitle("Places");
  const navigate = useNavigate();
  const { data, isLoading, isError, error, refetch } = useQuery({ queryKey: ["places"], queryFn: api.places });
  const [country, setCountry] = useState<string | null>(null);

  const points = useMemo(
    () => (data?.places ?? [])
      .filter((p: any) => p.lat != null)
      .map((p: any) => ({ lat: p.lat, lon: p.lon, count: p.photo_count, label: p.name, id: p.id })),
    [data],
  );

  if (isError) return <ErrorState error={error} onRetry={() => refetch()} />;
  if (isLoading) return <Spinner full label="Loading places" />;
  if (!data?.places.length) {
    return <EmptyState icon={<MapPin size={26} />} title="No locations yet"
      hint="Places come from GPS in your photos. Photos without GPS can still be placed from the events they belong to." />;
  }

  const hierarchy = data.hierarchy.filter((c: any) => !country || c.country === country);

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="display">Places</h1>
          <p className="dim">
            {data.places.length.toLocaleString()} places across {data.hierarchy.length} countries
          </p>
        </div>
        <Link to="/map" className="btn btn-ghost btn-sm"><Globe2 size={15} /> Open map</Link>
      </div>

      <div className="places-map card">
        <MiniMap points={points} height={280} onSelect={(p) => p.id && navigate(`/places/${p.id}`)} />
      </div>

      {data.hierarchy.length > 1 && (
        <div className="filter-bar">
          <button className={`chip chip-button${!country ? " chip-accent" : ""}`} onClick={() => setCountry(null)}>
            All countries
          </button>
          {data.hierarchy.map((c: any) => (
            <button key={c.country} className={`chip chip-button${country === c.country ? " chip-accent" : ""}`}
              onClick={() => setCountry(c.country)}>
              {c.country} <span className="dim tnum">{c.count.toLocaleString()}</span>
            </button>
          ))}
        </div>
      )}

      {hierarchy.map((c: any) => (
        <section key={c.country} className="place-country">
          <h2 className="place-country-head">
            {c.country} <span className="dim tnum">{c.count.toLocaleString()} photos</span>
          </h2>
          {c.regions.map((r: any) => (
            <div key={r.region} className="place-region">
              <h3 className="place-region-head dim">{r.region}</h3>
              <div className="place-grid">
                {r.places.map((p: any) => (
                  <Link key={p.id} to={`/places/${p.id}`} className="place-tile">
                    <div className="place-tile-img">
                      {p.cover_photo_id ? <img src={thumbUrl(p.cover_photo_id, "m")} alt="" loading="lazy" />
                        : <div className="event-card-blank" />}
                      <div className="event-card-grad" />
                    </div>
                    <div className="place-tile-body">
                      <span className="place-tile-name">{p.name}</span>
                      <span className="place-tile-meta dim">
                        <span className="tnum">{p.photo_count.toLocaleString()}</span> photos
                        {p.event_count > 0 && <> · <span className="tnum">{p.event_count}</span> events</>}
                        {p.people_count > 0 && <> · <Users size={11} /> {p.people_count}</>}
                      </span>
                    </div>
                  </Link>
                ))}
              </div>
            </div>
          ))}
        </section>
      ))}
    </div>
  );
}
