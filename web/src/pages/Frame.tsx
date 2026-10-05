/**
 * Photo-frame mode for a spare tablet or TV: /frame, optionally ?album=, ?person=,
 * ?rating= (minimum stars) or ?collection=. Random photos, a clock, runs until closed.
 * Dashboards that only take an image URL can use /api/random/image with the same filters.
 */
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { Slideshow } from "../components/Slideshow";
import { useTitle } from "../lib/hooks";

export default function Frame() {
  useTitle("Photo frame");
  const [params] = useSearchParams();
  const filter = {
    count: 300,
    album: params.get("album") ?? undefined,
    person: params.get("person") ?? undefined,
    min_rating: params.get("rating") ?? undefined,
    collection: params.get("collection") ?? undefined,
  };
  const { data, isError, error, refetch } = useQuery({
    queryKey: ["frame", filter],
    queryFn: () => api.random(filter),
    staleTime: Infinity,
    // A frame runs unattended for days: through a Wi-Fi blip or the server restarting it keeps trying,
    // and keeps showing the photos it has meanwhile.
    retry: true,
    retryDelay: 30_000,
  });

  if (isError && !data) return <div className="frame-message">{(error as Error).message}</div>;
  if (!data) return <div className="frame-message">Loading…</div>;
  if (!data.ids.length) return <div className="frame-message">No photos match this frame’s filter.</div>;
  return <Slideshow frame items={data.ids.map((id) => ({ id }))} onExhausted={() => refetch()}
    onClose={() => window.history.length > 1 ? window.history.back() : window.location.assign("/")} />;
}
