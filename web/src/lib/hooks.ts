import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type Role } from "./api";

/** The signed-in person's role; "owner" for a library without accounts. */
export function useRole(): Role {
  const { data } = useQuery({ queryKey: ["auth"], queryFn: api.authStatus, staleTime: Infinity });
  return data?.user?.role ?? "owner";
}

export function useTheme() {
  const [theme, setTheme] = useState<"dark" | "light">(() => {
    const saved = localStorage.getItem("theme");
    if (saved === "dark" || saved === "light") return saved;
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  });
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.querySelector('meta[name="color-scheme"]')?.setAttribute("content", theme);
    localStorage.setItem("theme", theme);
  }, [theme]);
  return { theme, toggle: () => setTheme((t) => (t === "dark" ? "light" : "dark")) };
}

export function useDebounced<T>(value: T, ms = 250): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

/** Sticky page title in the document head. */
export function useTitle(title?: string) {
  useEffect(() => {
    document.title = title ? `${title} · Memoria` : "Memoria";
  }, [title]);
}

export function useLocalState<T>(key: string, initial: T) {
  const [v, setV] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw ? (JSON.parse(raw) as T) : initial;
    } catch {
      return initial;
    }
  });
  const set = useCallback((next: T) => {
    setV(next);
    try {
      localStorage.setItem(key, JSON.stringify(next));
    } catch {
      /* storage may be unavailable (private mode) */
    }
  }, [key]);
  return [v, set] as const;
}

/** Animated count-up for hero numbers. */
export function useCountUp(target: number, ms = 700) {
  const [v, setV] = useState(0);
  const prev = useRef(0);
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setV(target);
      return;
    }
    const from = prev.current;
    const start = performance.now();
    let raf = 0;
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / ms);
      const eased = 1 - Math.pow(1 - t, 3);
      setV(Math.round(from + (target - from) * eased));
      if (t < 1) raf = requestAnimationFrame(tick);
      else prev.current = target;
    };
    raf = requestAnimationFrame(tick);
    // rAF is throttled or suspended in a background tab, so a library opened in
    // one and read later would show a frozen, wrong total. The number matters
    // more than the flourish: settle on it regardless of whether frames ran.
    const safety = window.setTimeout(() => {
      cancelAnimationFrame(raf);
      prev.current = target;
      setV(target);
    }, ms + 400);
    return () => {
      cancelAnimationFrame(raf);
      clearTimeout(safety);
    };
  }, [target, ms]);
  return v;
}

/**
 * How many items of a long list to draw: a first screenful at once, then more each time the returned
 * sentinel (put it after the list) comes within ~1200 px of view. A page that drew every card at once
 * (734 events, ~8,000 elements) took over five seconds to appear on a phone-speed CPU.
 * `resetKey` starts again from the first screenful (a new filter, say).
 */
export function useGrowOnScroll(total: number, first = 48, step = 96, resetKey: unknown = null) {
  const [shown, setShown] = useState(first);
  const sentinel = useRef<HTMLDivElement | null>(null);
  useEffect(() => { setShown(first); }, [resetKey, first]);
  const more = shown < total;
  useEffect(() => {
    const el = sentinel.current;
    if (!more || !el) return;
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) setShown((n) => n + step);
    }, { root: el.closest("[data-scroll-root]"), rootMargin: "1200px 0px" });
    io.observe(el);
    return () => io.disconnect();
  }, [more, shown, step]);
  return { shown, more, sentinel };
}

