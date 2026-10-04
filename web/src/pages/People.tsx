import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, CheckSquare, Eye, EyeOff, FolderOutput, Merge, Search, UserPlus, Users, X } from "lucide-react";
import { ExportDialog } from "../components/ExportDialog";
import { api, faceUrl, thumbUrl } from "../lib/api";
import { EmptyState, ErrorState, SectionHeader, Spinner } from "../components/States";
import { useTitle } from "../lib/hooks";
import { useDragSelect } from "../lib/dragSelect";
import { FloatingBar } from "../components/SelectionBar";

export default function People() {
  useTitle("People");
  const qc = useQueryClient();
  const [showHidden, setShowHidden] = useState(false);
  const [filter, setFilter] = useState("");
  const [sort, setSort] = useState<"photos" | "name" | "recent">("photos");
  // One select mode for everything: tap, or press and hold then drag across people, and
  // then choose what to do with them (hide, merge, export).
  const [selecting, setSelecting] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [picked, setPicked] = useState<number[]>([]);
  const [note, setNote] = useState<{ text: string; undo: number[] } | null>(null);

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["people", { showHidden, sort }],
    queryFn: () => api.people({ include_hidden: showHidden, sort }),
  });
  const suggestions = useQuery({ queryKey: ["merge-suggestions"], queryFn: api.mergeSuggestions });
  const names = useQuery({ queryKey: ["name-suggestions"], queryFn: api.nameSuggestions });
  const acceptName = useMutation({
    mutationFn: ({ id, name }: { id: number; name: string }) => api.renamePerson(id, name),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["people"] });
      qc.invalidateQueries({ queryKey: ["name-suggestions"] });
    },
  });
  const dismissName = useMutation({
    mutationFn: ({ id, name }: { id: number; name: string }) => api.dismissName(id, name),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["name-suggestions"] }),
  });

  const merge = useMutation({
    mutationFn: ({ target, sources }: { target: number; sources: number[] }) => api.mergePeople(target, sources),
    onSuccess: () => {
      setPicked([]);
      setSelecting(false);
      qc.invalidateQueries({ queryKey: ["people"] });
      qc.invalidateQueries({ queryKey: ["merge-suggestions"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
    },
  });
  const hide = useMutation({
    mutationFn: ({ ids, hidden }: { ids: number[]; hidden: boolean }) => api.hidePeople(ids, hidden),
    onSuccess: (r, { ids, hidden }) => {
      setPicked([]);
      setNote(hidden ? { text: `Hid ${r.changed.toLocaleString()} ${r.changed === 1 ? "person" : "people"}`, undo: ids } : null);
      qc.invalidateQueries({ queryKey: ["people"] });
      qc.invalidateQueries({ queryKey: ["merge-suggestions"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
    },
  });
  const notSame = useMutation({
    mutationFn: ({ a, b }: { a: number; b: number }) => api.notSame(a, b),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["merge-suggestions"] }),
  });

  useEffect(() => {
    if (!selecting) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      if (document.querySelector('[role="dialog"], [role="alertdialog"]')) return;
      setSelecting(false);
      setPicked([]);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selecting]);

  if (isError) return <ErrorState error={error} onRetry={() => refetch()} />;
  if (isLoading) return <Spinner full label="Loading people" />;

  const people = (data?.people ?? []).filter((p) =>
    !filter || p.label.toLowerCase().includes(filter.toLowerCase()));
  const named = people.filter((p) => p.named);
  const unnamed = people.filter((p) => !p.named);

  const begin = () => setSelecting(true);
  const allPicked = (list: any[]) => list.length > 0 && list.every((p) => picked.includes(p.id));
  // Select, or deselect, a whole list at once (a section, or everyone).
  const toggleList = (list: any[]) => {
    setSelecting(true);
    setNote(null);
    setPicked((cur) => allPicked(list)
      ? cur.filter((id) => !list.some((p) => p.id === id))
      : [...cur, ...list.filter((p) => !cur.includes(p.id)).map((p) => p.id)]);
  };
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
          <button className={`btn btn-ghost btn-sm${selecting ? " is-on" : ""}`}
            onClick={() => { setSelecting((v) => !v); setPicked([]); setNote(null); }}
            title="Pick people to hide, merge or export — or press and hold one, then drag across others. Esc to finish.">
            <CheckSquare size={14} /> {selecting ? "Done" : "Select"}
          </button>
          <button className="btn btn-quiet btn-icon btn-sm" onClick={() => setShowHidden((v) => !v)}
            title={showHidden ? "Hide hidden people" : "Show hidden people"}>
            {showHidden ? <Eye size={15} /> : <EyeOff size={15} />}
          </button>
        </div>
      </div>

      {exporting && (
        <ExportDialog spec={{ person_ids: picked }} onClose={() => setExporting(false)}
          title={picked.length === 1 ? `Export the photos of ${people.find((p) => p.id === picked[0])?.label}`
            : `Export the photos of ${picked.length} people`} />
      )}

      {selecting && (
        <FloatingBar>
          <div className="merge-bar" role="toolbar" aria-label="Selected people">
            <span className="tnum">
              {picked.length === 0
                ? (note
                    ? <>{note.text}. <button className="link-button" onClick={() => hide.mutate({ ids: note.undo, hidden: false })}>Undo</button></>
                    : "Tap people to pick them — or press and hold one and drag across others")
                : <><strong>{picked.length.toLocaleString()}</strong> selected</>}
            </span>
            <div className="merge-bar-actions">
              {people.length > 0 && (picked.length === 0 || allPicked(people)) && (
                <button className="btn btn-ghost btn-sm" onClick={() => toggleList(people)}
                  title="Pick everyone on this page">
                  {allPicked(people) ? "Deselect all" : `Select all ${people.length.toLocaleString()}`}
                </button>
              )}
              {picked.length > 0 && (
                <button className="btn btn-ghost btn-sm" disabled={hide.isPending}
                  onClick={() => hide.mutate({ ids: picked, hidden: true })}
                  title="Take them off this page. Nothing is deleted; the eye icon shows hidden people again.">
                  <EyeOff size={14} /> Hide {picked.length.toLocaleString()}
                </button>
              )}
              {picked.length > 1 && (
                <button className="btn btn-primary btn-sm"
                  onClick={() => {
                    const first = people.find((p) => p.id === picked[0])?.label ?? "the first one";
                    // Two is an ordinary merge. More than that is almost always a stray click after
                    // "Select all", and it would fold many different people into one.
                    if (picked.length > 2 && !window.confirm(
                      `Merge ${picked.length.toLocaleString()} people into ${first}?\n\nUse this only if they are all the same person.`)) return;
                    merge.mutate({ target: picked[0], sources: picked.slice(1) });
                  }}
                  title="They are the same person. The first one picked keeps its name.">
                  <Check size={14} /> Merge into {people.find((p) => p.id === picked[0])?.label}
                </button>
              )}
              {picked.length > 0 && (
                <button className="btn btn-ghost btn-sm" onClick={() => setExporting(true)}
                  title="Copy all their photos somewhere">
                  <FolderOutput size={14} /> Export
                </button>
              )}
              <button className="btn btn-quiet btn-sm" onClick={() => { setPicked([]); setNote(null); setSelecting(false); }}>
                Done
              </button>
            </div>
          </div>
        </FloatingBar>
      )}

      {!selecting && (suggestions.data?.suggestions?.length ?? 0) > 0 && (
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

      {!selecting && (names.data?.suggestions?.length ?? 0) > 0 && (
        <section className="suggest-merges">
          <SectionHeader title="Names from Google Photos"
            sub="Your Takeout export named people in these photos. Memoria never applies a name on its own — check the face, then accept or dismiss." />
          <div className="merge-cards">
            {names.data!.suggestions.slice(0, 8).map((n) => (
              <div key={`${n.person_id}-${n.name}`} className="merge-card">
                <div className="merge-card-faces">
                  <FaceBubble faceId={n.cover_face_id} label={n.label} count={n.matched_photos} />
                  <span className="merge-card-eq dim">→</span>
                  <span className="name-suggestion">{n.name}</span>
                </div>
                <div className="dim merge-card-score">
                  named “{n.name}” in {n.matched_photos} of their {n.labelled_photos} labelled photos
                </div>
                <div className="merge-card-actions">
                  <button className="btn btn-primary btn-sm"
                    onClick={() => acceptName.mutate({ id: n.person_id, name: n.name })}>
                    <Check size={13} /> That's {n.name}
                  </button>
                  <Link to={`/people/${n.person_id}`} className="btn btn-quiet btn-sm">Review</Link>
                  <button className="btn btn-quiet btn-sm" onClick={() => dismissName.mutate({ id: n.person_id, name: n.name })}>
                    <X size={13} /> Not them
                  </button>
                </div>
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
              <SectionHeader title="Named" count={named.length} action={
                <button className="btn btn-quiet btn-sm" onClick={() => toggleList(named)}>
                  {allPicked(named) ? "Deselect all" : "Select all"}
                </button>} />
              <PeopleGrid people={named} selecting={selecting} picked={picked} onPick={toggle} onSet={setPicked} onBegin={begin} />
            </section>
          )}
          {unnamed.length > 0 && (
            <section>
              <SectionHeader title="Discovered" count={unnamed.length}
                sub="Groups of the same face. Give them a name to search by person."
                action={
                  <button className="btn btn-quiet btn-sm" onClick={() => toggleList(unnamed)}>
                    {allPicked(unnamed) ? "Deselect all" : "Select all"}
                  </button>} />
              <PeopleGrid people={unnamed} selecting={selecting} picked={picked} onPick={toggle} onSet={setPicked} onBegin={begin} />
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

function PeopleGrid({ people, selecting, picked, onPick, onSet, onBegin }: {
  people: any[]; selecting: boolean; picked: number[]; onPick: (id: number) => void;
  onSet: (ids: number[]) => void; onBegin: () => void;
}) {
  const [shown, setShown] = useState(PEOPLE_PAGE);
  const visible = people.length > shown ? people.slice(0, shown) : people;
  const hostRef = useRef<HTMLDivElement>(null);
  const drag = useDragSelect({
    ids: visible.map((p) => p.id),
    selection: new Set(picked),
    selectMode: selecting,
    enabled: true,
    setSelection: onSet,
    begin: onBegin,
    getScroller: () => hostRef.current?.closest<HTMLElement>("[data-scroll-root]") ?? null,
  });
  return (
    <>
    <div className={`people-grid${selecting ? " is-selecting" : ""}`} ref={hostRef}
      onPointerDown={drag.onPointerDown} onContextMenu={drag.onContextMenu}>
      {visible.map((p, i) => {
        const inner = (
          <>
            <span className={`person-face${picked.includes(p.id) ? " is-picked" : ""}`}>
              {p.cover_face_id ? (
                <img src={faceUrl(p.cover_face_id, 240)} alt="" loading="lazy" draggable={false} />
              ) : (
                <span className="person-face-blank"><Users size={22} /></span>
              )}
              {selecting && picked.includes(p.id) && (
                <span className="person-pick-badge tnum">{picked.indexOf(p.id) + 1}</span>
              )}
              {p.hidden && <span className="person-hidden-badge"><EyeOff size={12} /></span>}
            </span>
            <span className="person-name">{p.label}</span>
            <span className="person-count dim tnum">{p.photo_count.toLocaleString()}</span>
          </>
        );
        return selecting ? (
          <button key={p.id} className="person-tile" data-sel-index={i} aria-pressed={picked.includes(p.id)}
            onClick={() => { if (!drag.shouldSuppressClick()) onPick(p.id); }}>{inner}</button>
        ) : (
          <Link key={p.id} to={`/people/${p.id}`} className="person-tile" data-sel-index={i} draggable={false}
            // The release that ends a press-and-hold must not also open the person.
            onClick={(e) => { if (drag.shouldSuppressClick()) e.preventDefault(); }}>{inner}</Link>
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
