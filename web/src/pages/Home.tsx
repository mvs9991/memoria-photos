import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowRight, CalendarRange, Images, MapPin, Sparkles, Users } from "lucide-react";
import { api, faceUrl, thumbUrl } from "../lib/api";
import { useViewer } from "../components/ViewerContext";
import { ErrorState, NoLibrary, Spinner } from "../components/States";
import { formatBytes, formatDate } from "../lib/format";
import { useCountUp, useTitle } from "../lib/hooks";

export default function Home() {
  useTitle();
  const viewer = useViewer();
  const stats = useQuery({ queryKey: ["stats"], queryFn: api.stats });
  const memories = useQuery({ queryKey: ["memories"], queryFn: api.memories });

  if (stats.isError) return <ErrorState error={stats.error} onRetry={() => stats.refetch()} />;
  if (stats.isLoading) return <Spinner full label="Opening your library" />;
  if (stats.data && stats.data.photos === 0) return <NoLibrary />;

  const s = stats.data!;
  const sections = memories.data?.sections ?? [];
  const from = s.date_range.from ? new Date(s.date_range.from * 1000).getUTCFullYear() : null;
  const to = s.date_range.to ? new Date(s.date_range.to * 1000).getUTCFullYear() : null;

  return (
    <div className="page home">
      <section className="hero rise">
        <div className="hero-copy">
          <h1 className="display hero-title">Your library, understood.</h1>
          <p className="hero-sub dim">
            {s.photos.toLocaleString()} photos{from && to ? ` spanning ${from}–${to}` : ""} ·
            {" "}{s.people.toLocaleString()} people · {s.events.toLocaleString()} events · {formatBytes(s.bytes)}
          </p>
          <div className="hero-actions">
            <Link to="/search" className="btn btn-primary"><Sparkles size={15} /> Search with words</Link>
            <Link to="/photos" className="btn btn-ghost">Browse all photos <ArrowRight size={15} /></Link>
          </div>
        </div>
        <div className="hero-stats">
          <StatTile icon={<Images size={15} />} label="Photos" value={s.photos} to="/photos" />
          <StatTile icon={<Users size={15} />} label="People" value={s.people} to="/people" />
          <StatTile icon={<CalendarRange size={15} />} label="Events" value={s.events + s.trips} to="/events" />
          <StatTile icon={<MapPin size={15} />} label="Places" value={s.places} to="/places" />
        </div>
      </section>

      {memories.isLoading && <Spinner label="Gathering memories" />}

      {sections.map((section: any) => {
        if (section.kind === "on_this_day" || section.kind === "years_ago") {
          return (
            <section key={section.kind} className="mem-section">
              <div className="section-head">
                <div>
                  <h2>{section.title}</h2>
                  {section.subtitle && <p className="dim section-sub">{section.subtitle}</p>}
                </div>
              </div>
              <div className="mem-groups">
                {section.groups.map((g: any) => (
                  <div key={g.title} className="mem-group">
                    <div className="mem-group-label">{g.title}</div>
                    <div className="mem-strip">
                      {g.photo_ids.map((id: number, i: number) => (
                        <button key={id} className="mem-strip-item"
                          onClick={() => viewer.open(g.photo_ids, i)} aria-label="Open photo">
                          <img src={thumbUrl(id, "sm")} alt="" loading="lazy" />
                        </button>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </section>
          );
        }
        if (section.kind === "people") {
          return (
            <section key={section.kind} className="mem-section">
              <div className="section-head">
                <h2>{section.title}</h2>
                <Link to="/people" className="btn btn-quiet btn-sm">See all <ArrowRight size={14} /></Link>
              </div>
              <div className="face-strip">
                {section.items.map((p: any) => (
                  <Link key={p.id} to={`/people/${p.id}`} className="face-chip">
                    <span className="face-chip-img">
                      {p.cover_face_id ? <img src={faceUrl(p.cover_face_id, 160)} alt="" loading="lazy" />
                        : <Users size={20} className="dim" />}
                    </span>
                    <span className="face-chip-name">{p.title}</span>
                    <span className="face-chip-count dim tnum">{p.photo_count.toLocaleString()}</span>
                  </Link>
                ))}
              </div>
            </section>
          );
        }
        return (
          <section key={section.kind} className="mem-section">
            <div className="section-head">
              <h2>{section.title}</h2>
              <Link to="/events" className="btn btn-quiet btn-sm">See all <ArrowRight size={14} /></Link>
            </div>
            <div className="event-rail">
              {section.items.map((e: any) => (
                <Link key={e.id} to={`/events/${e.id}`} className="event-card">
                  <div className="event-card-img">
                    {e.cover_photo_id ? <img src={thumbUrl(e.cover_photo_id, "m")} alt="" loading="lazy" />
                      : <div className="event-card-blank" />}
                    <div className="event-card-grad" />
                    <div className="event-card-text">
                      <span className="event-card-title">{e.title}</span>
                      <span className="event-card-sub">{e.subtitle} · {e.photo_count.toLocaleString()} photos</span>
                    </div>
                  </div>
                </Link>
              ))}
            </div>
          </section>
        );
      })}

      {s.pending > 0 && (
        <div className="notice">
          {s.pending.toLocaleString()} photos are still waiting to be analysed.{" "}
          <Link to="/settings" className="link">Open settings</Link>
        </div>
      )}
    </div>
  );
}

function StatTile({ icon, label, value, to }: { icon: React.ReactNode; label: string; value: number; to: string }) {
  const n = useCountUp(value);
  return (
    <Link to={to} className="stat-tile">
      <span className="stat-icon">{icon}</span>
      <span className="stat-value tnum">{n.toLocaleString()}</span>
      <span className="stat-label dim">{label}</span>
    </Link>
  );
}
