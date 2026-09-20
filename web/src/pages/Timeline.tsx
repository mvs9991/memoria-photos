import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { CalendarRange, Plane } from "lucide-react";
import { api, thumbUrl } from "../lib/api";
import { EmptyState, ErrorState, Spinner } from "../components/States";
import { monthName } from "../lib/format";
import { useTitle } from "../lib/hooks";

export default function Timeline() {
  useTitle("Timeline");
  const [person, setPerson] = useState<number | null>(null);
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["timeline", person],
    queryFn: () => api.timeline(person ? { person } : {}),
  });
  const people = useQuery({ queryKey: ["people", { sort: "photos" }], queryFn: () => api.people({ sort: "photos" }) });

  const eventsByMonth = useMemo(() => {
    const map = new Map<string, any[]>();
    for (const e of data?.events ?? []) {
      if (e.parent_id) continue;
      const key = `${e.year}-${e.month}`;
      map.set(key, [...(map.get(key) ?? []), e]);
    }
    return map;
  }, [data]);

  if (isError) return <ErrorState error={error} onRetry={() => refetch()} />;
  if (isLoading) return <Spinner full label="Building timeline" />;
  const years = data?.years ?? [];
  if (!years.length) {
    return <EmptyState icon={<CalendarRange size={26} />} title="Nothing on the timeline yet"
      hint="Index some photos with dates to see them here." />;
  }
  const maxMonth = Math.max(1, ...years.flatMap((y: any) => y.months.map((m: any) => m.count)));

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="display">Timeline</h1>
          <p className="dim">{years.length} years · {years.reduce((a: number, y: any) => a + y.count, 0).toLocaleString()} photos</p>
        </div>
        <select className="field field-sm" value={person ?? ""} aria-label="Filter by person"
          onChange={(e) => setPerson(e.target.value ? Number(e.target.value) : null)}>
          <option value="">Everyone</option>
          {(people.data?.people ?? []).slice(0, 40).map((p) => (
            <option key={p.id} value={p.id}>{p.label}</option>
          ))}
        </select>
      </div>

      <div className="timeline">
        {years.map((y: any) => (
          <section key={y.year} className="tl-year">
            <div className="tl-year-head">
              <h2 className="display tl-year-num">{y.year}</h2>
              <span className="dim tnum">{y.count.toLocaleString()} photos</span>
            </div>
            <div className="tl-months">
              {y.months.map((m: any) => {
                const evs = eventsByMonth.get(`${y.year}-${m.month}`) ?? [];
                return (
                  <div key={m.month} className="tl-month">
                    <Link to={`/photos?year=${y.year}&month=${m.month}`} className="tl-month-cover">
                      {m.cover_photo_id ? <img src={thumbUrl(m.cover_photo_id, "sm")} alt="" loading="lazy" />
                        : <div className="event-card-blank" />}
                      <span className="tl-month-bar" style={{ width: `${(m.count / maxMonth) * 100}%` }} />
                    </Link>
                    <div className="tl-month-body">
                      <div className="tl-month-name">{monthName(m.month)}</div>
                      <div className="dim tnum tl-month-count">{m.count.toLocaleString()}</div>
                      {evs.length > 0 && (
                        <div className="tl-events">
                          {evs.slice(0, 4).map((e: any) => (
                            <Link key={e.id} to={`/events/${e.id}`} className="tl-event">
                              {e.kind === "trip" && <Plane size={11} />}
                              <span className="ellipsis">{e.title}</span>
                              <span className="dim tnum">{e.photo_count}</span>
                            </Link>
                          ))}
                          {evs.length > 4 && <span className="dim tl-more">+{evs.length - 4} more</span>}
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
