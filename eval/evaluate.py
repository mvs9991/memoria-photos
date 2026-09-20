"""End-to-end evaluation of an indexed library against ground truth.

    python eval/evaluate.py --data data --library D:/pi_cache/testlib

Measures indexing fidelity, face identity quality, event grouping, duplicate
detection, location accuracy and search quality. All numbers are measured on the
synthetic library produced by make_test_library.py — real libraries are harder.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from photointel.context import AppContext  # noqa: E402
from photointel.metadata import ts_to_naive  # noqa: E402


def f1(p: float, r: float) -> float:
    return 2 * p * r / (p + r) if (p + r) else 0.0


class Evaluator:
    def __init__(self, data_dir: str, library: Path):
        self.ctx = AppContext(data_dir)
        self.conn = self.ctx.connect()
        self.library = Path(library)
        self.gt = json.loads((self.library / "ground_truth.json").read_text(encoding="utf-8"))
        # Ground truth may contain OS-native separators; the index always uses POSIX.
        self.gt["photos"] = {k.replace("\\", "/"): v for k, v in self.gt["photos"].items()}
        self.gt["people"] = {k: [p.replace("\\", "/") for p in v] for k, v in self.gt["people"].items()}
        self.gt["duplicate_groups"] = {k: [p.replace("\\", "/") for p in v]
                                       for k, v in self.gt["duplicate_groups"].items()}
        self.gt["burst_groups"] = {k: [p.replace("\\", "/") for p in v]
                                   for k, v in self.gt["burst_groups"].items()}
        self.rows = {r["rel_path"].replace("\\", "/"): dict(r) for r in self.conn.execute(
            "SELECT p.*, r.path root FROM photos p JOIN roots r ON r.id=p.root_id")}
        self.by_id = {r["id"]: rel for rel, r in self.rows.items()}
        self.report: dict = {}

    # ---------------------------------------------------------------- indexing
    def eval_indexing(self) -> dict:
        gt_photos = self.gt["photos"]
        expected = {p for p, m in gt_photos.items() if not m["note"].startswith("zero-byte")}
        found = set(self.rows)
        missing = expected - found
        extra = found - set(gt_photos)
        ok = sum(1 for r in self.rows.values() if r["status"] == "ok")
        errors = [(rel, r["error"]) for rel, r in self.rows.items() if r["status"] == "error"]
        expected_corrupt = {p for p, m in gt_photos.items() if m["corrupt"] and p in found}
        detected_corrupt = {rel for rel, _ in errors}
        # date accuracy for photos whose EXIF we wrote
        exact = near = wrong = 0
        wrong_examples = []
        for rel, meta in gt_photos.items():
            r = self.rows.get(rel)
            if not r or r["taken_ts"] is None or meta["corrupt"]:
                continue
            truth = datetime.fromisoformat(meta["taken"])
            got = ts_to_naive(r["taken_ts"])
            delta = abs((got - truth).total_seconds())
            if delta <= 1:
                exact += 1
            elif delta <= 86400:
                near += 1
            else:
                wrong += 1
                if len(wrong_examples) < 5:
                    wrong_examples.append((rel, str(truth), str(got), r["date_source"]))
        # source classification
        src_ok = src_total = 0
        confusion = Counter()
        for rel, meta in gt_photos.items():
            r = self.rows.get(rel)
            if not r or not meta["source"] or meta["corrupt"]:
                continue
            expect = meta["source"]
            got = r["source_kind"] or "unknown"
            src_total += 1
            if got == expect or (expect == "phone" and got in ("phone", "camera")):
                src_ok += 1
            else:
                confusion[f"{expect}->{got}"] += 1
        return {
            "expected_files": len(expected), "indexed": len(found), "missing_from_index": sorted(missing)[:10],
            "unexpected": sorted(extra)[:10], "status_ok": ok, "errors": len(errors),
            "corrupt_detected": len(detected_corrupt & expected_corrupt), "corrupt_expected": len(expected_corrupt),
            "error_examples": errors[:5],
            "date_exact": exact, "date_within_day": near, "date_wrong": wrong,
            "date_accuracy": round(exact / max(exact + near + wrong, 1), 4),
            "date_wrong_examples": wrong_examples,
            "source_accuracy": round(src_ok / max(src_total, 1), 4), "source_confusion": confusion.most_common(6),
        }

    def name_people(self) -> dict:
        """Simulate the user labelling each discovered cluster, which is what makes
        person-name search work. Names the single best-matching cluster per identity."""
        from photointel.engine.people import rename_person

        photo_persons = defaultdict(set)
        for pid, person in self.conn.execute(
                "SELECT photo_id, person_id FROM faces WHERE person_id IS NOT NULL"):
            photo_persons[int(pid)].add(int(person))
        person_photos = defaultdict(set)
        for photo_id, persons in photo_persons.items():
            rel = self.by_id.get(photo_id)
            if rel is None:
                continue
            for pp in persons:
                person_photos[pp].add(rel)
        named, used = {}, set()
        for name, paths in sorted(self.gt["people"].items(), key=lambda kv: -len(kv[1])):
            truth = set(paths)
            best, best_f1 = None, 0.0
            for pid, got in person_photos.items():
                if pid in used or not got:
                    continue
                inter = len(truth & got)
                if not inter:
                    continue
                score = f1(inter / len(got), inter / len(truth))
                if score > best_f1:
                    best, best_f1 = pid, score
            if best is not None:
                used.add(best)
                rename_person(self.conn, best, name)
                named[name] = best
        return named

    # ---------------------------------------------------------------- faces / people
    def eval_people(self) -> dict:
        gt_people = self.gt["people"]
        # DB: photo -> set(person_id)
        photo_persons: dict[int, set[int]] = defaultdict(set)
        for pid, person in self.conn.execute(
                "SELECT photo_id, person_id FROM faces WHERE person_id IS NOT NULL"):
            photo_persons[int(pid)].add(int(person))
        person_photos: dict[int, set[str]] = defaultdict(set)
        for photo_id, persons in photo_persons.items():
            rel = self.by_id.get(photo_id)
            if rel is None:
                continue
            for p in persons:
                person_photos[p].add(rel)

        results = {}
        used: set[int] = set()
        for name, paths in sorted(gt_people.items(), key=lambda kv: -len(kv[1])):
            truth = set(paths)
            best, best_f1 = None, -1.0
            for pid, got in person_photos.items():
                if pid in used:
                    continue
                inter = len(truth & got)
                if not inter:
                    continue
                prec = inter / len(got)
                rec = inter / len(truth)
                score = f1(prec, rec)
                if score > best_f1:
                    best, best_f1 = pid, score
            if best is None:
                results[name] = {"matched_person": None, "precision": 0, "recall": 0, "f1": 0,
                                 "gt_photos": len(truth)}
                continue
            used.add(best)
            got = person_photos[best]
            inter = len(truth & got)
            prec, rec = inter / len(got), inter / len(truth)
            # fragmentation: how many clusters hold this identity's photos
            frags = sum(1 for pid, ph in person_photos.items() if len(ph & truth) >= max(3, 0.05 * len(truth)))
            results[name] = {"matched_person": best, "precision": round(prec, 4), "recall": round(rec, 4),
                             "f1": round(f1(prec, rec), 4), "gt_photos": len(truth), "found_photos": len(got),
                             "clusters_holding_identity": frags}
        macro_p = sum(r["precision"] for r in results.values()) / max(len(results), 1)
        macro_r = sum(r["recall"] for r in results.values()) / max(len(results), 1)
        total_faces = self.conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
        assigned = self.conn.execute("SELECT COUNT(*) FROM faces WHERE person_id IS NOT NULL").fetchone()[0]
        clusters = self.conn.execute(
            "SELECT COUNT(*) FROM persons WHERE merged_into IS NULL AND face_count > 0").fetchone()[0]
        return {"per_person": results, "macro_precision": round(macro_p, 4), "macro_recall": round(macro_r, 4),
                "macro_f1": round(f1(macro_p, macro_r), 4), "faces": total_faces, "faces_assigned": assigned,
                "clusters": clusters, "gt_people": len(gt_people)}

    # ---------------------------------------------------------------- events
    def eval_events(self) -> dict:
        gt_event_of = {rel: m["event"] for rel, m in self.gt["photos"].items() if m["event"]}
        db_event_of = {}
        for rel, r in self.rows.items():
            if r["event_id"] and rel in gt_event_of:
                db_event_of[rel] = r["event_id"]
        if not gt_event_of:
            return {}
        assigned = len(db_event_of)
        # BCubed over the photos that have a ground-truth event
        keys = [k for k in gt_event_of if k in db_event_of]
        by_gt: dict[str, list[str]] = defaultdict(list)
        by_db: dict[int, list[str]] = defaultdict(list)
        for k in keys:
            by_gt[gt_event_of[k]].append(k)
            by_db[db_event_of[k]].append(k)
        prec = rec = 0.0
        for k in keys:
            same_db = by_db[db_event_of[k]]
            same_gt = by_gt[gt_event_of[k]]
            correct = sum(1 for j in same_db if gt_event_of[j] == gt_event_of[k])
            prec += correct / len(same_db)
            rec += correct / len(same_gt)
        prec /= max(len(keys), 1)
        rec /= max(len(keys), 1)
        # per ground-truth event: which db event covers it, and its title
        # A multi-day ground-truth event is modelled as a trip containing day events,
        # so score coverage against the trip when one exists.
        trip_of = {int(r[0]): int(r[1]) for r in self.conn.execute(
            "SELECT id, parent_id FROM events WHERE parent_id IS NOT NULL")}
        trip_event_of = {rel: trip_of.get(eid, eid) for rel, eid in db_event_of.items()}
        keys_t = [k for k in gt_event_of if k in trip_event_of]
        by_gt_t, by_db_t = defaultdict(list), defaultdict(list)
        for k in keys_t:
            by_gt_t[gt_event_of[k]].append(k)
            by_db_t[trip_event_of[k]].append(k)
        prec_t = rec_t = 0.0
        for k in keys_t:
            same_db = by_db_t[trip_event_of[k]]
            same_gt = by_gt_t[gt_event_of[k]]
            correct = sum(1 for j in same_db if gt_event_of[j] == gt_event_of[k])
            prec_t += correct / len(same_db)
            rec_t += correct / len(same_gt)
        prec_t /= max(len(keys_t), 1)
        rec_t /= max(len(keys_t), 1)

        detail = {}
        for gt_key, paths in by_gt.items():
            counts = Counter(trip_event_of[p] for p in paths if p in trip_event_of) or \
                Counter(db_event_of[p] for p in paths)
            eid, n = counts.most_common(1)[0]
            row = self.conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
            place = None
            if row and row["place_id"]:
                pl = self.conn.execute("SELECT name, city, admin1 FROM places WHERE id=?", (row["place_id"],)).fetchone()
                place = pl["city"] or pl["name"] if pl else None
            trip = None
            if row and row["parent_id"]:
                t = self.conn.execute("SELECT auto_title FROM events WHERE id=?", (row["parent_id"],)).fetchone()
                trip = t["auto_title"] if t else None
            detail[gt_key] = {
                "gt_title": self.gt["events"].get(gt_key, {}).get("title"),
                "gt_place": self.gt["events"].get(gt_key, {}).get("place"),
                "gt_category": self.gt["events"].get(gt_key, {}).get("category"),
                "detected_title": row["user_title"] or row["auto_title"] if row else None,
                "detected_category": row["category"] if row else None,
                "detected_place": place, "trip": trip,
                "coverage": round(n / len(paths), 3), "fragments": len(counts),
                "gt_photos": len(paths),
            }
        return {"bcubed_precision": round(prec, 4), "bcubed_recall": round(rec, 4),
                "bcubed_f1": round(f1(prec, rec), 4),
                "trip_level_precision": round(prec_t, 4), "trip_level_recall": round(rec_t, 4),
                "trip_level_f1": round(f1(prec_t, rec_t), 4),
                "photos_with_gt_event": len(gt_event_of), "assigned_to_an_event": assigned,
                "db_events": self.conn.execute("SELECT COUNT(*) FROM events WHERE kind='event'").fetchone()[0],
                "db_trips": self.conn.execute("SELECT COUNT(*) FROM events WHERE kind='trip'").fetchone()[0],
                "per_event": detail}

    # ---------------------------------------------------------------- duplicates
    def eval_duplicates(self) -> dict:
        gt_groups = self.gt["duplicate_groups"]
        gt_pairs: set[tuple[str, str]] = set()
        for members in gt_groups.values():
            m = sorted(set(members))
            for i in range(len(m)):
                for j in range(i + 1, len(m)):
                    gt_pairs.add((m[i], m[j]))
        burst_pairs: set[tuple[str, str]] = set()
        for members in self.gt["burst_groups"].values():
            m = sorted(set(members))
            for i in range(len(m)):
                for j in range(i + 1, len(m)):
                    burst_pairs.add((m[i], m[j]))

        db_pairs: dict[tuple[str, str], str] = {}
        for g in self.conn.execute("SELECT id, kind FROM dup_groups"):
            members = sorted(self.by_id.get(int(r[0]), "") for r in self.conn.execute(
                "SELECT photo_id FROM dup_members WHERE group_id=?", (g["id"],)))
            members = [m for m in members if m]
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    key = (members[i], members[j])
                    if key not in db_pairs or g["kind"] != "similar":
                        db_pairs[key] = g["kind"]
        dup_db = {k for k, v in db_pairs.items() if v != "similar"}
        tp = len(dup_db & gt_pairs)
        fp = len(dup_db - gt_pairs)
        fn = len(gt_pairs - dup_db)
        # how bursts were classified
        burst_as_dup = len(burst_pairs & dup_db)
        burst_as_similar = len({k for k, v in db_pairs.items() if v == "similar"} & burst_pairs)
        # which duplicate kinds were missed
        missed_kinds = Counter()
        for pair in list(gt_pairs - dup_db)[:200]:
            for p in pair:
                kind = self.gt["photos"].get(p, {}).get("dup_kind")
                if kind and kind != "original":
                    missed_kinds[kind] += 1
        fp_examples = [list(p) for p in list(dup_db - gt_pairs)[:5]]
        return {"gt_pairs": len(gt_pairs), "detected_pairs": len(dup_db),
                "precision": round(tp / max(tp + fp, 1), 4), "recall": round(tp / max(tp + fn, 1), 4),
                "f1": round(f1(tp / max(tp + fp, 1), tp / max(tp + fn, 1)), 4),
                "missed_by_kind": missed_kinds.most_common(), "false_positive_examples": fp_examples,
                "burst_pairs": len(burst_pairs), "burst_flagged_as_duplicate": burst_as_dup,
                "burst_flagged_as_similar": burst_as_similar,
                "groups_by_kind": {r[0]: r[1] for r in self.conn.execute(
                    "SELECT kind, COUNT(*) FROM dup_groups GROUP BY kind")}}

    # ---------------------------------------------------------------- places
    def eval_places(self) -> dict:
        ok = total = 0
        confusion = Counter()
        for rel, meta in self.gt["photos"].items():
            if not meta["place"]:
                continue
            r = self.rows.get(rel)
            if not r or not r["place_id"]:
                continue
            pl = self.conn.execute("SELECT name, city, admin1, country FROM places WHERE id=?",
                                   (r["place_id"],)).fetchone()
            total += 1
            got = {pl["name"], pl["city"], pl["admin1"]}
            if meta["place"] in got:
                ok += 1
            else:
                confusion[f"{meta['place']}->{pl['city'] or pl['name']}"] += 1
        with_gps = sum(1 for r in self.rows.values() if r["gps_lat"] is not None)
        geocoded = sum(1 for r in self.rows.values() if r["place_id"] is not None)
        inferred = sum(1 for r in self.rows.values()
                       if r["place_id"] is not None and r["location_source"] != "gps")
        return {"gps_photos": with_gps, "geocoded": geocoded, "inferred_locations": inferred,
                "city_accuracy": round(ok / max(total, 1), 4), "checked": total,
                "mismatches": confusion.most_common(5)}

    # ---------------------------------------------------------------- search
    def eval_search(self) -> dict:
        from photointel.search.engine import SearchEngine

        engine = SearchEngine(self.ctx)
        gt_people = self.gt["people"]
        gt_photos = self.gt["photos"]

        def truth_for(pred) -> set[str]:
            return {rel for rel, m in gt_photos.items() if pred(rel, m)}

        # "structured" = the answer is defined by metadata this library really has
        # (who, where, when, what kind of file). Those are the rows this dataset can
        # grade. The "visual" rows cannot be graded here and are reported separately:
        # the generator composites faces onto arbitrary COCO scenes, so a photo whose
        # *event* is a wedding shows, say, a coffee cup. The pixels carry no wedding,
        # so a correct system scores near zero. Real visual-search accuracy is
        # measured against COCO instance labels by eval/eval_visual_search.py.
        cases = [
            ("photos of Ghat", set(gt_people.get("Ghat", [])), "structured"),
            ("Ghat and Priya together",
             set(gt_people.get("Ghat", [])) & set(gt_people.get("Priya", [])), "structured"),
            ("show photos from Hyderabad", truth_for(lambda r, m: m["place"] == "Hyderabad"), "structured"),
            ("wedding photos", truth_for(lambda r, m: m["event"] == "wedding_vjw"), "visual"),
            ("beach photos", truth_for(lambda r, m: m["event"] in ("goa_2023", "vizag_2024")), "visual"),
            ("photos from 2023", truth_for(lambda r, m: m["taken"].startswith("2023")), "structured"),
            ("Ghat in Goa", {p for p in gt_people.get("Ghat", []) if gt_photos[p]["place"] == "Calangute"},
             "structured"),
            ("screenshots", truth_for(lambda r, m: m["source"] == "screenshot"), "structured"),
            ("photos of Lakshmi in Tirupati",
             {p for p in gt_people.get("Lakshmi", []) if gt_photos[p]["event"] == "tirupati_2024"}, "structured"),
        ]
        out = {}
        for query, truth, kind in cases:
            res = engine.search(self.conn, query, limit=2000)
            got = {self.by_id.get(pid) for pid in res.photo_ids}
            got.discard(None)
            inter = len(got & truth)
            prec = inter / max(len(got), 1)
            rec = inter / max(len(truth), 1)
            # precision@20 is what the user actually sees first
            top = [self.by_id.get(pid) for pid in res.photo_ids[:20]]
            p20 = sum(1 for t in top if t in truth) / max(len(top), 1)
            out[query] = {"returned": len(got), "expected": len(truth), "precision": round(prec, 3),
                          "recall": round(rec, 3), "f1": round(f1(prec, rec), 3), "p@20": round(p20, 3),
                          "kind": kind,
                          "interpretation": [c["kind"] + ":" + c["label"] for c in res.interpretation]}
        gradable = [v for v in out.values() if v["kind"] == "structured"]
        n = len(gradable)
        return {"cases": out,
                "macro_precision": round(sum(v["precision"] for v in gradable) / n, 3),
                "macro_recall": round(sum(v["recall"] for v in gradable) / n, 3),
                "macro_p@20": round(sum(v["p@20"] for v in gradable) / n, 3),
                "macro_over": f"{n} structured queries; visual queries excluded "
                              f"(ungradable on synthetic pixels — see eval/eval_visual_search.py)"}

    def run(self, sections: list[str], name_people: bool = True) -> dict:
        fns = {"indexing": self.eval_indexing, "people": self.eval_people, "events": self.eval_events,
               "duplicates": self.eval_duplicates, "places": self.eval_places, "search": self.eval_search}
        if name_people and "search" in sections:
            self.report["naming"] = {"named": list(self.name_people())}
        for name in sections:
            self.report[name] = fns[name]()
        return self.report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--library", default="D:/pi_cache/testlib")
    ap.add_argument("--sections", default="indexing,people,events,duplicates,places,search")
    ap.add_argument("--json", help="write full report to this file")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-naming", action="store_true",
                    help="do not label discovered people before search evaluation")
    args = ap.parse_args()

    ev = Evaluator(args.data, Path(args.library))
    report = ev.run(args.sections.split(","), name_people=not args.no_naming)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    def line(k, v):
        print(f"  {k:32s} {v}")

    for section, data in report.items():
        print(f"\n=== {section.upper()} ===")
        if section == "people":
            line("faces / assigned", f"{data['faces']} / {data['faces_assigned']}")
            line("clusters (>=1 face)", data["clusters"])
            line("macro precision/recall/F1", f"{data['macro_precision']} / {data['macro_recall']} / {data['macro_f1']}")
            if not args.quiet:
                print(f"  {'person':10s} {'gt':>5} {'found':>6} {'prec':>6} {'recall':>7} {'F1':>6} {'frag':>5}")
                for name, r in data["per_person"].items():
                    print(f"  {name:10s} {r['gt_photos']:5d} {r.get('found_photos', 0):6d} "
                          f"{r['precision']:6.3f} {r['recall']:7.3f} {r['f1']:6.3f} "
                          f"{r.get('clusters_holding_identity', 0):5d}")
        elif section == "events":
            for k in ("bcubed_precision", "bcubed_recall", "bcubed_f1", "trip_level_precision",
                      "trip_level_recall", "trip_level_f1", "photos_with_gt_event",
                      "assigned_to_an_event", "db_events", "db_trips"):
                line(k, data.get(k))
            if not args.quiet:
                for key, d in data.get("per_event", {}).items():
                    print(f"  {key:16s} gt='{d['gt_title']}' -> '{d['detected_title']}' "
                          f"[{d['detected_category']}] place={d['detected_place']} trip={d['trip']} "
                          f"cover={d['coverage']} frags={d['fragments']}")
        elif section == "search":
            line("macro precision/recall", f"{data['macro_precision']} / {data['macro_recall']}")
            line("macro p@20", data["macro_p@20"])
            line("macro over", data.get("macro_over", ""))
            if not args.quiet:
                for q, r in data["cases"].items():
                    flag = "" if r.get("kind") == "structured" else "   [ungradable: pixels are unrelated to the label]"
                    print(f"  {q:34s} P={r['precision']:.2f} R={r['recall']:.2f} p@20={r['p@20']:.2f} "
                          f"({r['returned']}/{r['expected']}) {r['interpretation']}{flag}")
        else:
            for k, v in data.items():
                if isinstance(v, (int, float, str)) or (isinstance(v, list) and len(v) < 8):
                    line(k, v)


if __name__ == "__main__":
    main()
