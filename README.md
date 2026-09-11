# Sheet Metal Enclosure Generator

Generates a family of bent sheet metal enclosures from a CSV table. For each
variant it writes a 3D model (STEP), a flat pattern for laser cutting (DXF),
and a manifest listing every file and its status.

![Isometric view](docs/showcase_isometric.png)

![Flat pattern](docs/showcase_flat_pattern.png)

## Output

For every row in the enclosure table:

- `step/<variant>.step`: the folded part, in mm.
- `dxf/<variant>.dxf`: the flat pattern, in mm. Cut contours are on layer
  `CUT`, bend lines on layer `BEND`. Each bend line is the centerline of
  its bend zone, not a tangent line. Geometry is lines, arcs and circles
  only, no splines.
- `manifest.csv` and `manifest.xlsx`: one row per variant with its
  parameters, file names and status. `features_manifest.csv` records the
  status of each feature.

## Input

**Enclosure table** (`showcase_input.csv`)

| column | meaning |
|---|---|
| `variant_id` | name used for the output files |
| `inner_length_mm`, `inner_width_mm`, `inner_height_mm` | inside dimensions of the box |
| `thickness_mm` | sheet thickness |
| `bend_radius_mm` | inside bend radius |
| `k_factor` | neutral axis position used for the bend allowance |

Dimensions are inside dimensions, so the box is sized directly from what has
to fit in it.

**Feature table** (`showcase_features.csv`): holes, slots and rectangular
cutouts on any face.

| column | meaning |
|---|---|
| `variant_id` | enclosure the feature belongs to |
| `face` | `base`, `front`, `back`, `left`, `right` |
| `type` | `hole`, `slot`, `rect` |
| `u_mm`, `v_mm` | center position (see below) |
| `size1_mm`, `size2_mm` | hole: diameter. slot: overall length, width. rect: width, height |
| `rotation_deg` | rotation of a slot or rect, in degrees, counterclockwise in the face's `u`-`v` frame; 0 = long axis horizontal |
| `count_u`, `count_v`, `pitch_u_mm`, `pitch_v_mm` | optional grid; `u`, `v` is the center of the grid |

Positions are measured on the inside surface:

- Base: from the inside front-left corner, `u` along the length, `v` along
  the width.
- Walls: viewed from outside, `u` from the wall's left inside edge, `v` up
  from the inside floor.

## Bend allowance

All bends are 90°. The flat length of each bend is

    BA = (π/2) · (R + K · T)

K is an input because it depends on the material and the press brake. The
value in the examples (0.38) is a placeholder: replace it with your
fabricator's value and regenerate.

## Validation

Before anything is built, every boxes.csv row is checked, and -- if that
passes -- every feature and every instance of a grid is checked.

**Box dimensions.** Each check below is either a geometric necessity or a
measured failure point in this SheetMetal build:

| check | why |
|---|---|
| all six numeric columns present and parse as numbers | a blank or mistyped cell would otherwise reach FreeCAD as a raw exception |
| `thickness_mm > 0` | `thickness_mm <= 0` raises a raw FreeCAD error while building the base plate (measured at `0` and `-1.5`) |
| `bend_radius_mm >= 0.1mm` | the flat-pattern step raises a raw error at `bend_radius_mm = 0.001mm` (measured); the floor is ~100x that failure point and still far below any real bend radius |
| `inner_length_mm > 2 * bend_radius_mm`, and the same for `inner_width_mm` | the base plate is a rectangle of these net dimensions; exactly `0` raises a raw error, and a negative value folds the box inside-out without raising anything -- which then crashes the interior-measurement step instead |
| `inner_height_mm > bend_radius_mm` | the wall's own extension length beyond the bend; `<= 0` raises the same class of raw error as `thickness_mm` |

A row that fails any of these gets `status="failed: <reason>"` in the
manifest, exactly like a feature-fit failure -- the rest of the table
still runs. There is no minimum enclosure size beyond that: a wall's
own face is picked by *position* (the outermost face on that side --
the same normal-plus-position pattern used to pick the base face for
the flat pattern), not by how large the face is, so a small but
otherwise buildable enclosure is never rejected for being small.

**Features.** Every feature, and every instance of a grid, is checked to
lie on the flat part of its face, clear of the bends. Rotated features
are checked with their rotated extent. If any feature of a variant
fails, that variant is not generated at all and the manifest gives the
reason. A part with a missing hole is worse than no part.

## Failure handling

A variant can fail for two different reasons, and both are handled the
same way: the manifest gets a `failed: <reason>` row for that variant,
and the run continues with the rest of the table.

- **Feature fit** (see Validation above): checked before any geometry is
  touched.
- **Geometry construction**: the folded shape turns out invalid, the base
  face can't be identified unambiguously, a wall face can't be found, or
  a feature's cut-through margin would reach the opposite wall. These are
  problems with that one variant's own numbers (a bend radius or
  thickness that doesn't suit its enclosure), not with the script, so
  they're recorded and skipped exactly like a feature-fit failure --
  never allowed to stop the batch.

Guarantees that follow from this:

- A variant's own previous STEP/DXF (from an earlier run) are only
  touched once that variant is *conclusively* going to succeed or fail --
  never speculatively at the start of its build. A failed rebuild never
  deletes a working file without a replacement ready; a successful
  rebuild only overwrites the old files once the new STEP and DXF are
  both written.
- `manifest.csv`, `manifest.xlsx` and `features_manifest.csv` are written
  for every variant that was actually attempted, even if a later variant
  in the same run hits an error the script has no name for and has to
  stop. The files on disk are never left describing a run that didn't
  happen, or silently missing a variant that did run.
- An error the script has no name for (a bug, or a FreeCAD/SheetMetal
  failure outside the cases listed above) still stops the whole run --
  but only after the manifest for everything processed so far has been
  written.

## Verification

- Inside dimensions measured on the generated 3D models match the input
  exactly (3 variants with different thickness, bend radius and K).
- Flat pattern sizes match the bend allowance formula to 4 decimal places.
- Feature positions in the DXF match their expected positions to 4 decimal
  places.
- The 3D volume matches a hand calculation.
- STEP files re-import with no change in volume or bounding box.

These are geometric checks. No part has been physically cut and bent.

## Requirements

- FreeCAD 1.1 (tested with 1.1.3) with the SheetMetal workbench, tested at
  commit `db3f875`.
- Runs headless with `freecadcmd`. No GUI needed.

## Usage

    freecadcmd generate_box_family.py --pass <boxes.csv> <features.csv> <out_dir>

Arguments must come after `--pass`, otherwise FreeCAD tries to open them as
documents instead of passing them to the script.

Example, run from inside this directory, using the showcase input:

    freecadcmd generate_box_family.py --pass showcase_input.csv showcase_features.csv output

## Known limitations

- Four walls, 90° bends, one thickness and one bend radius per variant. No
  hems, lids or extra flanges.
- The layered DXF is written through internal functions of the SheetMetal
  workbench, because its own layered export needs the GUI. A SheetMetal
  update may break this; the tested commit is listed above.
- Corner relief is the one SheetMetal generates. It is not a parameter.
- The flat pattern is drawn as seen from the outside surface, with flanges
  bending away from the viewer. This follows from the geometry and has not
  been confirmed on a physical part.
- Output files of a variant removed from the input table are not deleted.
- The wall-face rule (find_wall_outer: the single most extreme matching
  face on a side) assumes the wall's own outer skin really is the most
  extreme axis-aligned face on its side. Measured to hold at any ordinary
  or even generously oversized thickness (checked up to `thickness_mm`
  30-40mm against `bend_radius_mm` as small as 2mm, all exact matches to
  the input); it was this same rule, replacing an area cutoff, that fixed
  an earlier version of this limitation where `thickness_mm` comparable
  to `bend_radius_mm` silently shifted the measured interior dimensions.
  It still breaks at genuinely absurd proportions -- `thickness_mm` at
  or beyond the wall's own `leg` (inner_height_mm - bend_radius_mm), e.g.
  200mm+ thick "sheet" metal -- where the corner geometry stops producing
  a well-formed wall at all and some other face becomes the extreme
  match. No real sheet-metal part is anywhere near that thickness.
- Similarly, an `inner_length_mm` (or `inner_width_mm`) small enough that
  the resulting `L` (or `W`) drops below `thickness_mm` -- opposite walls
  closer together than the material itself is thick, which validate_box()
  only excludes at `L <= 0` / `W <= 0`, not at this narrower boundary --
  can also confuse the wall-face rule the same way. Both of these are
  proportions no real enclosure would ever call for; validate_box()
  deliberately does not add a rule for either, since neither reduced to
  a clean, confidently-measured threshold the way the checks it does
  enforce did (see Validation above).
