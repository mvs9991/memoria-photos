import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Loader2 } from "lucide-react";
import { api } from "../lib/api";
import { useEffect, useRef } from "react";

export function JobIndicator() {
  const qc = useQueryClient();
  const wasActive = useRef(false);
  const { data } = useQuery({
    queryKey: ["jobs"],
    queryFn: api.jobs,
    refetchInterval: (q) => (q.state.data?.active ? 1500 : 15_000),
  });
  const job = data?.active;

  useEffect(() => {
    if (wasActive.current && !job) {
      // Indexing finished: refresh everything that depends on it.
      qc.invalidateQueries();
    }
    wasActive.current = !!job;
  }, [job, qc]);

  if (!job) return null;
  const pct = job.progress_total ? Math.round((job.progress_done / job.progress_total) * 100) : null;
  return (
    <Link to="/settings" className="job-pill" title={job.message || job.stage || "Working"}>
      <Loader2 size={14} className="spin" />
      <span className="job-pill-text">
        {job.stage === "analyze" ? "Indexing" : job.stage ?? "Working"}
        {pct !== null && <span className="tnum"> {pct}%</span>}
      </span>
      {pct !== null && (
        <span className="job-pill-bar">
          <span style={{ width: `${pct}%` }} />
        </span>
      )}
    </Link>
  );
}
