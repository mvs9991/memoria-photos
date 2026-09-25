/**
 * Insights and Year in Review. Every number is a count from the database — nothing
 * here is estimated — so the page can be trusted the way a statement can.
 */
import { useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Camera, Compass, Film, Globe2, Images, MapPin, Plane, Sparkles, Users } from "lucide-react";
import { api, faceUrl, thumbUrl } from "../lib/api";
import { formatRange, monthName } from "../lib/format";
import { EmptyState, ErrorState, SectionHeader, Spinner } from "../components/States";
import { useViewer } from "../components/ViewerContext";
import { useTitle } from "../lib/hooks";

export default function Insights() {
  const [params, setParams] = useSearchParams();
  const viewer = useViewer();
  const year = Number(params.get("year")) || undefined;
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["insights", year ?? null],
    queryFn: () => api.insights(year),
  });
  useTitle(year ? `${year} in review` : "Insights");
  const labels = useMemo(() => new Map((data?.people ?? []).map((p) => [p.id, p])), [data]);

  if (isError) return <ErrorState error={error} onRetry={() => refetch()} />;
  if (isLoading || !data) return <Spinner full label="Counting" />;
  const t = data.totals;
  const peak = Math.max(1, ...data.months);
  const placeMax = Math.max(1, ...data.places.map((p) => p.photos));

  return (
    <div className="page insights-page">
      <div className="page-head">
        <div>
          <h1 className="display">{year ? `Your ${year}` : "Your library"}</h1>
          <p className="dim">{year ? "The year in photos — counted, not estimated" : "Everything so far, counted from your photos"}</p>
        </div>
      </div>
      <div className="year-chips" role="tablist" aria-label="Year">
        <button className={`chip chip-button${!year ? " chip-accent" : ""}`} onClick={() => setParams({}, { replace: true })}>
          All time
        </button>
        {data.years.map((y) => (
          <button key={y} className={`chip chip-button${year === y ? " chip-accent" : ""}`}
            onClick={() => setParams({ year: String(y) }, { replace: true })}>{y}</button>
        ))}
      </div>

      {t.photos + t.videos === 0 ? (
        <EmptyState icon={<Sparkles size={26} />} title="Nothing dated in this period" />
      ) : (
        <>
          <div className="insight-stats">
            <Stat icon={Images} value={t.photos} label="photos" />
            {t.videos > 0 && <Stat icon={Film} value={t.videos} label={`${t.videos === 1 ? "video" : "videos"} · ${
              t.video_minutes < 1 ? `${Math.round(t.video_minutes * 60)} s` : `${Math.round(t.video_minutes)} min`}`} />}
            <Stat icon={Users} value={t.people} label="people" />
            <Stat icon={MapPin} value={t.places} label="places" />
            <Stat icon={Globe2} value={t.countries} label={t.countries === 1 ? "country" : "countries"} />
            <Stat icon={Plane} value={t.trips} label={t.trips === 1 ? "trip" : "trips"} />
          </div>

          {data.best_photo_ids.length > 0 && (
            <section className="insight-section">
              <SectionHeader title={year ? "Best of the year" : "Your best"} sub="Your stars first, then the quality score — at most two a day" />
              <div className="best-strip">
                {data.best_photo_ids.map((id, i) => (
                  <button key={id} className="best-tile" onClick={() => viewer.open(data.best_photo_ids, i)} aria-label={`Photo ${id}`}>
                    <img src={thumbUrl(id, "m")} alt="" loading="lazy" />
                  </button>
                ))}
              </div>
            </section>
          )}

          <div className="insight-columns">
            <section className="card insight-card">
              <SectionHeader title="Month by month" sub={data.busiest_day
                ? `Busiest day: ${new Date(data.busiest_day.date).toLocaleDateString([], { day: "numeric", month: "long", year: "numeric" })} · ${data.busiest_day.photos} photos`
                : undefined} />
              <div className="month-bars">
                {data.months.map((n, i) => {
                  const bar = <><span className="month-bar" style={{ height: `${(n / peak) * 100}%` }} />
                    <span className="month-label">{monthName(i + 1).slice(0, 3)}</span></>;
                  return year && n > 0 ? (
                    <Link key={i} to={`/photos?year=${year}&month=${i + 1}`} className="month-col" title={`${n.toLocaleString()} photos`}>{bar}</Link>
                  ) : (
                    <div key={i} className="month-col" title={`${n.toLocaleString()} photos`}>{bar}</div>
                  );
                })}
              </div>
            </section>

            {data.places.length > 0 && (
              <section className="card insight-card">
                <SectionHeader title="Where" sub={data.countries.slice(0, 8).join(" · ")} />
                <ul className="bar-list">
                  {data.places.slice(0, 8).map((p) => (
                    <li key={p.id}>
                      <Link to={`/places/${p.id}`} className="ellipsis">{p.city ?? "Unknown"}{p.country ? `, ${p.country}` : ""}</Link>
                      <span className="bar-track"><span style={{ width: `${(p.photos / placeMax) * 100}%` }} /></span>
                      <span className="dim tnum">{p.photos.toLocaleString()}</span>
                    </li>
                  ))}
                </ul>
                {data.furthest_from_home && (
                  <p className="insight-callout"><Compass size={14} /> Furthest from {data.furthest_from_home.home}:{" "}
                    <Link to={`/places/${data.furthest_from_home.place_id}`} className="link">
                      {data.furthest_from_home.city}{data.furthest_from_home.country ? `, ${data.furthest_from_home.country}` : ""}
                    </Link>, {data.furthest_from_home.km.toLocaleString()} km away</p>
                )}
              </section>
            )}
          </div>

          {data.people.length > 0 && (
            <section className="insight-section">
              <SectionHeader title="Who was there" count={data.people.length} />
              <div className="insight-people">
                {data.people.map((p) => (
                  <Link key={p.id} to={`/people/${p.id}`} className="insight-person">
                    {p.cover_face_id ? <img src={faceUrl(p.cover_face_id, 160)} alt="" loading="lazy" /> : <span className="face-blank" />}
                    <span className="ellipsis">{p.label}</span>
                    <span className="dim tnum">{p.photos.toLocaleString()}</span>
                  </Link>
                ))}
              </div>
              {data.constellation.length > 0 && (
                <>
                  <h3 className="insight-sub">Most often together</h3>
                  <div className="pair-list">
                    {data.constellation.slice(0, 6).map((c) => {
                      const a = labels.get(c.a), b = labels.get(c.b);
                      if (!a || !b) return null;
                      return (
                        <Link key={`${c.a}-${c.b}`} to={`/search?q=${encodeURIComponent(`${a.label} and ${b.label}`)}`} className="pair">
                          <span className="pair-faces">
                            {[a, b].map((p) => p.cover_face_id
                              ? <img key={p.id} src={faceUrl(p.cover_face_id, 96)} alt="" loading="lazy" />
                              : <span key={p.id} className="face-blank" />)}
                          </span>
                          <span className="ellipsis">{a.label} & {b.label}</span>
                          <span className="dim tnum">{c.photos}</span>
                        </Link>
                      );
                    })}
                  </div>
                </>
              )}
              {year && data.new_people.length > 0 && (
                <p className="dim insight-callout"><Sparkles size={14} /> New faces this year:{" "}
                  {data.new_people.slice(0, 8).map((p, i) => (
                    <span key={p.id}>{i ? ", " : ""}<Link to={`/people/${p.id}`} className="link">{p.label}</Link></span>
                  ))}{data.new_people.length > 8 ? ` and ${data.new_people.length - 8} more` : ""}</p>
              )}
            </section>
          )}

          {data.trips.length > 0 && (
            <section className="insight-section">
              <SectionHeader title="Trips" count={data.trips.length} />
              <div className="event-grid">
                {data.trips.slice(0, 8).map((tr) => (
                  <Link key={tr.id} to={`/events/${tr.id}`} className="event-tile event-tile-sm">
                    <div className="event-tile-img">
                      {tr.cover_photo_id ? <img src={thumbUrl(tr.cover_photo_id, "m")} alt="" loading="lazy" /> : <div className="event-card-blank" />}
                    </div>
                    <div className="event-tile-body">
                      <h3 className="event-tile-title">{tr.title}</h3>
                      <p className="dim event-tile-sub">{formatRange(tr.start_ts, tr.end_ts)} · {tr.photos.toLocaleString()} photos</p>
                    </div>
                  </Link>
                ))}
              </div>
            </section>
          )}

          <div className="insight-columns">
            {data.tags.length > 0 && (
              <section className="card insight-card">
                <SectionHeader title="What you photographed" />
                <div className="fact-chips">
                  {data.tags.map((tg) => (
                    <Link key={tg.name} to={`/search?q=${encodeURIComponent(tg.name)}${year ? `+in+${year}` : ""}`} className="chip chip-button">
                      {tg.name} <span className="dim tnum">{tg.photos}</span>
                    </Link>
                  ))}
                </div>
              </section>
            )}
            {data.cameras.length > 0 && (
              <section className="card insight-card">
                <SectionHeader title="Cameras" />
                <ul className="bar-list">
                  {data.cameras.slice(0, 6).map((c) => (
                    <li key={c.camera}>
                      <span className="ellipsis"><Camera size={12} className="dim" /> {c.camera || "Unknown"}</span>
                      <span className="dim tnum">{c.photos.toLocaleString()}</span>
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function Stat({ icon: Icon, value, label }: { icon: React.ComponentType<{ size?: number }>; value: number; label: string }) {
  return (
    <div className="insight-stat">
      <Icon size={16} />
      <span className="insight-stat-value tnum">{value.toLocaleString()}</span>
      <span className="dim">{label}</span>
    </div>
  );
}
