#!/usr/bin/env python3
"""
ldraw2svg.py - render LDraw parts and models (.dat / .ldr / .mpd) as SVG.

Orthographic projection. Default view: 30 degrees from above, rotated 45 degrees
(front face on the left, right side on the right); more fixed views via --view.
Pure Python 3, no dependencies.

Usage:
    python3 ldraw2svg.py 3001.dat                  # -> 3001.svg
    python3 ldraw2svg.py 3001 3003 3020            # parts straight from the library
    python3 ldraw2svg.py 3001 -c 1 --size 800      # blue (LDraw code), 800 px
    python3 ldraw2svg.py 3001 -c "#0055BF"         # custom base colour, shading derived
    python3 ldraw2svg.py 3001 --wireframe          # lines only, no faces
    python3 ldraw2svg.py 3001 --hidden-line        # visible lines only
    python3 ldraw2svg.py model.ldr -m -c "#333"    # whole model in one colour
    python3 ldraw2svg.py model.mpd -o image.svg
    python3 ldraw2svg.py 3001 --view front         # front view (iso, iso-left, front, back,
                                                   #   left, right, top, bottom)
    python3 ldraw2svg.py 3001 --angles 20,-30      # custom angles

The LDraw library is looked up via: --ldraw DIR, $LDRAWDIR, the folders above the
input file, then common install locations (incl. BrickLink Studio).

What it does:
  * Sub-files (type 1) are resolved recursively, incl. MPD sub-files, colour
    inheritance (16/24), LDConfig.ldr colours and direct colours (0x2RRGGBB).
  * BFC (CERTIFY, CW/CCW, INVERTNEXT, CLIP/NOCLIP, mirrored matrices) is
    honoured; back faces are culled.
  * Faces are ordered by actual 2D overlap (not just centroid depth), so that
    e.g. rear studs are correctly drawn on top of the top face.
  * Edges (type 2) and conditional lines (type 5) are clipped exactly against
    occluding faces and drawn last.
"""

import argparse
import heapq
import math
import os
import sys
from collections import defaultdict

# ------------------------------------------------------------------- View --
# Views: (elevation, rotation about the vertical axis) in degrees.
# lat > 0 = looking from above; lon > 0 = right side (+X) turns towards the viewer.
VIEWS = {
    "iso":      (30.0, 45.0),    # front + right side + top (default)
    "iso-left": (30.0, -45.0),   # front + left side + top
    "front":    (0.0, 0.0),
    "back":     (0.0, 180.0),
    "right":    (0.0, 90.0),
    "left":     (0.0, -90.0),
    "top":      (90.0, 0.0),     # top view, front at the bottom
    "bottom":   (-90.0, 0.0),    # view from below
}
DEFAULT_VIEW = "iso"
LIGHT = (-0.5, -1.0, -0.8) # direction towards the light in view space (y down, z away from viewer)
AMBIENT, DIFFUSE = 0.60, 0.45
DEFAULT_COLOR = 4          # colour for "16" at the top level (4 = Red)
DEFAULT_SIZE = 512         # longest image side in px
MARGIN = 8                 # margin in px

# fallback colour table if no LDConfig.ldr is found: code -> (rgb, edge, alpha)
FALLBACK_COLORS = {
    0: ("#1B2A34", "#808080", 255), 1: ("#1E5AA8", "#333333", 255),
    2: ("#00852B", "#333333", 255), 4: ("#B40000", "#333333", 255),
    7: ("#8A928D", "#333333", 255), 8: ("#545955", "#333333", 255),
    14: ("#FAC80A", "#333333", 255), 15: ("#F4F4F4", "#333333", 255),
    19: ("#E4CD9E", "#333333", 255), 25: ("#D67923", "#333333", 255),
    28: ("#958A73", "#333333", 255), 70: ("#5F3109", "#333333", 255),
    71: ("#969696", "#333333", 255), 72: ("#646464", "#333333", 255),
    47: ("#FCFCFC", "#C3C3C3", 128), 36: ("#C91A09", "#880000", 128),
    16: ("#FFFF80", "#333333", 255), 24: ("#7F7F7F", "#333333", 255),
}

COMMON_LDRAW_DIRS = [
    "~/ldraw", "~/LDraw", "~/Documents/LDraw", "/usr/share/ldraw", "/usr/local/share/ldraw",
    "/opt/ldraw", "C:/LDraw", "C:/Users/Public/Documents/LDraw",
    "C:/Program Files/Studio 2.0/ldraw", "C:/Program Files (x86)/Studio 2.0/ldraw",
    "/Applications/Studio 2.0/ldraw", "~/Library/LDraw",
]


# ------------------------------------------------------------------- Math --
def mat_mul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def mat_vec(m, v):
    return (m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
            m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
            m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2])


def det3(m):
    return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))


def view_matrix(lat, lon):
    a, b = math.radians(lat), math.radians(lon)
    ry = ((math.cos(b), 0, math.sin(b)), (0, 1, 0), (-math.sin(b), 0, math.cos(b)))
    rx = ((1, 0, 0), (0, math.cos(a), -math.sin(a)), (0, math.sin(a), math.cos(a)))
    return mat_mul(rx, ry)


# ----------------------------------------------------------------- Colours --
def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(c):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(x)))) for x in c)


def shade(rgb, k):
    """k = 1: base colour; k < 1: darken towards black; k > 1: lighten towards white."""
    if k <= 1.0:
        return tuple(c * k for c in rgb)
    return tuple(c + (255 - c) * min(1.0, k - 1.0) for c in rgb)


def auto_edge(rgb):
    """Edge colour from the base colour: dark colours get lighter edges, others darker ones."""
    lum = (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255.0
    return shade(rgb, 1.45) if lum < 0.2 else shade(rgb, 0.4)


class Colors:
    """Colour key: int (LDraw code or direct colour) or ("edge", key)."""

    def __init__(self, ldconfig_path):
        self.opaque = False  # with --mono: ignore transparency
        self.table = {k: (hex_to_rgb(v[0]), hex_to_rgb(v[1]), v[2]) for k, v in FALLBACK_COLORS.items()}
        if ldconfig_path:
            self._load(ldconfig_path)

    def _load(self, path):
        pending = []
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                t = line.split()
                if len(t) < 3 or t[0] != "0" or t[1] != "!COLOUR":
                    continue
                kv = {}
                i = 3
                while i < len(t):
                    key = t[i].upper()
                    if key in ("CODE", "VALUE", "EDGE", "ALPHA", "LUMINANCE") and i + 1 < len(t):
                        kv[key] = t[i + 1]
                        i += 2
                    else:
                        i += 1
                try:
                    code = int(kv["CODE"])
                    value = hex_to_rgb(kv["VALUE"])
                except (KeyError, ValueError):
                    continue
                alpha = int(kv.get("ALPHA", 255))
                edge = kv.get("EDGE", "#333333")
                self.table[code] = (value, value, alpha)
                pending.append((code, edge))
        for code, edge in pending:
            if edge.startswith("#"):
                e = hex_to_rgb(edge)
            else:
                try:
                    e = self.table.get(int(edge), ((51, 51, 51),))[0]
                except ValueError:
                    e = (51, 51, 51)
            v, _, a = self.table[code]
            self.table[code] = (v, e, a)

    def _entry(self, key):
        if isinstance(key, tuple):  # ("edge", parent)
            return (self.edge(key[1]), (0, 0, 0), 255)
        if key in self.table:
            e = self.table[key]
            return (e[0], e[1], 255) if self.opaque else e
        if key >= 0x2000000:  # direct colour 0x2RRGGBB
            rgb = ((key >> 16) & 255, (key >> 8) & 255, key & 255)
            return (rgb, auto_edge(rgb), 255)
        return ((128, 128, 128), (51, 51, 51), 255)

    def main(self, key):
        return self._entry(key)[0]

    def edge(self, key):
        return self._entry(key)[1]

    def alpha(self, key):
        return self._entry(key)[2] / 255.0


def resolve_color(code, parent):
    if code == 16:
        return parent
    if code == 24:
        return ("edge", parent)
    return code


def parse_color(tok):
    tok = tok.strip()
    if tok.lower().startswith("0x"):
        return int(tok, 16)
    if tok.startswith("#"):
        return 0x2000000 | int(tok[1:], 16)
    return int(tok)


# ----------------------------------------------------------------- Library --
class Library:
    def __init__(self, root):
        self.root = root
        self._dircache = {}
        self.search = []
        if root:
            for sub in ("parts", "p", "models", "unofficial/parts", "unofficial/p"):
                d = self._ci_path(root, sub)
                if d and os.path.isdir(d):
                    self.search.append(d)

    def _listdir(self, d):
        if d not in self._dircache:
            try:
                self._dircache[d] = {n.lower(): n for n in os.listdir(d)}
            except OSError:
                self._dircache[d] = {}
        return self._dircache[d]

    def _ci_path(self, base, rel):
        """Case-insensitive path resolution (LDraw libraries come from Windows)."""
        cur = base
        for part in rel.replace("\\", "/").split("/"):
            if not part:
                continue
            real = self._listdir(cur).get(part.lower())
            if real is None:
                return None
            cur = os.path.join(cur, real)
        return cur

    def find(self, name, local_dirs):
        for d in list(local_dirs) + self.search:
            p = self._ci_path(d, name)
            if p and os.path.isfile(p):
                return p
        return None

    def ldconfig(self):
        return self._ci_path(self.root, "LDConfig.ldr") if self.root else None


def is_ldraw_root(d):
    """An LDraw root folder contains parts/ and p/ side by side."""
    try:
        names = {n.lower() for n in os.listdir(d)}
    except OSError:
        return False
    return "parts" in names and "p" in names


def ancestors(path, levels=6):
    d = os.path.dirname(os.path.abspath(path))
    for _ in range(levels):
        yield d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent


def find_ldraw_dir(cli, inputs=()):
    cands = []
    if cli:  # also accept a path pointing at parts/ or p/
        cli = os.path.abspath(os.path.expanduser(cli))
        cands += [cli, os.path.dirname(cli)]
    if os.environ.get("LDRAWDIR"):
        cands.append(os.environ["LDRAWDIR"])
    for inp in inputs:  # input file may live in e.g. <ldraw>/parts/ or <ldraw>/models/
        if os.path.isfile(inp):
            cands += list(ancestors(inp))
    cands += COMMON_LDRAW_DIRS
    for c in cands:
        p = os.path.expanduser(c)
        if is_ldraw_root(p):
            return p
    return None


# ----------------------------------------------------------------- Parser --
class LFile:
    __slots__ = ("cmds", "certified", "ccw")

    def __init__(self):
        self.cmds, self.certified, self.ccw = [], False, True


def parse_lines(lines):
    lf = LFile()
    geometry_seen = False
    for raw in lines:
        t = raw.split()
        if not t:
            continue
        typ = t[0]
        try:
            if typ == "0":
                if len(t) > 1 and t[1] == "BFC":
                    toks = [x.upper() for x in t[2:]]
                    if "CERTIFY" in toks and not geometry_seen:
                        lf.certified = True
                        if "CW" in toks:
                            lf.ccw = False
                        toks = [x for x in toks if x not in ("CERTIFY", "CW", "CCW")]
                    if "NOCERTIFY" in toks:
                        lf.certified = False
                    if toks:
                        lf.cmds.append(("bfc", toks))
                continue
            if typ == "1" and len(t) >= 15:
                c = parse_color(t[1])
                v = [float(x) for x in t[2:14]]
                m = ((v[3], v[4], v[5]), (v[6], v[7], v[8]), (v[9], v[10], v[11]))
                name = " ".join(t[14:])
                lf.cmds.append(("sub", c, m, (v[0], v[1], v[2]), name))
            elif typ == "2" and len(t) >= 8:
                v = [float(x) for x in t[2:8]]
                lf.cmds.append(("line", parse_color(t[1]), (tuple(v[0:3]), tuple(v[3:6]))))
            elif typ == "3" and len(t) >= 11:
                v = [float(x) for x in t[2:11]]
                lf.cmds.append(("face", parse_color(t[1]), (tuple(v[0:3]), tuple(v[3:6]), tuple(v[6:9]))))
            elif typ == "4" and len(t) >= 14:
                v = [float(x) for x in t[2:14]]
                lf.cmds.append(("face", parse_color(t[1]),
                                (tuple(v[0:3]), tuple(v[3:6]), tuple(v[6:9]), tuple(v[9:12]))))
            elif typ == "5" and len(t) >= 14:
                v = [float(x) for x in t[2:14]]
                lf.cmds.append(("cond", parse_color(t[1]),
                                (tuple(v[0:3]), tuple(v[3:6]), tuple(v[6:9]), tuple(v[9:12]))))
            else:
                continue
            geometry_seen = True
        except ValueError:
            continue
    return lf


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


def split_mpd(lines):
    """Returns (main name, {name_lower: lines}); empty dict for non-MPD files."""
    files, order, cur, skipping = {}, [], None, False
    for line in lines:
        t = line.split(None, 2)
        if len(t) >= 2 and t[0] == "0" and t[1].upper() == "FILE":
            cur = (t[2] if len(t) > 2 else "").strip().lower()
            files[cur], skipping = [], False
            order.append(cur)
            continue
        if len(t) >= 2 and t[0] == "0" and t[1].upper() in ("NOFILE", "!DATA"):
            skipping = True
            continue
        if cur is not None and not skipping:
            files[cur].append(line)
    return (order[0] if order else None), files


class Loader:
    def __init__(self, lib, colors, wireframe=None, mono=False):
        self.lib, self.colors, self.wireframe, self.mono = lib, colors, wireframe, mono
        self.cache = {}
        self.missing = set()

    def load_model(self, path):
        lines = read_text(path)
        main, mpd = split_mpd(lines)
        self.mpd = {k: parse_lines(v) for k, v in mpd.items()}
        self.local_dirs = [os.path.dirname(os.path.abspath(path))]
        return self.mpd[main] if main else parse_lines(lines)

    def get(self, name):
        key = name.replace("\\", "/").lower()
        if key in self.mpd:
            return self.mpd[key]
        if key in self.cache:
            return self.cache[key]
        p = self.lib.find(key, self.local_dirs)
        lf = parse_lines(read_text(p)) if p else None
        if lf is None and key not in self.missing:
            self.missing.add(key)
            print("  Warning: sub-file not found: %s" % name, file=sys.stderr)
        self.cache[key] = lf
        return lf

    def flatten(self, root, main_color):
        """Returns world-space geometry: faces, lines, conds."""
        faces, lines, conds = [], [], []
        ident = ((1, 0, 0), (0, 1, 0), (0, 0, 1))

        def walk(lf, m, off, color, invert, clip_ok, depth):
            if depth > 64:
                return
            ccw, clip, invnext = lf.ccw, True, False
            cert = lf.certified

            def xf(p):
                q = mat_vec(m, p)
                return (q[0] + off[0], q[1] + off[1], q[2] + off[2])

            for cmd in lf.cmds:
                kind = cmd[0]
                if kind == "bfc":
                    for tok in cmd[1]:
                        if tok == "CW":
                            ccw = False
                        elif tok == "CCW":
                            ccw = True
                        elif tok == "CLIP":
                            clip = True
                        elif tok == "NOCLIP":
                            clip = False
                        elif tok == "INVERTNEXT":
                            invnext = True
                elif kind == "sub":
                    _, c, lm, lt, name = cmd
                    child = self.get(name)
                    if child is not None:
                        cm = mat_mul(m, lm)
                        ct = xf(lt)
                        cinv = invert ^ invnext ^ (det3(lm) < 0)
                        walk(child, cm, ct, color if self.mono else resolve_color(c, color), cinv,
                             clip_ok and (clip or not cert), depth + 1)
                    invnext = False
                elif kind == "face":
                    faces.append(([xf(p) for p in cmd[2]], color if self.mono else resolve_color(cmd[1], color),
                                  cert and clip and clip_ok, ccw != invert))
                elif kind == "line":
                    lines.append(([xf(p) for p in cmd[2]], self._line_rgb(cmd[1], color)))
                elif kind == "cond":
                    conds.append(([xf(p) for p in cmd[2]], self._line_rgb(cmd[1], color)))

        walk(root, ident, (0.0, 0.0, 0.0), main_color, False, True, 0)
        return faces, lines, conds

    def _line_rgb(self, code, color):
        if code == 24 or self.mono:
            return self.colors.main(color) if self.wireframe else self.colors.edge(color)
        return self.colors.main(resolve_color(code, color))


# ------------------------------------------------------------ 2D geometry --
def shoelace(pts):
    s = 0.0
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i][0], pts[i][1]
        x1, y1 = pts[(i + 1) % n][0], pts[(i + 1) % n][1]
        s += x0 * y1 - x1 * y0
    return s


def clip_polygon(subject, clipper):
    """Sutherland-Hodgman; clipper must be convex with positive orientation."""
    out = subject
    n = len(clipper)
    for i in range(n):
        ex0, ey0 = clipper[i]
        ex1, ey1 = clipper[(i + 1) % n]
        dx, dy = ex1 - ex0, ey1 - ey0
        inp, out = out, []
        if not inp:
            break
        px, py = inp[-1]
        pv = dx * (py - ey0) - dy * (px - ex0)
        for cx, cy in inp:
            cv = dx * (cy - ey0) - dy * (cx - ex0)
            if cv >= 0:
                if pv < 0:
                    r = pv / (pv - cv)
                    out.append((px + (cx - px) * r, py + (cy - py) * r))
                out.append((cx, cy))
            elif pv >= 0:
                r = pv / (pv - cv)
                out.append((px + (cx - px) * r, py + (cy - py) * r))
            px, py, pv = cx, cy, cv
    return out


def centroid(poly):
    a = cx = cy = 0.0
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        cr = x0 * y1 - x1 * y0
        a += cr
        cx += (x0 + x1) * cr
        cy += (y0 + y1) * cr
    if abs(a) < 1e-12:
        return a / 2, sum(p[0] for p in poly) / n, sum(p[1] for p in poly) / n
    return a / 2, cx / (3 * a), cy / (3 * a)


def clip_segment(a, b, poly, inset):
    """Cyrus-Beck: parameter range [t0,t1] of segment a-b inside poly
    (inset > 0: polygon shrunk, inset < 0: polygon grown)."""
    t0, t1 = 0.0, 1.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = len(poly)
    for i in range(n):
        ex0, ey0 = poly[i]
        ex1, ey1 = poly[(i + 1) % n]
        ex, ey = ex1 - ex0, ey1 - ey0
        el = math.hypot(ex, ey)
        if el == 0:
            continue
        num = ex * (a[1] - ey0) - ey * (a[0] - ex0) - inset * el
        den = ex * dy - ey * dx
        if abs(den) < 1e-15:
            if num < 0:
                return None
            continue
        t = -num / den
        if den > 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 >= t1:
            return None
    return t0, t1


class Grid:
    def __init__(self, bboxes, bounds):
        self.minx, self.miny, w, h = bounds
        n = max(1, len(bboxes))
        self.g = max(1, min(256, int(math.sqrt(n) * 1.5)))
        self.cw = (w / self.g) or 1.0
        self.ch = (h / self.g) or 1.0
        self.cells = defaultdict(list)
        for i, bb in enumerate(bboxes):
            for c in self.cover(bb):
                self.cells[c].append(i)

    def cover(self, bb):
        g = self.g
        x0 = min(g - 1, max(0, int((bb[0] - self.minx) / self.cw)))
        x1 = min(g - 1, max(0, int((bb[2] - self.minx) / self.cw)))
        y0 = min(g - 1, max(0, int((bb[1] - self.miny) / self.ch)))
        y1 = min(g - 1, max(0, int((bb[3] - self.miny) / self.ch)))
        for cx in range(x0, x1 + 1):
            for cy in range(y0, y1 + 1):
                yield (cx, cy)

    def query(self, bb):
        s = set()
        for c in self.cover(bb):
            s.update(self.cells.get(c, ()))
        return s


def bbox_of(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def bb_overlap(a, b, e=0.0):
    return a[0] < b[2] - e and b[0] < a[2] - e and a[1] < b[3] - e and b[1] < a[3] - e


# --------------------------------------------------------------- Rendering --
class Face:
    __slots__ = ("p2", "p3", "n", "d", "z", "fill", "alpha", "bb")

    def depth_at(self, x, y):
        nx, ny, nz = self.n
        return (self.d - nx * x - ny * y) / nz


def render(faces_w, lines_w, conds_w, colors, size, wireframe=None, view=VIEWS[DEFAULT_VIEW]):
    """wireframe: None = faces + visible edges, "all" = all edges,
    "hidden" = visible edges only, faces act as (invisible) occluders."""
    lat, lon = view
    V = view_matrix(lat, lon)
    ll = math.sqrt(sum(c * c for c in LIGHT))
    light = tuple(c / ll for c in LIGHT)
    # Normalise brightness so the main face of the view shows exactly the base colour:
    # the top/bottom face when looking from above/below, otherwise the side facing the viewer.
    if lat >= 20:
        ref = (0.0, -1.0, 0.0)
    elif lat <= -20:
        ref = (0.0, 1.0, 0.0)
    else:
        b = math.radians(lon)
        ref = (math.sin(b), 0.0, -math.cos(b))
    top = mat_vec(V, ref)
    top_inten = AMBIENT + DIFFUSE * max(0.0, sum(a * b for a, b in zip(top, light)))

    # --- transform faces, cull back faces, apply lighting
    faces = []
    for pts, ckey, cullable, ccw in faces_w:
        vp = [mat_vec(V, p) for p in pts]
        area = shoelace(vp)
        if abs(area) < 1e-9:
            continue
        # in view space (x right, y down, z away): CCW front face <=> signed area < 0
        if cullable and ((area < 0) != ccw):
            continue
        nx = ny = nz = 0.0  # Newell normal
        k = len(vp)
        for i in range(k):
            x0, y0, z0 = vp[i]
            x1, y1, z1 = vp[(i + 1) % k]
            nx += (y0 - y1) * (z0 + z1)
            ny += (z0 - z1) * (x0 + x1)
            nz += (x0 - x1) * (y0 + y1)
        nl = math.sqrt(nx * nx + ny * ny + nz * nz)
        if nl == 0 or abs(nz) < 1e-12:
            continue
        nx, ny, nz = nx / nl, ny / nl, nz / nl
        if nz > 0:  # flip normal towards the viewer (for lighting)
            lnx, lny, lnz = -nx, -ny, -nz
        else:
            lnx, lny, lnz = nx, ny, nz
        inten = AMBIENT + DIFFUSE * max(0.0, lnx * light[0] + lny * light[1] + lnz * light[2])
        f = Face()
        p3 = list(vp)
        if area < 0:
            p3.reverse()
        f.p3 = p3
        f.p2 = [(p[0], p[1]) for p in p3]
        f.n = (nx, ny, nz)
        cx3 = sum(p[0] for p in vp) / k
        cy3 = sum(p[1] for p in vp) / k
        cz3 = sum(p[2] for p in vp) / k
        f.d = nx * cx3 + ny * cy3 + nz * cz3
        f.z = cz3
        f.fill = rgb_to_hex(shade(colors.main(ckey), inten / top_inten))
        f.alpha = colors.alpha(ckey)
        f.bb = bbox_of(f.p2)
        faces.append(f)

    # --- transform lines; evaluate conditional lines
    segs = []
    for pts, rgb in lines_w:
        segs.append((mat_vec(V, pts[0]), mat_vec(V, pts[1]), rgb))
    for pts, rgb in conds_w:
        a, b, c, d = (mat_vec(V, p) for p in pts)
        dx, dy = b[0] - a[0], b[1] - a[1]
        s1 = dx * (c[1] - a[1]) - dy * (c[0] - a[0])
        s2 = dx * (d[1] - a[1]) - dy * (d[0] - a[0])
        if s1 * s2 > 0:
            segs.append((a, b, rgb))

    allpts = [p for f in faces for p in f.p2] + [(s[i][0], s[i][1]) for s in segs for i in (0, 1)]
    if not allpts:
        raise RuntimeError("no visible geometry")
    minx, miny, maxx, maxy = bbox_of(allpts)
    w, h = max(maxx - minx, 1e-6), max(maxy - miny, 1e-6)
    extent = max(w, h)
    eps_z = 2e-3 * extent
    eps_a = 1e-6 * extent * extent
    bounds = (minx, miny, w, h)

    if wireframe == "all":  # all lines visible, no occluders
        faces = []
    # --- painting order: compare pairs that actually overlap
    #     (only needed when faces are drawn)
    n = 0 if wireframe else len(faces)
    grid = Grid([f.bb for f in faces], bounds)
    succ = [[] for _ in range(n)]
    indeg = [0] * n
    shrink = 1e-5 * extent
    for i in range(n):
        fi = faces[i]
        for j in grid.query(fi.bb):
            if j <= i:
                continue
            fj = faces[j]
            if not bb_overlap(fi.bb, fj.bb, shrink):
                continue
            inter = clip_polygon(fi.p2, fj.p2)
            if len(inter) < 3:
                continue
            area, px, py = centroid(inter)
            if area < eps_a:
                continue
            zi, zj = fi.depth_at(px, py), fj.depth_at(px, py)
            if zi > zj + eps_z:      # i is behind -> draw first
                succ[i].append(j)
                indeg[j] += 1
            elif zj > zi + eps_z:
                succ[j].append(i)
                indeg[i] += 1

    order, placed = [], [False] * n
    heap = [(-faces[i].z, i) for i in range(n) if indeg[i] == 0]
    heapq.heapify(heap)
    by_depth = sorted(range(n), key=lambda i: -faces[i].z)
    ptr = 0
    while len(order) < n:
        if not heap:  # cycle (e.g. intersecting faces): force the rearmost
            while placed[by_depth[ptr]]:
                ptr += 1
            heapq.heappush(heap, (0, by_depth[ptr]))
        _, i = heapq.heappop(heap)
        if placed[i]:
            continue
        placed[i] = True
        order.append(i)
        for j in succ[i]:
            indeg[j] -= 1
            if indeg[j] == 0 and not placed[j]:
                heapq.heappush(heap, (-faces[j].z, j))

    # --- remove hidden line segments
    #     Occluders are triangles (quads are split so slightly warped quads don't
    #     produce wrong depths). Each triangle is grown minimally so adjacent faces
    #     close without gaps - but only as far as its depth barely changes. Faces seen
    #     almost edge-on are therefore hardly grown and don't hide neighbouring edges.
    #     In hidden-line mode every face occludes, otherwise only opaque ones.
    base_grow = 1e-4 * extent
    occl = []
    for f in faces:
        if not (wireframe or f.alpha >= 0.999):
            continue
        p = f.p3
        for k in range(1, len(p) - 1):
            t0, t1, t2 = p[0], p[k], p[k + 1]
            ux, uy, uz = t1[0] - t0[0], t1[1] - t0[1], t1[2] - t0[2]
            vx, vy, vz = t2[0] - t0[0], t2[1] - t0[1], t2[2] - t0[2]
            nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
            nl = math.sqrt(nx * nx + ny * ny + nz * nz)
            if nl == 0 or abs(nz) < 1e-12 * nl:
                continue
            nx, ny, nz = nx / nl, ny / nl, nz / nl
            tri = [(t0[0], t0[1]), (t1[0], t1[1]), (t2[0], t2[1])]
            if shoelace(tri) < 0:
                tri.reverse()
            grad = math.hypot(nx, ny) / abs(nz)  # depth change per image unit
            grow = min(base_grow, 0.5 * eps_z / grad) if grad > 0 else base_grow
            bb = bbox_of(tri)
            bb = (bb[0] - grow, bb[1] - grow, bb[2] + grow, bb[3] + grow)
            occl.append((tri, (nx, ny, nz), nx * t0[0] + ny * t0[1] + nz * t0[2], bb, -grow))
    ogrid = Grid([o[3] for o in occl], bounds)
    min_len = 1e-3 * extent
    visible = []
    for a, b, rgb in segs:
        sbb = (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
        hidden = []
        for k in ogrid.query(sbb):
            tri, (nx, ny, nz), d, bb, inset = occl[k]
            if sbb[2] < bb[0] or bb[2] < sbb[0] or sbb[3] < bb[1] or bb[3] < sbb[1]:
                continue
            r = clip_segment(a, b, tri, inset)
            if r is None:
                continue
            t0, t1 = r
            vals = []
            for t in (t0, t1):
                x = a[0] + (b[0] - a[0]) * t
                y = a[1] + (b[1] - a[1]) * t
                zl = a[2] + (b[2] - a[2]) * t
                vals.append(zl - (d - nx * x - ny * y) / nz - eps_z)
            g0, g1 = vals
            if g0 > 0 and g1 > 0:
                hidden.append((t0, t1))
            elif g0 > 0 or g1 > 0:
                tc = t0 + (t1 - t0) * (g0 / (g0 - g1))
                hidden.append((t0, tc) if g0 > 0 else (tc, t1))
        hidden.sort()
        t = 0.0
        pieces = []
        for h0, h1 in hidden:
            if h0 > t:
                pieces.append((t, h0))
            t = max(t, h1)
        if t < 1.0:
            pieces.append((t, 1.0))
        seglen = math.hypot(b[0] - a[0], b[1] - a[1])
        for t0, t1 in pieces:
            if (t1 - t0) * seglen < min_len:
                continue
            visible.append(((a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0),
                            (a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1), rgb))

    # --- write SVG
    scale = (size - 2 * MARGIN) / extent
    W = w * scale + 2 * MARGIN
    H = h * scale + 2 * MARGIN

    def P(p):
        return "%s,%s" % (fmt((p[0] - minx) * scale + MARGIN), fmt((p[1] - miny) * scale + MARGIN))

    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%s" height="%s" viewBox="0 0 %s %s">'
           % (fmt(W), fmt(H), fmt(W), fmt(H))]
    if not wireframe:
        out.append('<g id="faces" stroke-linejoin="round" stroke-width="0.6">')
    run_key, run_d = None, []

    def flush():
        if run_d:
            fill, alpha = run_key
            if alpha >= 0.999:
                out.append('<path fill="%s" stroke="%s" d="%s"/>' % (fill, fill, "".join(run_d)))
            else:
                out.append('<path fill="%s" fill-opacity="%.2f" stroke="none" d="%s"/>'
                           % (fill, alpha, "".join(run_d)))

    for i in order:
        f = faces[i]
        key = (f.fill, f.alpha)
        if key != run_key or f.alpha < 0.999:
            flush()
            run_key, run_d = key, []
        run_d.append("M" + "L".join(P(p) for p in f.p2) + "Z")
    flush()
    if not wireframe:
        out.append("</g>")

    lw = max(1.0, size / 400.0)
    out.append('<g id="edges" fill="none" stroke-width="%s" stroke-linecap="round">' % fmt(lw))
    by_color = defaultdict(list)
    seen = set()
    for a, b, rgb in visible:
        pa, pb = P(a), P(b)
        key = (min(pa, pb), max(pa, pb))
        if key in seen:  # emit duplicate edges (e.g. from two primitives) only once
            continue
        seen.add(key)
        by_color[rgb_to_hex(rgb)].append("M%sL%s" % (pa, pb))
    for col, ds in by_color.items():
        out.append('<path stroke="%s" d="%s"/>' % (col, "".join(ds)))
    out.append("</g>\n</svg>\n")
    return "\n".join(out), len(order), len(visible)


def fmt(v):
    s = "%.2f" % v
    s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


# ------------------------------------------------------------------- Main --
def main():
    ap = argparse.ArgumentParser(description="Render LDraw parts/models as SVG.")
    ap.add_argument("inputs", nargs="+", help="file(s) or part names from the library, e.g. 3001")
    ap.add_argument("-o", "--output", help="output file (directory when there are several inputs)")
    ap.add_argument("-c", "--color", type=parse_color, default=DEFAULT_COLOR,
                    help="base colour for colour 16: LDraw code (e.g. 1) or hex (e.g. '#0055BF'). "
                         "Shading and edge colour are derived from it. Default: %d = Red" % DEFAULT_COLOR)
    ap.add_argument("-w", "--wireframe", action="store_true",
                    help="lines only (all edges, including hidden ones), no faces; lines in the base colour")
    ap.add_argument("-H", "--hidden-line", action="store_true",
                    help="like --wireframe, but hidden edges are removed")
    ap.add_argument("-m", "--mono", action="store_true",
                    help="draw everything in the base colour (-c), ignoring fixed part colours and transparency")
    ap.add_argument("--size", type=int, default=DEFAULT_SIZE, help="longest image side in px (default 512)")
    ap.add_argument("-v", "--view", choices=sorted(VIEWS), default=DEFAULT_VIEW,
                    help="view (default: iso)")
    ap.add_argument("--angles", metavar="LAT,LON",
                    help="custom view instead of --view, e.g. 20,-30 (elevation, rotation in degrees)")
    ap.add_argument("--ldraw", help="path to the LDraw library (otherwise $LDRAWDIR or auto-detection)")
    args = ap.parse_args()

    root = find_ldraw_dir(args.ldraw, args.inputs)
    if root:
        print("LDraw library: %s" % root, file=sys.stderr)
    else:
        print("Note: no LDraw library (folder with parts/ and p/) found - "
              "use --ldraw or set LDRAWDIR.", file=sys.stderr)
    lib = Library(root)
    colors = Colors(lib.ldconfig())
    colors.opaque = args.mono
    if args.angles:
        try:
            lat, lon = (float(x) for x in args.angles.split(","))
        except ValueError:
            ap.error("--angles expects LAT,LON, e.g. 20,-30")
        view = (lat, lon)
    else:
        view = VIEWS[args.view]
    mode = "hidden" if args.hidden_line else ("all" if args.wireframe else None)

    multi = len(args.inputs) > 1 or (args.output and os.path.isdir(args.output))
    rc = 0
    for inp in args.inputs:
        path = inp if os.path.isfile(inp) else None
        if path is None:
            name = inp if os.path.splitext(inp)[1] else inp + ".dat"
            path = lib.find(name, [])
        if path is None:
            print("Not found: %s" % inp, file=sys.stderr)
            rc = 1
            continue
        base = os.path.splitext(os.path.basename(path))[0] + ".svg"
        if multi:
            dst = os.path.join(args.output or ".", base)
        else:
            dst = args.output or base
        loader = Loader(lib, colors, mode, args.mono)
        root_file = loader.load_model(path)
        faces, lines, conds = loader.flatten(root_file, args.color)
        try:
            svg, nf, nl = render(faces, lines, conds, colors, args.size, mode, view)
        except RuntimeError as e:
            print("%s: %s" % (inp, e), file=sys.stderr)
            rc = 1
            continue
        with open(dst, "w", encoding="utf-8") as f:
            f.write(svg)
        print("%s -> %s  (%d faces, %d edge segments)" % (inp, dst, nf, nl))
    return rc


if __name__ == "__main__":
    sys.exit(main())
