import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft, Check, EyeOff, MapPin, Pencil, Scissors, Star, UserCheck, UserX, Users, X,
} from "lucide-react";
import { api, faceUrl, thumbUrl } from "../lib/api";
import { PhotoGrid } from "../components/PhotoGrid";
import { EmptyState, ErrorState, SectionHeader, Spinner } from "../components/States";
import { useViewer } from "../components/ViewerContext";
import { formatDate } from "../lib/format";
import { useTitle } from "../lib/hooks";

export default function PersonDetail() {
  const { id } = useParams();
  const personId = Number(id);
  const qc = useQueryClient();
  const navigate = useNavigate();
  const viewer = useViewer();
  const [tab, setTab] = useState<"photos" | "review">("photos");
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState("");
  const [selected, setSelected] = useState<Set<number>>(new Set());

  const person = useQuery({ queryKey: ["person", personId], queryFn: () => api.person(personId) });
  const photos = useQuery({
    queryKey: ["photos", { person: personId }],
    queryFn: () => api.photos({ person: [personId] }),
  });
  const faces = useQuery({
    queryKey: ["person-faces", personId],
    queryFn: () => api.personFaces(personId, { limit: 400, order: "confidence" }),
    enabled: tab === "review",
  });
  useTitle(person.data?.label);

  const rename = useMutation({
    mutationFn: (value: string | null) => api.renamePerson(personId, value),
    onSuccess: () => {
      setRenaming(false);
      qc.invalidateQueries({ queryKey: ["person", personId] });
      qc.invalidateQueries({ queryKey: ["people"] });
    },
  });
  const flags = useMutation({
    mutationFn: (body: any) => api.personFlags(personId, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["person", personId] });
      qc.invalidateQueries({ queryKey: ["people"] });
    },
  });
  const split = useMutation({
    mutationFn: (faceIds: number[]) => api.splitPerson(personId, faceIds),
    onSuccess: (res: any) => {
      setSelected(new Set());
      qc.invalidateQueries();
      if (res?.created) navigate(`/people/${res.created}`);
    },
  });
  const reject = useMutation({
    mutationFn: (faceIds: number[]) => api.rejectFaces(faceIds, personId),
    onSuccess: () => {
      setSelected(new Set());
      qc.invalidateQueries();
    },
  });
  const confirm = useMutation({
    mutationFn: (faceIds: number[]) => api.assignFaces(faceIds, personId),
    onSuccess: () => {
      setSelected(new Set());
      qc.invalidateQueries({ queryKey: ["person-faces", personId] });
    },
  });

  const items = useMemo(() => {
    if (!photos.data) return [];
    const { ids, ratio, ts, flags: fl } = photos.data;
    return ids.map((pid, i) => ({ id: pid, ratio: ratio[i], ts: ts[i], flags: fl[i] }));
  }, [photos.data]);

  if (person.isError) return <ErrorState error={person.error} onRetry={() => person.refetch()} />;
  if (person.isLoading || !person.data) return <Spinner full label="Loading person" />;
  const p = person.data;

  const yearsMax = Math.max(1, ...(p.years ?? []).map((y: any) => y.count));

  return (
    <div className="page person-page">
      <Link to="/people" className="back-link"><ArrowLeft size={15} /> People</Link>

      <header className="person-hero">
        <div className="person-hero-face">
          {p.cover_face_id ? <img src={faceUrl(p.cover_face_id, 320)} alt="" />
            : <span className="person-face-blank"><Users size={30} /></span>}
        </div>
        <div className="person-hero-main">
          {renaming ? (
            <form className="rename-form" onSubmit={(e) => { e.preventDefault(); rename.mutate(name.trim() || null); }}>
              <input autoFocus className="field" value={name} placeholder="Name this person"
                onChange={(e) => setName(e.target.value)} aria-label="Person name" />
              <button className="btn btn-primary" type="submit"><Check size={15} /> Save</button>
              <button className="btn btn-quiet" type="button" onClick={() => setRenaming(false)}>
                <X size={15} />
              </button>
            </form>
          ) : (
            <h1 className="display person-title">
              {p.label}
              <button className="btn btn-quiet btn-icon btn-sm" title="Rename"
                onClick={() => { setName(p.name ?? ""); setRenaming(true); }}>
                <Pencil size={14} />
              </button>
            </h1>
          )}
          <p className="dim person-meta">
            {p.photo_count.toLocaleString()} photos · {p.face_count.toLocaleString()} faces
            {p.first_seen_ts && ` · first seen ${formatDate(p.first_seen_ts)}`}
            {p.last_seen_ts && ` · last ${formatDate(p.last_seen_ts)}`}
          </p>
          {p.confidence != null && (
            <p className="dim person-meta">
              Grouping confidence {Math.round(p.confidence * 100)}%
              {p.confidence < 0.55 && " — worth reviewing"}
            </p>
          )}
          <div className="person-actions">
            <button className={`btn btn-ghost btn-sm${p.is_me ? " is-on" : ""}`}
              onClick={() => flags.mutate({ is_me: !p.is_me })} title="Mark as yourself">
              <UserCheck size={14} /> {p.is_me ? "This is you" : "This is me"}
            </button>
            <button className="btn btn-ghost btn-sm" onClick={() => flags.mutate({ hidden: !p.hidden })}>
              <EyeOff size={14} /> {p.hidden ? "Unhide" : "Hide"}
            </button>
            <button className="btn btn-ghost btn-sm" onClick={() => flags.mutate({ ignored: !p.ignored })}
              title="Exclude from People and search">
              <UserX size={14} /> {p.ignored ? "Un-ignore" : "Ignore"}
            </button>
          </div>
        </div>
      </header>

      <div className="person-facts">
        {p.co_occurring?.length > 0 && (
          <div className="fact-card">
            <div className="fact-head">Often with</div>
            <div className="face-strip">
              {p.co_occurring.map((c: any) => (
                <Link key={c.id} to={`/people/${c.id}`} className="face-chip face-chip-sm">
                  <span className="face-chip-img">
                    {c.cover_face_id ? <img src={faceUrl(c.cover_face_id, 120)} alt="" loading="lazy" />
                      : <Users size={16} />}
                  </span>
                  <span className="face-chip-name">{c.label}</span>
                  <span className="face-chip-count dim tnum">{c.shared_photos}</span>
                </Link>
              ))}
            </div>
          </div>
        )}
        {p.places?.length > 0 && (
          <div className="fact-card">
            <div className="fact-head"><MapPin size={13} /> Places</div>
            <div className="fact-chips">
              {p.places.slice(0, 8).map((pl: any) => (
                <Link key={pl.id} to={`/places/${pl.id}`} className="chip chip-button">
                  {pl.name} <span className="dim tnum">{pl.count}</span>
                </Link>
              ))}
            </div>
          </div>
        )}
        {p.years?.length > 0 && (
          <div className="fact-card">
            <div className="fact-head">Over time</div>
            <div className="spark">
              {p.years.map((y: any) => (
                <div key={y.year} className="spark-bar" title={`${y.year}: ${y.count} photos`}>
                  <span style={{ height: `${Math.max(6, (y.count / yearsMax) * 100)}%` }} />
                  <em className="dim">{String(y.year).slice(2)}</em>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {p.events?.length > 0 && (
        <section>
          <SectionHeader title="Events" count={p.events.length} />
          <div className="event-rail">
            {p.events.slice(0, 12).map((e: any) => (
              <Link key={e.id} to={`/events/${e.id}`} className="event-card event-card-sm">
                <div className="event-card-img">
                  {e.cover_photo_id ? <img src={thumbUrl(e.cover_photo_id, "m")} alt="" loading="lazy" />
                    : <div className="event-card-blank" />}
                  <div className="event-card-grad" />
                  <div className="event-card-text">
                    <span className="event-card-title">{e.title}</span>
                    <span className="event-card-sub">{e.matched} photos with {p.label}</span>
                  </div>
                </div>
              </Link>
            ))}
          </div>
        </section>
      )}

      <div className="tabs">
        <button className={tab === "photos" ? "on" : ""} onClick={() => setTab("photos")}>
          Photos <span className="dim tnum">{p.photo_count.toLocaleString()}</span>
        </button>
        <button className={tab === "review" ? "on" : ""} onClick={() => setTab("review")}>
          Review faces <span className="dim tnum">{p.face_count.toLocaleString()}</span>
        </button>
      </div>

      {tab === "photos" ? (
        photos.isLoading ? <Spinner label="Loading photos" /> : (
          <PhotoGrid items={items} grouping="month" targetHeight={210}
            onOpen={(_, index) => viewer.open(items.map((i) => i.id), index)}
            emptyState={<EmptyState title="No photos" hint="This person has no photos yet." />} />
        )
      ) : (
        <div className="review">
          <p className="dim review-hint">
            Least-confident matches first. Select any face that isn't {p.label} and correct it —
            Memoria remembers your decision and never re-suggests it.
          </p>
          {selected.size > 0 && (
            <div className="review-bar">
              <span>{selected.size} selected</span>
              <button className="btn btn-primary btn-sm" onClick={() => confirm.mutate([...selected])}>
                <Check size={14} /> Confirm as {p.label}
              </button>
              <button className="btn btn-ghost btn-sm" onClick={() => split.mutate([...selected])}>
                <Scissors size={14} /> Split into new person
              </button>
              <button className="btn btn-danger btn-sm" onClick={() => reject.mutate([...selected])}>
                <UserX size={14} /> Not {p.label}
              </button>
              <button className="btn btn-quiet btn-sm" onClick={() => setSelected(new Set())}>Clear</button>
            </div>
          )}
          {faces.isLoading ? <Spinner label="Loading faces" /> : (
            <div className="face-review-grid">
              {(faces.data?.faces ?? []).map((f: any) => {
                const on = selected.has(f.id);
                return (
                  <button key={f.id} className={`face-review${on ? " is-selected" : ""}`}
                    onClick={() => setSelected((s) => {
                      const next = new Set(s);
                      next.has(f.id) ? next.delete(f.id) : next.add(f.id);
                      return next;
                    })}
                    title={`confidence ${f.confidence != null ? Math.round(f.confidence * 100) + "%" : "n/a"}`}>
                    <img src={faceUrl(f.id, 160)} alt="" loading="lazy" />
                    {on && <span className="face-review-check"><Check size={14} strokeWidth={3} /></span>}
                    {f.source === "user" && <span className="face-review-badge" title="Confirmed by you">✓</span>}
                    <span className="face-review-conf tnum">
                      {f.confidence != null ? Math.round(f.confidence * 100) : "–"}
                    </span>
                  </button>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
