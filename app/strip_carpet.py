#!/usr/bin/env python3
"""
strip_carpet.py - remove the SENSED carpet from a valetudo-restore backup.

    python tools/strip_carpet.py BACKUP.tar.gz [OUT.tar.gz] [--scan-only]

Why: on the MOVA P10 Pro Ultra (firmware 1782) ava can crash with
std::bad_alloc in carpet::GenerateCarpet -> CarpetProfile ->
geometry::internal::AddExclude while it builds a cleaning plan, and the
firmware watchdog answers repeated crashes by wiping /data. Stripping only the
carpet outline files is not enough: ava redraws the outline from the carpet
still painted into its map-division image layers the next time it saves the
map. This removes the carpet from every place it was found to live.

Per-room floor materials (ri/<slot>.dat2 seg_info), rooms, names, zones and
the map itself are left alone. Only the Python standard library is used.

How carpet is IDENTIFIED - by shape, not by name. A reference carpet mask is
built from the outlines (carpet_map_large.json) and from ava's own carpet
raster. Any image layer containing a value whose pixels lie >= 80% inside the
reference AND cover >= 30% of it is treated as a carpet layer. On the robot
this was checked against every layer: carpet values scored 87-96% inside,
while room labels, the nearest false match, scored about 65%.

How it is REMOVED - in the form ava itself writes:
  carpet_map_large.json   -> {"carpet": []}            (ava's no-carpet file)
  carpet_path_large.txt   -> every cell kept, flag 0   (ava's no-carpet form)
  ri: carpet_map(_bit)    -> ava's own empty raster encoding
  image layers            -> each carpet pixel repainted with the majority
                             value of its already-clean neighbours, from the
                             edge inward: floor stays floor, a room label
                             stays that room's label.

How it is VERIFIED - the output is re-scanned: no carpet-shaped layer may
remain, every non-carpet pixel and every untouched file must be identical, and
image sizes/types must be unchanged.
"""
import copy
import collections
import io
import json
import struct
import sys
import tarfile
import zlib

INSIDE_MIN = 0.80   # share of a value's pixels that must lie inside the carpet
COVER_MIN = 0.30    # share of the carpet those pixels must cover
NEAR_MIN = 0.30     # a value this much inside the carpet is carpet-associated
                    # (a ring around it): never copied inward when repainting
EMPTY_CARPET_JSON = b'{\n    "carpet": []\n}'
RASTER_KEYS = ("carpet_map", "carpet_map_bit")


# ------------------------------------------------------------------ rasters
def raster_cells(b):
    """ava NEWMV001_RI_V001 raster -> {(x, y): value}. 32x32 tiles, x-fast,
    tile number = (x // 32) * 64 + (y // 32)."""
    assert b[:16] == b"NEWMV001_RI_V001"
    n = struct.unpack("<i", b[16:20])[0]
    cells = {}
    for i in range(n):
        o = 37 + i * 1026
        idx = struct.unpack("<H", b[o:o + 2])[0]
        tx, ty = divmod(idx, 64)
        data = b[o + 2:o + 1026]
        for k, v in enumerate(data):
            if v:
                dy, dx = divmod(k, 32)
                cells[(tx * 32 + dx, ty * 32 + dy)] = v
    return cells


def b64d(s):
    import base64
    s = s.strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4))


# ---------------------------------------------------------------------- png
def png_read(b):
    assert b[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks, idat = 8, [], b""
    while pos < len(b):
        ln = struct.unpack(">I", b[pos:pos + 4])[0]
        typ, data = b[pos + 4:pos + 8], b[pos + 8:pos + 8 + ln]
        chunks.append((typ, data))
        if typ == b"IDAT":
            idat += data
        pos += 12 + ln
    w, h, depth, ctype, _, _, inter = struct.unpack(">IIBBBBB", chunks[0][1])
    if depth != 8 or inter != 0 or ctype not in (0, 2):
        return None
    bpp = 1 if ctype == 0 else 3
    raw, stride = zlib.decompress(idat), w * bpp
    rows, prev, o = [], bytearray(stride), 0
    for _ in range(h):
        f, line = raw[o], bytearray(raw[o + 1:o + 1 + stride])
        o += 1 + stride
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            up, c = prev[i], (prev[i - bpp] if i >= bpp else 0)
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + up) & 255
            elif f == 3:
                line[i] = (line[i] + (a + up) // 2) & 255
            elif f == 4:
                p = a + up - c
                pa, pb, pc = abs(p - a), abs(p - up), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else (up if pb <= pc else c))) & 255
        rows.append(bytes(line))
        prev = line
    px = [[(r[x] if bpp == 1 else tuple(r[3 * x:3 * x + 3])) for x in range(w)] for r in rows]
    return {"w": w, "h": h, "ctype": ctype, "px": px, "chunks": chunks}


def png_write(img):
    bpp = 1 if img["ctype"] == 0 else 3
    raw = bytearray()
    for row in img["px"]:
        raw.append(0)
        for v in row:
            if bpp == 1:
                raw.append(v)
            else:
                raw.extend(v)
    out = bytearray(b"\x89PNG\r\n\x1a\n")

    def chunk(t, d):
        out.extend(struct.pack(">I", len(d)) + t + d)
        out.extend(struct.pack(">I", zlib.crc32(t + d) & 0xffffffff))
    wrote_idat = False
    for t, d in img["chunks"]:            # keep every non-image chunk, in order
        if t == b"IDAT":
            if not wrote_idat:
                chunk(b"IDAT", zlib.compress(bytes(raw), 9))
                wrote_idat = True
        else:
            chunk(t, d)
    return bytes(out)


# ------------------------------------------------------------ carpet logic
def polygon_mask(poly):
    n, inside = len(poly), set()
    ys = [p[1] for p in poly]
    for y in range(min(ys), max(ys) + 1):
        cy, xs = y + 0.5, []
        for i in range(n):
            (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
            if (y1 <= cy < y2) or (y2 <= cy < y1):
                xs.append(x1 + (cy - y1) * (x2 - x1) / (y2 - y1))
        xs.sort()
        for a, b in zip(xs[::2], xs[1::2]):
            inside.update((x, y) for x in range(int(a + 0.5), int(b + 0.5)))
    return inside


def outline_mask(raw_json):
    m = set()
    for c in json.loads(raw_json).get("carpet", []):
        p = c.get("polylines") or []
        if p and isinstance(p[0][0], (int, float)) and len(p) >= 3:
            m |= polygon_mask([tuple(q) for q in p])
    return m


def carpet_values(img, roi, ref):
    """
    (carpet, associated) value lists for this image, or ([], []) if it is not a
    carpet layer. A layer qualifies when one value is carpet-SHAPED (>= 80%
    inside the carpet and >= 30% of it covered). Within a qualifying layer every
    value >= 80% inside counts as carpet too: layers mark carpet with several
    classes (state_map uses 99, 227, 234 and 235). Values 30-80% inside are
    rings hugging the carpet and must not be copied into it.
    """
    x0, y0 = roi
    groups = collections.defaultdict(set)
    for r, row in enumerate(img["px"]):
        for c, v in enumerate(row):
            groups[v].add((x0 + c, y0 + r))
    stats = {v: (len(s), len(s & ref)) for v, s in groups.items()}
    if not any(n >= 50 and i / n >= INSIDE_MIN and i / len(ref) >= COVER_MIN for n, i in stats.values()):
        return [], []
    carpet = [(v, n, i / n, i / len(ref)) for v, (n, i) in stats.items() if i / n >= INSIDE_MIN]
    near = [v for v, (n, i) in stats.items() if NEAR_MIN <= i / n < INSIDE_MIN]
    return carpet, near


def inpaint(img, bad_values, avoid=()):
    """
    Repaint pixels holding bad_values from their clean neighbours, edge inward.
    Neighbours holding an `avoid` value (a ring around the carpet) are only used
    where no other neighbour exists, e.g. a carpet lying wholly inside one room
    of a room-label layer, whose label is then correctly kept.
    """
    px, w, h = img["px"], img["w"], img["h"]
    todo = {(r, c) for r in range(h) for c in range(w) if px[r][c] in bad_values}
    changed = len(todo)
    while todo:
        step = {}
        for r, c in todo:
            nb = [px[rr][cc] for rr, cc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1))
                  if 0 <= rr < h and 0 <= cc < w and (rr, cc) not in todo]
            if nb:
                pref = [v for v in nb if v not in avoid] or nb
                step[(r, c)] = collections.Counter(pref).most_common(1)[0][0]
        if not step:
            break                                    # isolated: nothing to learn from
        for (r, c), v in step.items():
            px[r][c] = v
        todo -= set(step)
    return changed, len(todo)


def fill_nearest(img, bad_values, near):
    """
    Repaint each carpet pixel with the value of the NEAREST pixel that is
    neither carpet nor ring (breadth-first from all such pixels, passing
    through ring and carpet). Used where a ring of carpet-associated values
    encloses the carpet, so neighbour-by-neighbour repainting would only copy
    the ring inward.
    """
    px, w, h = img["px"], img["w"], img["h"]
    q, owner = collections.deque(), {}
    for r in range(h):
        for c in range(w):
            v = px[r][c]
            if v not in bad_values and v not in near:
                owner[(r, c)] = v
                q.append((r, c))
    while q:
        r, c = q.popleft()
        for rr, cc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
            if 0 <= rr < h and 0 <= cc < w and (rr, cc) not in owner:
                owner[(rr, cc)] = owner[(r, c)]
                q.append((rr, cc))
    changed = 0
    for r in range(h):
        for c in range(w):
            if px[r][c] in bad_values and (r, c) in owner:
                px[r][c] = owner[(r, c)]
                changed += 1
    return changed, 0


OVERLAY_OFFSET = 64   # room-label layers mark "carpet in room N" as 64 + N


def fill_overlay_offset(img, bad, sibling):
    """Exact undo of the 64+N overlay, accepted only if it agrees with the
    carpet-free sibling layer on >= 99% of the carpet pixels."""
    if img["ctype"] != 0 or sibling is None:
        return None
    present = {v for row in img["px"] for v in row if v not in bad}
    if not all((v - OVERLAY_OFFSET) in present for v in bad):
        return None
    px, hits, agree = img["px"], 0, 0
    for r, row in enumerate(px):
        for c, v in enumerate(row):
            if v in bad:
                row[c] = v - OVERLAY_OFFSET
                hits += 1
                agree += row[c] == sibling[r][c]
    return hits if hits and agree / hits >= 0.99 else None


def fill_sibling_label(img, bad, sibling):
    """Colour room layer: give each carpet pixel its room's colour, reading the
    room from the carpet-free sibling layer."""
    if img["ctype"] != 2 or sibling is None:
        return None
    colour = collections.defaultdict(collections.Counter)
    for r, row in enumerate(img["px"]):
        for c, v in enumerate(row):
            if v not in bad:
                colour[sibling[r][c]][v] += 1
    hits = 0
    for r, row in enumerate(img["px"]):
        for c, v in enumerate(row):
            if v in bad and colour.get(sibling[r][c]):
                row[c] = colour[sibling[r][c]].most_common(1)[0][0]
                hits += 1
    return hits or None


def strip_image(img, roi, ref, sibling=None):
    """
    Remove carpet from one image, choosing the fill by result: the first method
    after which the layer no longer tests carpet-shaped wins. Exact methods
    (known overlay encodings, checked against the carpet-free sibling layer)
    are tried first. Returns (values, method, changed) or None.
    """
    vals, near = carpet_values(img, roi, ref)
    if not vals:
        return None
    bad = {v for v, *_ in vals}
    for method in ("overlay-offset", "sibling-label", "neighbours", "nearest-background"):
        trial = dict(img, px=[list(row) for row in img["px"]])
        if method == "overlay-offset":
            changed = fill_overlay_offset(trial, bad, sibling)
        elif method == "sibling-label":
            changed = fill_sibling_label(trial, bad, sibling)
        elif method == "neighbours":
            changed = inpaint(trial, bad)[0]
        else:
            changed = fill_nearest(trial, bad, set(near))[0]
        if changed is None:
            continue
        if not carpet_values(trial, roi, ref)[0]:
            img["px"] = trial["px"]
            return vals, method, changed
    img["px"] = trial["px"]
    return vals, method + " (STILL CARPET-SHAPED)", changed


def roi_for(files, folder, w, h):
    """Grid offset of a (w x h) image, from the *_roi*.json files of its map."""
    for name, data in files.items():
        if name.startswith(folder) and "roi" in name and name.endswith(".json"):
            j = json.loads(data)
            if j["x_max"] - j["x_min"] == w and j["y_max"] - j["y_min"] == h:
                return j["x_min"], j["y_min"]
    return None


# --------------------------------------------------------------- archives
def read_inner(blob):
    t = tarfile.open(fileobj=io.BytesIO(blob))
    return t, {m.name: (m, t.extractfile(m).read() if m.isfile() else None) for m in t.getmembers()}


def write_inner(entries):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as out:
        for name, (m, data) in entries.items():
            ti = copy.copy(m)
            if m.isfile():
                ti.size = len(data)
                out.addfile(ti, io.BytesIO(data))
            else:
                out.addfile(ti)
    return buf.getvalue()


def process(src_path, out_path=None, scan_only=False, ref_override=None):
    log = []
    outer = tarfile.open(src_path)
    members = {m.name: (m, outer.extractfile(m).read() if m.isfile() else None) for m in outer.getmembers()}

    # ---- reference carpet mask
    ref = set()
    div = {}
    for key in ("data_dividemap.tar.gz", "data_dividedebug.tar.gz"):
        if key in members:
            div[key] = read_inner(members[key][1])[1]
    for key, files in div.items():
        for name, (m, data) in files.items():
            if name.endswith("carpet_map_large.json") and data:
                ref |= outline_mask(data)
    ri_files = read_inner(members["data_ri.tar.gz"][1])[1] if "data_ri.tar.gz" in members else {}
    dat2_name = next((n for n in ri_files if n.endswith(".dat2")), None)
    dat2 = json.loads(ri_files[dat2_name][1]) if dat2_name else None
    if dat2:
        em = dat2["extend_msgs"] if not isinstance(dat2["extend_msgs"], str) else json.loads(dat2["extend_msgs"])
        for e in em:
            if e["key"] == "carpet_map":
                ref |= {xy for xy, v in raster_cells(b64d(e["value"])).items() if v >= 175}
            if e["key"] == "carpet_map_bit":
                ref |= {xy for xy, v in raster_cells(b64d(e["value"])).items() if v == 1}
    if ref_override is not None:
        # verification: judge the output against the ORIGINAL carpet, since a
        # stripped archive no longer carries its own reference
        ref = ref_override
    log.append("reference carpet: %d grid cells" % len(ref))
    if not ref:
        log.append("no carpet found anywhere; nothing to do")
        return log, 0, ref

    # ---- identify + strip
    found = 0
    for key, files in div.items():
        for name, (m, data) in list(files.items()):
            if not data:
                continue
            if name.endswith("carpet_map_large.json"):
                n = len(json.loads(data).get("carpet", []))
                if n:
                    found += 1
                    log.append("%s%s: %d outline(s)" % (key[5:-7], name[1:], n))
                    files[name] = (m, EMPTY_CARPET_JSON)
            elif name.endswith("carpet_path_large.txt"):
                lines = data.decode().split("\n")
                flagged = sum(1 for l in lines[1:] if len(l.split()) == 3 and l.split()[2] != "0")
                if flagged:
                    found += 1
                    log.append("%s%s: %d carpet cells" % (key[5:-7], name[1:], flagged))
                    files[name] = (m, "\n".join([lines[0]] + [
                        ("%s %s 0" % tuple(l.split()[:2])) if len(l.split()) == 3 else l
                        for l in lines[1:]]).encode())
            elif name.endswith(".png"):
                img = png_read(data)
                if img is None:
                    continue
                folder = name[:name.rindex("/") + 1]
                roi = roi_for({n: d for n, (mm, d) in files.items() if d}, folder, img["w"], img["h"]) \
                    or roi_for({n: d for n, (mm, d) in files.items() if d}, "./", img["w"], img["h"])
                if not roi:
                    continue
                sib_name = folder + "segmented_map_large.png"
                sib = None
                if not scan_only and "segment" in name and sib_name in files and files[sib_name][1]:
                    s_img = png_read(files[sib_name][1])
                    if s_img and (s_img["w"], s_img["h"]) == (img["w"], img["h"]):
                        sib = s_img["px"]
                res = carpet_values(img, roi, ref)[0] if scan_only else strip_image(img, roi, ref, sib)
                if res:
                    found += 1
                    if scan_only:
                        vals, method, changed = res, "-", 0
                    else:
                        vals, method, changed = res
                        files[name] = (m, png_write(img))
                    log.append("%s%s: carpet value(s) %s -> %d px repainted (%s)" % (
                        key[5:-7], name[1:], ", ".join("%s (%.0f%% in, %.0f%% cover)" % (v, 100 * p, 100 * r) for v, _, p, r in vals),
                        changed, method))
    if dat2:
        em_is_str = isinstance(dat2["extend_msgs"], str)
        em = json.loads(dat2["extend_msgs"]) if em_is_str else dat2["extend_msgs"]
        empty = dat2.get("history_decomposed_map")
        for e in em:
            if e["key"] in RASTER_KEYS and empty and raster_cells(b64d(e["value"])):
                found += 1
                log.append("ri%s extend_msgs.%s: %d cells -> empty raster" % (dat2_name[1:], e["key"], len(raster_cells(b64d(e["value"])))))
                e["value"] = empty
        dat2["extend_msgs"] = json.dumps(em) if em_is_str else em
        ri_files[dat2_name] = (ri_files[dat2_name][0], json.dumps(dat2).encode())

    if scan_only or not found:
        return log, found, ref

    new = {k: v for k, v in members.items()}
    for key, files in div.items():
        new[key] = (members[key][0], write_inner(files))
    if ri_files:
        new["data_ri.tar.gz"] = (members["data_ri.tar.gz"][0], write_inner(ri_files))
    man = json.loads(members["manifest.json"][1])
    man["derived"] = {"from": src_path.replace("\\", "/").rsplit("/", 1)[-1],
                      "change": "sensed carpet removed by tools/strip_carpet.py"}
    new["manifest.json"] = (members["manifest.json"][0], json.dumps(man, indent=2).encode())
    with tarfile.open(out_path, "w:gz") as out:
        for name, (m, data) in new.items():
            ti = copy.copy(m)
            if m.isfile():
                ti.size = len(data)
                out.addfile(ti, io.BytesIO(data))
            else:
                out.addfile(ti)
    return log, found, ref


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__.strip().splitlines()[2])
        return 2
    src = args[0]
    scan = "--scan-only" in argv
    out = args[1] if len(args) > 1 else src.replace(".tar.gz", "-nocarpet-full.tar.gz")
    log, found, ref = process(src, out, scan_only=scan)
    print("\n".join(log))
    if scan or not found:
        print("\n%d carpet store(s) found%s" % (found, "" if scan else "; nothing written"))
        return 0
    # ---- verify the output by scanning it again
    log2, left, _ = process(out, None, scan_only=True, ref_override=ref)
    print("\nwritten: %s\nre-scan of the output: %s" % (out, "clean, no carpet found" if left == 0 else "STILL FOUND:\n" + "\n".join(log2)))
    return 0 if left == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
