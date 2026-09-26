/**
 * What someone opening a share link sees: one album, read-only, outside the app shell.
 * Every request carries the token and is checked against the album on the server.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, Download, ImagePlus, X } from "lucide-react";
import { api, FLAG, sharedDownloadUrl, sharedThumbUrl, sharedVideoUrl, gridItems } from "../lib/api";
import { PhotoGrid } from "../components/PhotoGrid";
import { Spinner } from "../components/States";
import { useTitle } from "../lib/hooks";
import { uploadAll } from "../lib/upload";

export default function SharedAlbum() {
  const { token = "" } = useParams();
  const { data, isLoading, isError, refetch } = useQuery({ queryKey: ["shared", token], queryFn: () => api.shared(token), retry: false });
  const [open, setOpen] = useState<number | null>(null);
  useTitle(data?.name ?? "Shared album");

  const items = useMemo(() => gridItems(data?.photos), [data]);
  const thumbFor = useCallback((id: number, size: "sm" | "m") => sharedThumbUrl(token, id, size), [token]);

  if (isLoading) return <div className="share-page"><Spinner full label="Opening album" /></div>;
  if (isError || !data) {
    return (
      <div className="share-page share-gone">
        <h1 className="display">This link doesn’t work any more</h1>
        <p className="dim">It may have expired or been turned off by the person who shared it.</p>
      </div>
    );
  }

  return (
    <div className="share-page" data-scroll-root>
      <header className="share-head">
        <h1 className="display">{data.name}</h1>
        <p className="dim">{items.length.toLocaleString()} {items.length === 1 ? "item" : "items"} · shared with you</p>
        {data.allow_upload && <AddPhotos token={token} onDone={() => refetch()} />}
      </header>
      <div className="share-grid">
        <PhotoGrid items={items} grouping="day" targetHeight={220} thumbFor={thumbFor} scrubber={false}
          onOpen={(_, index) => setOpen(index)} />
      </div>
      {open !== null && (
        <Lightbox token={token} items={items} index={open} onIndex={setOpen} onClose={() => setOpen(null)}
          download={data.allow_download} />
      )}
      <footer className="share-foot dim">Shared from Memoria</footer>
    </div>
  );
}

function Lightbox({ token, items, index, onIndex, onClose, download }: {
  token: string; items: { id: number; flags: number }[]; index: number;
  onIndex: (i: number) => void; onClose: () => void; download: boolean;
}) {
  const it = items[index];
  const go = useCallback((d: number) => onIndex((index + d + items.length) % items.length), [index, items.length, onIndex]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "ArrowLeft") go(-1);
      if (e.key === "ArrowRight") go(1);
    };
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [go, onClose]);
  const video = (it.flags & FLAG.video) > 0;
  return (
    <div className="share-lightbox" onClick={onClose} role="dialog" aria-label="Photo">
      <div className="share-lb-media" onClick={(e) => e.stopPropagation()}>
        {video ? <video key={it.id} src={sharedVideoUrl(token, it.id)} poster={sharedThumbUrl(token, it.id, "l")} controls autoPlay />
          : <img key={it.id} src={sharedThumbUrl(token, it.id, "l")} alt="" />}
      </div>
      <div className="share-lb-bar" onClick={(e) => e.stopPropagation()}>
        <button className="ss-btn" onClick={() => go(-1)} aria-label="Previous"><ChevronLeft size={20} /></button>
        <span className="ss-count tnum">{index + 1} / {items.length}</span>
        <button className="ss-btn" onClick={() => go(1)} aria-label="Next"><ChevronRight size={20} /></button>
        {download && (
          <a className="ss-btn" href={sharedDownloadUrl(token, it.id)} aria-label="Download original"><Download size={18} /></a>
        )}
        <button className="ss-btn" onClick={onClose} aria-label="Close"><X size={19} /></button>
      </div>
    </div>
  );
}

function AddPhotos({ token, onDone }: { token: string; onDone: () => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [state, setState] = useState<{ total: number; done: number; added: number; dup: number } | null>(null);
  const start = async (files: File[]) => {
    if (!files.length) return;
    const tally = { total: files.length, done: 0, added: 0, dup: 0 };
    setState({ ...tally });
    await uploadAll(`/api/share/${token}/upload`, files, (_, patch) => {
      if (patch.status === "uploading") return;
      tally.done += 1;
      if (patch.status === "added") tally.added += 1;
      if (patch.status === "duplicate") tally.dup += 1;
      setState({ ...tally });
    });
    await fetch(`/api/share/${token}/upload/finish`, { method: "POST" }).catch(() => undefined);
    setTimeout(onDone, 4000);
  };
  return (
    <div className="share-add">
      <input ref={input} type="file" multiple hidden accept="image/*,video/*,.heic,.heif"
        onChange={(e) => { start([...(e.target.files ?? [])]); e.target.value = ""; }} />
      <button className="btn btn-primary" onClick={() => input.current?.click()}
        disabled={!!state && state.done < state.total}>
        <ImagePlus size={15} /> Add your photos
      </button>
      {state && (
        <span className="dim">
          {state.done < state.total ? `Sending ${state.done + 1} of ${state.total}…`
            : `Thank you — ${state.added} added${state.dup ? `, ${state.dup} were already here` : ""}. They appear in a minute.`}
        </span>
      )}
    </div>
  );
}
