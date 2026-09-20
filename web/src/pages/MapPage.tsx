import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Info, MapPin } from "lucide-react";
import { api } from "../lib/api";
import { MiniMap } from "../components/MiniMap";
import { EmptyState, ErrorState, Spinner } from "../components/States";
import { useTitle } from "../lib/hooks";

export default function MapPage() {
  useTitle("Map");
  const navigate = useNavigate();
  const [person, setPerson] = useState<number | null>(null);
  const places = useQuery({ queryKey: ["places"], queryFn: api.places });
  const people = useQuery({ queryKey: ["people", { sort: "photos" }], queryFn: () => api.people({ sort: "photos" }) });
  const points = useQuery({
    queryKey: ["map-points", person],
    queryFn: () => api.mapPoints(person ? { person } : {}),
  });
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });

  const clustered = useMemo(() => {
    if (person) {
      // Individual photo positions for a person query, lightly de-duplicated.
      const seen = new Map<string, { lat: number; lon: number; count: number }>();
      for (const p of points.data?.points ?? []) {
        const key = `${p.lat.toFixed(2)},${p.lon.toFixed(2)}`;
        const hit = seen.get(key);
        if (hit) hit.count += 1;
        else seen.set(key, { lat: p.lat, lon: p.lon, count: 1 });
      }
      return [...seen.values()];
    }
    return (places.data?.places ?? [])
      .filter((p: any) => p.lat != null)
      .map((p: any) => ({ lat: p.lat, lon: p.lon, count: p.photo_count, label: p.name, id: p.id }));
  }, [places.data, points.data, person]);

  if (places.isError) return <ErrorState error={places.error} onRetry={() => places.refetch()} />;
  if (places.isLoading) return <Spinner full label="Loading map" />;
  if (!clustered.length) {
    return <EmptyState icon={<MapPin size={26} />} title="No photos with locations"
      hint="Photos need GPS metadata (or an event with GPS) to appear on the map." />;
  }

  return (
    <div className="page map-page">
      <div className="page-head">
        <div>
          <h1 className="display">Map</h1>
          <p className="dim">{clustered.length.toLocaleString()} locations</p>
        </div>
        <select className="field field-sm" value={person ?? ""} aria-label="Filter by person"
          onChange={(e) => setPerson(e.target.value ? Number(e.target.value) : null)}>
          <option value="">Everyone</option>
          {(people.data?.people ?? []).slice(0, 40).map((p) => (
            <option key={p.id} value={p.id}>{p.label}</option>
          ))}
        </select>
      </div>

      <div className="card map-full">
        <MiniMap points={clustered} height={620} onSelect={(p: any) => p.id && navigate(`/places/${p.id}`)} />
      </div>

      {!settings.data?.settings?.allow_online_map_tiles && (
        <p className="dim map-note">
          <Info size={13} /> Showing the built-in offline map. Detailed street tiles can be enabled in
          Settings — they are off by default because loading them tells the tile server which areas you view.
        </p>
      )}
    </div>
  );
}
