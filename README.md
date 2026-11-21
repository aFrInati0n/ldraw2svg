# ldraw2svg

Render [LDraw](https://www.ldraw.org/) parts and models as clean vector graphics (SVG).

`ldraw2svg.py` is a single Python 3 script with no dependencies. It reads `.dat`, `.ldr` and `.mpd` files, resolves all sub-files from your LDraw library and writes an SVG with shaded faces and outline edges, as wireframe, or as hidden-line drawing.

## Requirements

- Python 3.8 or newer
- An LDraw parts library (the official library from ldraw.org, or the `ldraw` folder that ships with BrickLink Studio)

## Quick start

```bash
python3 ldraw2svg.py 3001                  # part from the library -> 3001.svg
python3 ldraw2svg.py path/to/model.ldr     # model file -> model.svg
```

## Usage

```
python3 ldraw2svg.py INPUT [INPUT ...] [options]
```

`INPUT` is either a file path or a part name from the library (`3001` or `3001.dat`). Several inputs can be given at once; each produces its own SVG.

| Option | Description |
|---|---|
| `-o`, `--output FILE` | Output file. With several inputs: output directory. Default: input name with `.svg` in the current directory |
| `-c`, `--color COLOR` | Base colour for LDraw colour 16 (the "main" colour). Either an LDraw colour code (`1`, `71`, …) or hex (`"#0055BF"`). Default: `4` (Red) |
| `-m`, `--mono` | Draw everything in the base colour, ignoring fixed part colours and transparency |
| `-w`, `--wireframe` | Lines only, including hidden edges; no faces |
| `-H`, `--hidden-line` | Lines only, hidden edges removed |
| `-v`, `--view NAME` | Predefined view (see below). Default: `iso` |
| `--angles LAT,LON` | Custom view angles in degrees, overrides `--view` |
| `--size PX` | Length of the longest image side in pixels. Default: `512` |
| `--ldraw DIR` | Path to the LDraw library |

### Examples

```bash
# Blue 2x4 brick, 800 px
python3 ldraw2svg.py 3001 -c 1 --size 800

# Custom colour; shading and edge colour are derived automatically
python3 ldraw2svg.py 3001 -c "#F2CD37"

# Front view of a part
python3 ldraw2svg.py 89801 --view front

# Whole model in a single colour, as hidden-line drawing
python3 ldraw2svg.py model.mpd -m -H -c "#333333"

# Several parts into one folder
python3 ldraw2svg.py 3001 3003 3020 -o svg/
```

## Views

All views use an orthographic projection.

| `--view` | Description |
|---|---|
| `iso` | 30° from above, front and right side visible (default) |
| `iso-left` | 30° from above, front and left side visible |
| `front`, `back`, `left`, `right` | Straight side views |
| `top` | Top view, front of the part at the bottom |
| `bottom` | View from below |

`--angles LAT,LON` sets a custom view: `LAT` is the elevation (positive = from above, `90` = top view), `LON` the rotation about the vertical axis (positive = right side turns towards the viewer). `--angles 30,45` is the same as `iso`.

## Colours

- Colours are read from `LDConfig.ldr` in the library root. Without it, a small built-in table of common colours is used.
- The base colour (`-c`) is shown exactly on the main face of the view: the top face in `iso`/`top`, the front face in `front`, etc. Other faces are shaded darker (or lighter) from it.
- Edge colours come from `LDConfig.ldr`. For hex colours they are derived automatically: darker for light colours, lighter for very dark ones.
- `-c` only replaces colour 16. Parts in a model that have a fixed colour keep it unless `-m` is used.
- In wireframe modes, lines are drawn in the base colour instead of the edge colour.

## Finding the LDraw library

The library root is the folder that contains `parts/` and `p/` side by side. It is looked up in this order:

1. `--ldraw DIR` (also accepted if it points at `parts/` or `p/` directly)
2. The `LDRAWDIR` environment variable
3. The folders above the input file, so `ldraw2svg.py C:/LDraw/parts/3001.dat` works without further options
4. Common install locations, e.g. `~/ldraw`, `C:/LDraw`, `/usr/share/ldraw`, and the `ldraw` folder of BrickLink Studio

The library in use is printed at start-up. Sub-files are searched in the input file's folder, then in `parts/`, `p/`, `models/` and `unofficial/`. File names are matched case-insensitively.

## Supported input

| Format | Supported |
|---|---|
| `.dat` (parts) | yes |
| `.ldr` (models) | yes |
| `.mpd` (multi-part models) | yes, incl. embedded sub-models |
| `.io` (BrickLink Studio) | no; export to LDraw (`.ldr`/`.mpd`) from Studio first |
| `.lxf` (LEGO Digital Designer) | no; convert to LDraw first |

Supported LDraw features: line types 1–5, colour inheritance (16/24), direct colours (`0x2RRGGBB`), transparency, BFC (`CERTIFY`, `CW`/`CCW`, `INVERTNEXT`, `CLIP`/`NOCLIP`, mirrored matrices). Textures (`!TEXMAP`) and `!DATA` blocks are ignored.

## Output

The SVG contains two groups, so it can easily be embedded or post-processed:

```xml
<svg ...>
  <g id="faces">  ... filled polygons, painted back to front ... </g>
  <g id="edges">  ... visible edge and outline segments, grouped by colour ... </g>
</svg>
```

In wireframe and hidden-line mode only the `edges` group is written.

## How it works

1. **Load:** All sub-files are resolved recursively and flattened into triangles, quads, edges and conditional lines in world coordinates.
2. **Cull:** Back faces of BFC-certified geometry are dropped.
3. **Order:** Faces whose projections overlap are compared at their common area. The resulting order is sorted topologically (a painter's algorithm that doesn't rely on centroid depth alone).
4. **Hide lines:** Each edge is clipped against all occluding faces; only the visible pieces are kept and drawn on top of the faces. Conditional lines (type 5) are drawn only where they form a silhouette.

## Limitations

- **Tolerances:** Whether an edge lies *on* a face or *just behind* it can only be decided within a tolerance, because parts are made of approximated faces and edges often come from different sub-files. In rare cases, especially in straight front/side views of curved parts, a short edge piece may show through or be missing.
- **Intersecting faces** are not split. Where faces penetrate each other, the painting order is decided by depth and may be slightly off.
- **Transparency:** Edges behind transparent faces are drawn at full strength.
- **Performance:** The script is pure Python. Single parts and small models take well under a second (a 16×16 plate with ~6,000 faces takes about 0.5 s); large models with hundreds of parts can take considerably longer and produce SVG files of several MB.

## Customising

View presets, light direction, default colour, image size and margin are constants at the top of the script (`VIEWS`, `LIGHT`, `AMBIENT`, `DIFFUSE`, `DEFAULT_COLOR`, `DEFAULT_SIZE`, `MARGIN`).
