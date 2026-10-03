"""The Takeout sidecar matcher: the indexed implementation must pick exactly the sidecar the
original linear scan picked, for every input. The original is kept below as the oracle."""
from __future__ import annotations

import random

import pytest

from photointel.engine import takeout as T
from photointel.engine.takeout import (EDIT_SUFFIXES, MIN_TRUNCATED, NOT_SIDECARS, SUPPLEMENTAL,
                                       _MEDIA_NAME, _NUM_AT_END, _stem_variants)


class OldFolderSidecars:
    """Verbatim copy of the pre-optimisation O(photos x sidecars) implementation."""

    def __init__(self, names):
        self.entries = []
        for n in names:
            if n.lower() in NOT_SIDECARS or not n.lower().endswith(".json"):
                continue
            m = _NUM_AT_END.match(n[:-5])
            self.entries.append((m.group(1).lower(), m.group(2), n))

    def match(self, media_name):
        m = _MEDIA_NAME.match(media_name)
        if not m:
            return None
        stem, num, ext = m.group(1), m.group(2), m.group(3)
        for base_stem in _stem_variants(stem):
            hit = self._match_one(f"{base_stem}{ext}".lower(), base_stem.lower(), num)
            if hit:
                return hit
        return None

    def _match_one(self, name, stem, num):
        full = name + SUPPLEMENTAL
        best, best_rank = None, 99
        for core, jnum, fname in self.entries:
            if jnum != num:
                continue
            if core == name:
                rank = 0
            elif core == full:
                rank = 1
            elif full.startswith(core) and (len(core) > len(name) or len(core) >= MIN_TRUNCATED):
                rank = 2
            elif core == stem:
                rank = 3
            else:
                continue
            if rank < best_rank or (rank == best_rank and best is not None and len(fname) > len(best)):
                best, best_rank = fname, rank
        return best


def _compare(jsons, medias):
    old, new = OldFolderSidecars(jsons), T._FolderSidecars(jsons)
    for media in medias:
        assert new.match(media) == old.match(media), (media, jsons)


FIXTURE_CASES = [
    ("IMG_1.jpg", ["IMG_1.jpg.json"]),
    ("IMG_1.jpg", ["IMG_1.jpg.supplemental-metadata.json"]),
    ("IMG_1.jpg", ["IMG_1.jpg.supplemental-me.json"]),
    ("IMG_1(2).jpg", ["IMG_1.jpg(2).json", "IMG_1.jpg.json"]),
    ("IMG_1(2).jpg", ["IMG_1.jpg.supplemental-metadata(2).json"]),
    ("IMG_1-edited.jpg", ["IMG_1.jpg.supplemental-metadata.json"]),
    ("20030616.jpg", ["20030616.json"]),
    ("IMG_1.jpg", ["IMG_10.jpg.json", "metadata.json"]),
    ("IMG_1(2).jpg", ["IMG_1.jpg.json"]),
    ("PXL_20191231_235959123.NIGHT.long-description-name.jpg",
     [("PXL_20191231_235959123.NIGHT.long-description-name.jpg" + SUPPLEMENTAL)[:46] + ".json"]),
    ("image(1).jpg", ["image.jpg.supplemental-metadata(1).json"]),
    ("IMG_20190301_101010.jpg", ["IMG_20190301_101010.jpg.supplemental-metadata.json", "metadata.json"]),
    ("scan_of_grandma.jpg", ["scan_of_grandma.jpg.supplemental-metadata.json"]),
]


@pytest.mark.parametrize("media,jsons", FIXTURE_CASES)
def test_fixtures_match_oracle(media, jsons):
    _compare(jsons, [media])


def test_pinned_answers():
    f = T._FolderSidecars
    assert f(["IMG_1.jpg.json"]).match("IMG_1.jpg") == "IMG_1.jpg.json"
    assert f(["IMG_1(2).jpg"[:0] + "IMG_1.jpg(2).json", "IMG_1.jpg.json"]).match("IMG_1(2).jpg") == "IMG_1.jpg(2).json"
    assert f(["IMG_1.jpg.json"]).match("IMG_1(2).jpg") is None
    assert f([]).match("a.jpg") is None


_STEMS = ["IMG_0001", "PXL_20191231_235959123.NIGHT", "photo", "Résumé café", "写真", "a.b.c", "x[1]", "IMG_1",
          "IMG_10", "DSC (copy)", "very-long-descriptive-file-name-that-keeps-going-and-going", "UPPER", "upper",
          "weird.name.with.dots.and[brackets]", "IMG_1.jpg", "a"]
_EXTS = [".jpg", ".JPG", ".png", ".mp4", ".heic", ".MP4", ".jpeg"]


def _random_folder(rng):
    medias, jsons = [], []
    for _ in range(rng.randint(1, 14)):
        stem = rng.choice(_STEMS) + rng.choice(["", "", "_" + str(rng.randint(0, 30))])
        ext = rng.choice(_EXTS)
        dup = rng.choice(["", "", "", "(1)", "(2)", "(10)"])
        edit = rng.choice(["", "", "", rng.choice(EDIT_SUFFIXES), rng.choice(EDIT_SUFFIXES).upper()])
        medias.append(f"{stem}{edit}{dup}{ext}")
        base = f"{stem}{ext}"
        style = rng.randrange(9)
        if style == 0:
            j = f"{base}{dup}.json"                       # the Takeout form: name.ext(n).json
        elif style == 1:
            j = f"{base}.json"
        elif style == 2:
            j = f"{base}{SUPPLEMENTAL}{dup}.json"
        elif style == 3:
            cut = rng.randint(1, 46)
            j = f"{(base + SUPPLEMENTAL)[:cut]}{dup}.json"   # truncation, any length
        elif style == 4:
            j = f"{(base + SUPPLEMENTAL)[:46]}{dup}.json"
        elif style == 5:
            j = f"{stem}{dup}.json"                       # no extension
        elif style == 6:
            j = f"{base.upper()}{SUPPLEMENTAL}{dup}.json"   # case
        elif style == 7:
            j = rng.choice(["metadata.json", "other.json", "Metadata.JSON", "x.txt", "(1).json", ".json"])
        else:
            j = f"{base}{SUPPLEMENTAL}{rng.choice(['', '(1)', '(2)'])}.json"
        jsons.append(j)
        if rng.random() < 0.3:                            # sidecars that match several photos / ties
            jsons.append(rng.choice(jsons))
            jsons.append(j + "" if rng.random() < .5 else j.replace(".json", ".JSON"))
    medias += [rng.choice(jsons).replace(".json", "") for _ in range(3)] + ["noext", "(1).jpg", ".jpg", "a..jpg"]
    rng.shuffle(jsons)
    return medias, jsons


def test_randomised_folders_match_oracle():
    rng = random.Random(20261003)
    checked = 0
    for _ in range(1500):
        medias, jsons = _random_folder(rng)
        _compare(jsons, medias)
        checked += len(medias)
    assert checked > 5000


def test_randomised_big_folder_with_many_sidecars():
    rng = random.Random(7)
    medias, jsons = [], []
    for _ in range(60):
        m, j = _random_folder(rng)
        medias += m
        jsons += j
    _compare(jsons, medias)
    # and the result is not trivially None
    new = T._FolderSidecars(jsons)
    assert sum(new.match(m) is not None for m in medias) > len(medias) // 4
