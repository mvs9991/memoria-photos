/**
 * The Locked folder. Its photos appear nowhere else; here they appear only after the PIN,
 * and only for a few minutes in this browser. The server enforces it — this page just asks.
 */
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckSquare, Lock, LockOpen, X } from "lucide-react";
import { api, ApiError, gridItems } from "../lib/api";
import { PhotoGrid } from "../components/PhotoGrid";
import { EmptyState, Spinner } from "../components/States";
import { useSelectAllShortcut, useSelection } from "../components/SelectionBar";
import { useViewer } from "../components/ViewerContext";
import { useTitle } from "../lib/hooks";

export default function Locked() {
  useTitle("Locked");
  const qc = useQueryClient();
  const status = useQuery({ queryKey: ["locked-status"], queryFn: api.lockedStatus, refetchInterval: 30_000 });
  if (status.isLoading || !status.data) return <Spinner full label="Locked folder" />;
  if (!status.data.pin_set) return <SetPin onDone={() => qc.invalidateQueries({ queryKey: ["locked-status"] })} />;
  if (!status.data.open) return <EnterPin count={status.data.count} />;
  return <Open />;
}

function SetPin({ onDone }: { onDone: () => void }) {
  const [pin, setPin] = useState("");
  const [again, setAgain] = useState("");
  const save = useMutation({ mutationFn: () => api.setLockedPin(pin), onSuccess: onDone });
  return (
    <div className="page locked-gate">
      <form className="card login-card" onSubmit={(e) => { e.preventDefault(); if (pin.length >= 4 && pin === again) save.mutate(); }}>
        <span className="brand-mark login-mark" aria-hidden><Lock size={20} /></span>
        <h1 className="display">Locked folder</h1>
        <p className="dim">Photos you move here disappear from everywhere else in Memoria and open only with this PIN.
          The files stay where they are on your disk — this hides them from people using the app; it does not encrypt them.</p>
        <input className="field" type="password" inputMode="numeric" placeholder="New PIN (4 or more)" value={pin}
          onChange={(e) => setPin(e.target.value)} aria-label="New PIN" autoFocus />
        <input className="field" type="password" inputMode="numeric" placeholder="Again" value={again}
          onChange={(e) => setAgain(e.target.value)} aria-label="Repeat PIN" />
        {again && pin !== again && <p className="danger-text">The two PINs differ.</p>}
        {save.error && <p className="danger-text">{(save.error as Error).message}</p>}
        <button className="btn btn-primary" type="submit" disabled={pin.length < 4 || pin !== again}>Set PIN</button>
        <p className="dim fix-hint">Forgotten PINs can be reset at the computer: <code>photointel accounts reset-pin</code></p>
      </form>
    </div>
  );
}

function EnterPin({ count }: { count: number }) {
  const qc = useQueryClient();
  const [pin, setPin] = useState("");
  const open = useMutation({
    mutationFn: () => api.openLocked(pin),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["locked-status"] }); qc.invalidateQueries({ queryKey: ["locked-photos"] }); },
  });
  return (
    <div className="page locked-gate">
      <form className="card login-card" onSubmit={(e) => { e.preventDefault(); if (pin) open.mutate(); }}>
        <span className="brand-mark login-mark" aria-hidden><Lock size={20} /></span>
        <h1 className="display">Locked folder</h1>
        <p className="dim">{count ? `${count.toLocaleString()} ${count === 1 ? "item" : "items"}. ` : ""}Enter the PIN to open it on this device for 15 minutes.</p>
        <input className="field" type="password" inputMode="numeric" autoFocus value={pin}
          onChange={(e) => setPin(e.target.value)} aria-label="PIN" />
        {open.error && <p className="danger-text">{(open.error as ApiError).status === 429
          ? `Too many wrong tries — ${(open.error as Error).message.replace(/^.*try again/, "try again")}.` : "Wrong PIN."}</p>}
        <button className="btn btn-primary" type="submit" disabled={!pin || open.isPending}>Open</button>
      </form>
    </div>
  );
}

function Open() {
  const qc = useQueryClient();
  const viewer = useViewer();
  const selection = useSelection();
  const [selecting, setSelecting] = useState(false);
  const photos = useQuery({ queryKey: ["locked-photos"], queryFn: api.lockedPhotos });
  const items = useMemo(() => gridItems(photos.data), [photos.data]);
  const allIds = useMemo(() => items.map((i) => i.id), [items]);
  useSelectAllShortcut(selecting, allIds, selection.setAll);
  const refresh = () => { qc.invalidateQueries(); selection.clear(); };
  const close = useMutation({ mutationFn: api.closeLocked, onSuccess: () => qc.invalidateQueries({ queryKey: ["locked-status"] }) });
  const unlock = useMutation({ mutationFn: (ids: number[]) => api.unlock(ids), onSuccess: refresh });
  const sel = [...selection.selected];
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="display">Locked folder</h1>
          <p className="dim">Open on this device for up to 15 minutes. Nothing here appears anywhere else in Memoria.</p>
        </div>
        <div className="toolbar">
          <button className={`btn btn-ghost btn-sm${selecting ? " is-on" : ""}`}
            onClick={() => { setSelecting((v) => !v); selection.clear(); }}>
            <CheckSquare size={14} /> {selecting ? "Done" : "Select"}
          </button>
          <button className="btn btn-primary btn-sm" onClick={() => close.mutate()}><Lock size={14} /> Lock now</button>
        </div>
      </div>
      {sel.length > 0 && (
        <div className="review-bar selection-bar" role="toolbar" aria-label="Selected">
          <span className="tnum"><strong>{sel.length}</strong> selected</span>
          <button className="btn btn-ghost btn-sm" onClick={() => unlock.mutate(sel)}>
            <LockOpen size={14} /> Move out of Locked
          </button>
          <button className="btn btn-quiet btn-sm" onClick={selection.clear}><X size={14} /> Clear</button>
        </div>
      )}
      {photos.isLoading ? <Spinner /> : (
        <PhotoGrid items={items} grouping="month" targetHeight={200}
          onOpen={(_, index) => viewer.open(allIds, index)}
          selectable selectMode={selecting} selection={selection.selected} onToggleSelect={selection.toggle}
          onSelectRange={selection.addMany}
          emptyState={<EmptyState icon={<Lock size={26} />} title="Nothing locked yet"
            hint="Select photos anywhere and choose “Lock” to move them here." />} />
      )}
    </div>
  );
}
