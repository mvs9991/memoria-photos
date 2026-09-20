import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Eye, EyeOff, Merge, Search, UserPlus, Users, X } from "lucide-react";
import { api, faceUrl, thumbUrl } from "../lib/api";
import { EmptyState, ErrorState, SectionHeader, Spinner } from "../components/States";
import { useTitle } from "../lib/hooks";

export default function People() {
  useTitle("People");
  const qc = useQueryClient();
  const [showHidden, setShowHidden] = useState(false);
  const [filter, setFilter] = useState("");
  const [sort, setSort] = useState<"photos" | "name" | "recent">("photos");
  const [mergeMode, setMergeMode] = useState(false);
  const [picked, setPicked] = useState<number[]>([]);

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["people", { showHidden, sort }],
    queryFn: () => api.people({ include_hidden: showHidden, sort }),
  });
  const suggestions = useQuery({ queryKey: ["merge-suggestions"], queryFn: api.mergeSuggestions });

  const merge = useMutation({
    mutationFn: ({ target, sources }: { target: number; sources: number[] }) => api.mergePeople(target, sources),
    onSuccess: () => {
      setPicked([]);
      setMergeMode(false);
      qc.invalidateQueries({ queryKey: ["people"] });
      qc.invalidateQueries({ queryKey: ["merge-suggestions"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
    },
  });
  const notSame = useMutation({
    mutationFn: ({ a, b }: { a: number; b: number }) => api.notSame(a, b),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["merge-suggestions"] }),
  });

  if (isError) return <ErrorState error={error} onRetry={() => refetch()} />;
  if (isLoading) return <Spinner full label="Loading people" />;

  const people = (data?.people ?? []).filter((p) =>
    !filter || p.label.toLowerCase().includes(filter.toLowerCase()));
  const named = people.filter((p) => p.named);
  const unnamed = people.filter((p) => !p.named);

  const toggle = (id: number) =>
    setPicked((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]));

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="display">People</h1>
          <p className="dim">
            {people.length.toLocaleString()} people found by grouping similar faces
            {data?.unassigned_faces ? (
              <>
                {" · "}
                <Link to="/people/unassigned" className="link">
                  {data.unassigned_faces.toLocaleString()} faces not yet grouped
                </Link>
              </>
            ) : ""}
          </p>
        </div>
        <div className="toolbar">
          <div className="input-inline">
            <Search size={14} className="dim" />
            <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter people"
              aria-label="Filter people" />
          </div>
          <select className="field field-sm" value={sort} onChange={(e) => setSort(e.target.value as any)}
            aria-label="Sort people">
            <option value="photos">Most photos</option>
            <option value="name">Name</option>
            <option value="recent">Recently seen</option>
          </select>
          <button className={`btn btn-ghost btn-sm${mergeMode ? " is-on" : ""}`}
            onClick={() => { setMergeMode((v) => !v); setPicked([]); }}>
            <Merge size={14} /> {mergeMode ? "Cancel" : "Merge"}
          </button>
          <button className="btn btn-quiet btn-icon btn-sm" onClick={() => setShowHidden((v) => !v)}
            title={showHidden ? "Hide hidden people" : "Show hidden people"}>
            {showHidden ? <Eye size={15} /> : <EyeOff size={15} />}
          </button>
        </div>
      </div>

      {mergeMode && (
        <div className="merge-bar">
          <span>{picked.length === 0 ? "Pick the people who are the same person" :
            `${picked.length} selected — the first one keeps its name`}</span>
          <div className="merge-bar-actions">
            {picked.length > 1 && (
              <button className="btn btn-primary btn-sm"
                onClick={() => merge.mutate({ target: picked[0], sources: picked.slice(1) })}>
                <Check size={14} /> Merge into {people.find((p) => p.id === picked[0])?.label}
              </button>
            )}
            <button className="btn btn-quiet btn-sm" onClick={() => { setPicked([]); setMergeMode(false); }}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {!mergeMode && (suggestions.data?.suggestions?.length ?? 0) > 0 && (
        <section className="suggest-merges">
          <SectionHeader title="Possible same person" sub="Memoria thinks these two groups might be one person." />
          <div className="merge-cards">
            {suggestions.data!.suggestions.slice(0, 6).map((s: any) => (
              <div key={`${s.a.id}-${s.b.id}`} className="merge-card">
                <div className="merge-card-faces">
                  <FaceBubble faceId={s.a.cover_face_id} label={s.a.label} count={s.a.photo_count} />
                  <span className="merge-card-eq dim">≈</span>
                  <FaceBubble faceId={s.b.cover_face_id} label={s.b.label} count={s.b.photo_count} />
                </div>
                <div className="merge-card-actions">
                  <button className="btn btn-primary btn-sm"
                    onClick={() => merge.mutate({ target: s.a.id, sources: [s.b.id] })}>
                    <Merge size={13} /> Same person
                  </button>
                  <button className="btn btn-quiet btn-sm" onClick={() => notSame.mutate({ a: s.a.id, b: s.b.id })}>
                    <X size={13} /> Different
                  </button>
                </div>
                <div className="dim merge-card-score tnum">{Math.round(s.score * 100)}% match</div>
              </div>
            ))}
          </div>
        </section>
      )}

      {people.length === 0 ? (
        <EmptyState icon={<Users size={26} />} title="No people yet"
          hint="People appear automatically once photos with faces are indexed." />
      ) : (
        <>
          {named.length > 0 && (
            <section>
              <SectionHeader title="Named" count={named.length} />
              <PeopleGrid people={named} mergeMode={mergeMode} picked={picked} onPick={toggle} />
            </section>
          )}
          {unnamed.length > 0 && (
            <section>
              <SectionHeader title="Discovered" count={unnamed.length}
                sub="Groups of the same face. Give them a name to search by person." />
              <PeopleGrid people={unnamed} mergeMode={mergeMode} picked={picked} onPick={toggle} />
            </section>
          )}
        </>
      )}
    </div>
  );
}

// A real library discovers far more people than anyone scrolls through — 1,790 on
// a 23k-photo library, mostly one-off faces from crowds. Rendering them all cost
// 9,229 DOM nodes and a 7s load, so the tail is revealed on request.
const PEOPLE_PAGE = 300;

function PeopleGrid({ people, mergeMode, picked, onPick }: {
  people: any[]; mergeMode: boolean; picked: number[]; onPick: (id: number) => void;
}) {
  const [shown, setShown] = useState(PEOPLE_PAGE);
  const visible = people.length > shown ? people.slice(0, shown) : people;
  return (
    <>
    <div className="people-grid">
      {visible.map((p) => {
        const inner = (
          <>
            <span className={`person-face${picked.includes(p.id) ? " is-picked" : ""}`}>
              {p.cover_face_id ? (
                <img src={faceUrl(p.cover_face_id, 240)} alt="" loading="lazy" />
              ) : (
                <span className="person-face-blank"><Users size={22} /></span>
              )}
              {mergeMode && picked.includes(p.id) && (
                <span className="person-pick-badge tnum">{picked.indexOf(p.id) + 1}</span>
              )}
              {p.hidden && <span className="person-hidden-badge"><EyeOff size={12} /></span>}
            </span>
            <span className="person-name">{p.label}</span>
            <span className="person-count dim tnum">{p.photo_count.toLocaleString()}</span>
          </>
        );
        return mergeMode ? (
          <button key={p.id} className="person-tile" onClick={() => onPick(p.id)}>{inner}</button>
        ) : (
          <Link key={p.id} to={`/people/${p.id}`} className="person-tile">{inner}</Link>
        );
      })}
    </div>
    {people.length > shown && (
      <button className="btn subtle people-more" onClick={() => setShown((n) => n + PEOPLE_PAGE * 2)}>
        Show more — {(people.length - shown).toLocaleString()} more people
      </button>
    )}
    </>
  );
}

function FaceBubble({ faceId, label, count }: { faceId: number | null; label: string; count: number }) {
  return (
    <div className="face-bubble">
      {faceId ? <img src={faceUrl(faceId, 160)} alt="" /> : <span className="person-face-blank"><Users size={18} /></span>}
      <span className="face-bubble-name">{label}</span>
      <span className="dim tnum">{count.toLocaleString()}</span>
    </div>
  );
}
