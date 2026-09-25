/** 1–5 stars. Clicking the current rating clears it. */
import { Star } from "lucide-react";

export function StarRating({ value, onChange, size = 16, label = "Rating" }: {
  value: number;
  onChange: (rating: number) => void;
  size?: number;
  label?: string;
}) {
  return (
    <span className="stars" role="radiogroup" aria-label={label}>
      {[1, 2, 3, 4, 5].map((n) => (
        <button key={n} type="button" role="radio" aria-checked={value === n}
          className={`star${n <= value ? " on" : ""}`}
          onClick={(e) => { e.stopPropagation(); onChange(value === n ? 0 : n); }}
          title={value === n ? "Clear rating" : `${n} star${n > 1 ? "s" : ""}`}
          aria-label={`${n} star${n > 1 ? "s" : ""}`}>
          <Star size={size} fill={n <= value ? "currentColor" : "none"} />
        </button>
      ))}
    </span>
  );
}
