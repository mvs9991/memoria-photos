import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { CalendarRange, MapPin, Search, Sparkles, Tag, User, Wand2 } from "lucide-react";
import { api, faceUrl, thumbUrl } from "../lib/api";
import { PhotoGrid } from "../components/PhotoGrid";
import { EmptyState, ErrorState, SectionHeader, Spinner } from "../components/States";
import { useViewer } from "../components/ViewerContext";
import { useTitle } from "../lib/hooks";
import { EventCard } from "./Events";

const CHIP_ICON: Record<string, any> = {
  person: User, place: MapPin, event: CalendarRange, tag: Tag, visual: Sparkles, date: CalendarRange,
  filter: Wand2, sort: Wand2, keyword: Search,
};

const IDEAS = [
  "photos of Ghat", "Ghat and Priya together", "beach photos", "wedding", "show my trips",
  "best photos from 2024", "photos taken in Hyderabad", "temple", "birthday cake", "screenshots",
];

export default function SearchPage() {
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const [input, setInput] = useState(q);
  const viewer = useViewer();
  useTitle(q ? `${q} · Search` : "Search");

  useEffect(() => setInput(q), [q]);

  const { data, isFetching, isError, error, refetch } = useQuery({
    queryKey: ["search", q],
    queryFn: () => api.search(q, { limit: 1000 }),
    enabled: q.trim().length > 0,
  });

  const items = useMemo(
    () => (data?.photos ?? []).map((p) => ({ id: p.id, ratio: p.ratio, ts: p.ts, score: p.score })),
    [data],
  );

  const submit = (text: string) => {
    if (!text.trim()) return;
    setParams({ q: text.trim() });
  };

  return (
    <div className="page search-page">
      <div className="search-hero">
        <h1 className="display">Ask your library</h1>
        <p className="dim">
          Combine people, places, dates and what a photo looks like — “Ghat at a wedding in 2024”.
        </p>
        <form className="search-big" onSubmit={(e) => { e.preventDefault(); submit(input); }}>
          <Search size={19} className="dim" />
          <input autoFocus value={input} onChange={(e) => setInput(e.target.value)}
            placeholder="Search photos, people, places, events…" aria-label="Search query" />
          <button className="btn btn-primary" type="submit">Search</button>
        </form>
        {!q && (
          <div className="search-ideas">
            {IDEAS.map((idea) => (
              <button key={idea} className="chip chip-button" onClick={() => submit(idea)}>{idea}</button>
            ))}
          </div>
        )}
      </div>

      {isError && <ErrorState error={error} onRetry={() => refetch()} />}
      {q && isFetching && <Spinner label="Searching" />}

      {data && !isFetching && (
        <>
          <div className="search-interpretation">
            <div className="chips">
              {data.interpretation.map((c, i) => {
                const Icon = CHIP_ICON[c.kind] ?? Sparkles;
                return (
                  <span key={i} className="chip chip-accent" title={c.detail ?? undefined}>
                    <Icon size={12} /> {c.label}
                  </span>
                );
              })}
              {data.interpretation.length === 0 && (
                <span className="chip">no filters matched — searched visually</span>
              )}
            </div>
            <span className="dim search-timing tnum">
              {data.total.toLocaleString()} results · {data.took_ms} ms
            </span>
          </div>

          {data.people?.length > 0 && (
            <section>
              <SectionHeader title="People" />
              <div className="face-strip">
                {data.people.map((p: any) => (
                  <Link key={p.id} to={`/people/${p.id}`} className="face-chip">
                    <span className="face-chip-img">
                      {p.cover_face_id ? <img src={faceUrl(p.cover_face_id, 160)} alt="" /> : <User size={18} />}
                    </span>
                    <span className="face-chip-name">{p.label}</span>
                    <span className="face-chip-count dim tnum">{(p.matched ?? p.photo_count)?.toLocaleString()}</span>
                  </Link>
                ))}
              </div>
            </section>
          )}

          {data.events?.length > 0 && (
            <section>
              <SectionHeader title="Events" count={data.events.length} />
              <div className="event-grid">
                {data.events.map((e: any) => (
                  <EventCard key={e.id} size="sm" event={{
                    ...e,
                    date_label: e.matched ? `${e.matched} matching photos` : (e.date_label ?? ""),
                    place: e.place ?? null,
                    people_count: e.people_count ?? 0,
                  }} />
                ))}
              </div>
            </section>
          )}

          {data.places?.length > 0 && (
            <section>
              <SectionHeader title="Places" count={data.places.length} />
              <div className="chips">
                {data.places.map((p: any) => (
                  <Link key={p.id} to={`/places/${p.id}`} className="chip chip-button">
                    <MapPin size={12} /> {p.name} <span className="dim tnum">{p.matched}</span>
                  </Link>
                ))}
              </div>
            </section>
          )}

          {items.length > 0 ? (
            <section>
              <SectionHeader title="Photos" count={items.length} />
              <PhotoGrid items={items} grouping="none" targetHeight={240} scrubber={false}
                onOpen={(_, index) => viewer.open(items.map((i) => i.id), index)} />
            </section>
          ) : (
            data.events.length === 0 && data.people.length === 0 && (
              <EmptyState title={`Nothing found for “${q}”`}
                hint="Try a person's name, a place, a year, or what the photo looks like — for example “beach”, “cake”, “temple”." />
            )
          )}
        </>
      )}
    </div>
  );
}
