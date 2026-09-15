"""Generates a family of sheet-metal boxes from CSV, with optional
holes/slots/rectangular cutouts (features).

Run with freecadcmd:
  freecadcmd generate_box_family.py --pass <boxes.csv> <features.csv> <out_dir>

boxes.csv columns:
  variant_id, inner_length_mm, inner_width_mm, inner_height_mm,
  thickness_mm, bend_radius_mm, k_factor

features.csv columns:
  variant_id, face, type, u_mm, v_mm, size1_mm, size2_mm, rotation_deg,
  count_u, count_v, pitch_u_mm, pitch_v_mm
  face: base | front (y=0) | back | left (x=0) | right
  type: hole (size1=diameter) | slot (size1=TOTAL length, size2=width,
        rotation 0 = long axis horizontal) | rect (size1=horizontal,
        size2=vertical)
  Position (center), referenced from the INNER surface:
    base: from the front-left inner corner, u along length (x),
          v along width (y).
    wall: viewed from outside; u from the wall's left inner edge
          (horizontal), v from the base's top surface (z=T) upward.

  count_u/count_v/pitch_u_mm/pitch_v_mm: optional grid repeat, blank
  defaults to 1, 1, 0, 0 (a single instance, existing rows keep working
  unchanged). u,v is the CENTER of the whole grid; instances are laid
  out symmetrically around it:
    u_i = u + (i - (count_u-1)/2) * pitch_u   for i in 0..count_u-1
    v_j = v + (j - (count_v-1)/2) * pitch_v   for j in 0..count_v-1

Conversion (SheetMetal input):
  L = inner_length - 2R, W = inner_width - 2R, leg = inner_height - R

Every boxes.csv row is validated (validate_box(), below) before any
geometry is built: all six numeric columns present and parseable,
thickness_mm > 0, bend_radius_mm >= 0.1mm, L > 0, and leg > 0 (W > 0 is
the same check as L > 0, mirrored onto the width). Each check is either
a geometric necessity (a rectangle needs a positive side) or a measured
failure point in this SheetMetal build (see validate_box()'s docstring
for the numbers) -- a row that fails is skipped exactly like a
feature-fit failure below, never a raw traceback. There is no minimum
enclosure size beyond that: measure_interior()/find_wall_mid() (below)
pick a wall's own face by position (find_wall_outer()), not by how
large it is, so a small but otherwise valid box is never rejected for
being small.

Validation (before production) runs on EVERY grid instance separately.
w,h = the feature's total horizontal/vertical footprint after rotation:
  base: R <= u_i-w/2  and  u_i+w/2 <= inner_length - R
        R <= v_j-h/2  and  v_j+h/2 <= inner_width - R
  wall: R <= u_i-w/2  and  u_i+w/2 <= wall's inner span - R
        R <= v_j-h/2  and  v_j+h/2 <= inner_height   (no -R at the top --
        the open rim is not a bend zone)

If ANY grid instance of ANY feature row for a variant fails validation, or
if any geometry-construction step for that variant fails (invalid folded
shape, ambiguous base face, missing wall face, cut-through margin too
large), that variant is NOT produced: no STEP/DXF are written for it. Any
STEP/DXF left over from a previous run for that variant ARE deleted, but
only once the variant is conclusively not going to be produced -- never
pre-emptively before or during construction, so a run that dies partway
through never destroys a previously good file without a replacement.
manifest.csv records status="failed: <reason>" for that variant; the rest
of the batch is unaffected. features_manifest.csv still records the
outcome of every feature row independently, regardless of whether the
variant as a whole was built. manifest.csv/manifest.xlsx/
features_manifest.csv are (re)written after every variant that was
attempted, even if a later variant hits an error the script has no name
for and has to stop the run. See README ("Failure handling") for the full
contract.

Per successful variant: STEP + layered DXF (CUT/BEND) + manifest.py
(bundled in this repo, no cadkit dependency), written only once every
step of that variant's own pipeline has succeeded. Base-face selection:
deterministic rule (the single planar face whose center is at z=0 and
whose outward normal is exactly -Z; anything other than one match fails
that variant, the same as a feature-fit failure).

freecadcmd note: extra CLI arguments must come after --pass, otherwise
FreeCAD tries to open them as documents. The SheetMetal addon folder
is added to sys.path automatically by freecadcmd, no need to append it.
The script runs inside FreeCAD's own startup context, so __name__ is
NOT "__main__" -- main() is therefore called unconditionally.
"""
import csv
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

import FreeCAD as App
import Part
import importDXF

import SheetMetalBaseCmd
import SheetMetalCmd
import SheetMetalNewUnfolder
from SheetMetalNewUnfolder import BendAllowanceCalculator, SketchExtraction

from manifest import build_manifest_fields, write_manifest_csv, write_manifest_xlsx

NUMERIC_FIELDS = [
    "inner_length_mm", "inner_width_mm", "inner_height_mm",
    "thickness_mm", "bend_radius_mm", "k_factor",
]
FORMATS = ["step", "dxf"]

FEATURE_FIELDS = [
    "variant_id", "face", "type", "u_mm", "v_mm", "size1_mm", "size2_mm",
    "rotation_deg", "count_u", "count_v", "pitch_u_mm", "pitch_v_mm",
    "status", "reason",
]


def select_base_face(shape):
    """Deterministic rule: the single planar face whose center lies at
    z=0 and whose outward (orientation-corrected) normal is exactly
    (0,0,-1) -- the base plate's outer skin. Exactly one match required."""
    matches = []
    for i, f in enumerate(shape.Faces):
        if not isinstance(f.Surface, Part.Plane):
            continue
        c = f.CenterOfMass
        if abs(c.z - 0.0) > 1e-6:
            continue
        ur = f.ParameterRange
        u_mid = (ur[0] + ur[1]) / 2
        v_mid = (ur[2] + ur[3]) / 2
        n = f.normalAt(u_mid, v_mid)
        if abs(n.x) < 1e-6 and abs(n.y) < 1e-6 and abs(n.z - (-1.0)) < 1e-6:
            matches.append(i + 1)
    if len(matches) != 1:
        raise SystemExit(f"base-face rule found {len(matches)} matches (expected 1).")
    return f"Face{matches[0]}"


def find_wall_outer(shape, axis, positive_side, threshold):
    """The wall's own outer-skin planar face on the given side of
    `threshold` (positive_side selects the face with the LARGEST
    coordinate along `axis`; the other side selects the SMALLEST) --
    deterministic by construction, the same normal+position pattern
    select_base_face (above) uses, with no area or size heuristic:

    Every corner-relief notch and every wall's own T x leg end cap sits
    strictly BETWEEN the wall's outer skin and the box interior (measured
    directly: dumping every axis-aligned planar face of boxes from
    100x80x40mm down to 40x30x20mm, the outer skin is always the single
    most extreme match, regardless of box size -- unlike an area cutoff,
    "furthest from the interior" never depends on how big the wall is).
    So the single most extreme candidate on a side is always the outer
    skin, full stop; a tie (more than one face at that extreme) is
    exactly as ambiguous as select_base_face finding more than one base
    face, and is rejected the same way."""
    candidates = []
    for f in shape.Faces:
        if not isinstance(f.Surface, Part.Plane):
            continue
        n = f.Surface.Axis
        c = f.CenterOfMass
        if axis == "x" and abs(n.x) > 0.99 and abs(n.y) < 0.01:
            val = c.x
        elif axis == "y" and abs(n.y) > 0.99 and abs(n.x) < 0.01:
            val = c.y
        else:
            continue
        if (val > threshold) == positive_side:
            candidates.append(val)
    if not candidates:
        raise SystemExit(f"no wall face found (axis={axis}, positive_side={positive_side}).")
    extreme = max(candidates) if positive_side else min(candidates)
    matches = [v for v in candidates if abs(v - extreme) < 1e-6]
    if len(matches) != 1:
        raise SystemExit(
            f"wall outer-skin rule found {len(matches)} matches (expected 1) "
            f"(axis={axis}, positive_side={positive_side})."
        )
    return extreme


def measure_interior(shape, L, W, T):
    """Interior clear dimensions: each wall's outer skin (find_wall_outer,
    above) offset inward by the sheet thickness T gives that wall's INNER
    skin exactly -- the inner and outer skin of a bent sheet are always
    exactly T apart along the wall's own normal, so the inner skin never
    needs its own (separately ambiguous) face search. Interior height is
    wall top (shape ZMax) minus base top (z=T)."""
    inner_x_lo = find_wall_outer(shape, "x", False, L / 2) + T
    inner_x_hi = find_wall_outer(shape, "x", True, L / 2) - T
    inner_y_lo = find_wall_outer(shape, "y", False, W / 2) + T
    inner_y_hi = find_wall_outer(shape, "y", True, W / 2) - T

    inner_length = inner_x_hi - inner_x_lo
    inner_width = inner_y_hi - inner_y_lo
    inner_height = shape.BoundBox.ZMax - T

    return inner_length, inner_width, inner_height


def find_wall_mid(shape, axis, positive_side, threshold, T):
    """Wall mid-thickness coordinate (along `axis`, 'x' or 'y') on the
    given side: the outer skin (find_wall_outer, above) offset inward by
    half the sheet thickness."""
    outer = find_wall_outer(shape, axis, positive_side, threshold)
    return outer - T / 2.0 if positive_side else outer + T / 2.0


def face_geometry(face, u, v, L, W, R, T, box_shape):
    """Resolves a feature's face/(u,v) spec into:
      center -- world Vector of the feature center (the axis normal to
                the face is already resolved: T/2 for base, the detected
                wall mid-thickness coordinate for walls)
      p_hat  -- unit Vector: world direction of +u (local horizontal)
      q_hat  -- unit Vector: world direction of +v (local vertical)
      n_hat  -- unit Vector: cut-through (thickness) direction
    """
    if face == "base":
        cx, cy = u - R, v - R
        center = App.Vector(cx, cy, T / 2.0)
        return center, App.Vector(1, 0, 0), App.Vector(0, 1, 0), App.Vector(0, 0, 1)
    if face == "front":
        cx, cz = u - R, T + v
        y_mid = find_wall_mid(box_shape, "y", False, W / 2.0, T)
        center = App.Vector(cx, y_mid, cz)
        return center, App.Vector(1, 0, 0), App.Vector(0, 0, 1), App.Vector(0, 1, 0)
    if face == "back":
        cx, cz = (L + R) - u, T + v
        y_mid = find_wall_mid(box_shape, "y", True, W / 2.0, T)
        center = App.Vector(cx, y_mid, cz)
        return center, App.Vector(-1, 0, 0), App.Vector(0, 0, 1), App.Vector(0, 1, 0)
    if face == "left":
        cy, cz = (W + R) - u, T + v
        x_mid = find_wall_mid(box_shape, "x", False, L / 2.0, T)
        center = App.Vector(x_mid, cy, cz)
        return center, App.Vector(0, -1, 0), App.Vector(0, 0, 1), App.Vector(1, 0, 0)
    if face == "right":
        cy, cz = u - R, T + v
        x_mid = find_wall_mid(box_shape, "x", True, L / 2.0, T)
        center = App.Vector(x_mid, cy, cz)
        return center, App.Vector(0, 1, 0), App.Vector(0, 0, 1), App.Vector(1, 0, 0)
    raise ValueError(f"unknown face: {face}")


def feature_footprint(ftype, size1, size2, rotation_deg):
    """Bounding w,h (rotation-aware) of the feature's footprint, centered
    on its own local origin -- used only for validation."""
    theta = math.radians(rotation_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)

    def rot_bbox(points):
        rw = [abs(px * cos_t - py * sin_t) for px, py in points]
        rh = [abs(px * sin_t + py * cos_t) for px, py in points]
        return 2 * max(rw), 2 * max(rh)

    if ftype == "hole":
        return size1, size1
    if ftype == "rect":
        hw, hh = size1 / 2.0, size2 / 2.0
        return rot_bbox([(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)])
    if ftype == "slot":
        total_len, width = size1, size2
        half_center_dist = (total_len - width) / 2.0
        r = width / 2.0
        w, h = rot_bbox([(-half_center_dist, 0.0), (half_center_dist, 0.0)])
        return w + 2 * r, h + 2 * r
    raise ValueError(f"unknown feature type: {ftype}")


def build_feature_cutter(ftype, size1, size2, rotation_deg, center, p_hat, q_hat, n_hat, depth):
    theta = math.radians(rotation_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)

    def world_pt(p, q):
        pr = p * cos_t - q * sin_t
        qr = p * sin_t + q * cos_t
        return center + p_hat * pr + q_hat * qr - n_hat * (depth / 2.0)

    if ftype == "hole":
        r = size1 / 2.0
        return Part.makeCylinder(r, depth, center - n_hat * (depth / 2.0), n_hat)

    if ftype == "rect":
        w, h = size1, size2
        pts = [world_pt(-w / 2, -h / 2), world_pt(w / 2, -h / 2),
               world_pt(w / 2, h / 2), world_pt(-w / 2, h / 2)]
        wire = Part.makePolygon(pts + [pts[0]])
        face_ = Part.Face(wire)
        return face_.extrude(n_hat * depth)

    if ftype == "slot":
        total_len, width = size1, size2
        r = width / 2.0
        half_center_dist = (total_len - width) / 2.0
        c1 = world_pt(-half_center_dist, 0.0)
        c2 = world_pt(half_center_dist, 0.0)
        cyl1 = Part.makeCylinder(r, depth, c1, n_hat)
        cyl2 = Part.makeCylinder(r, depth, c2, n_hat)
        pts = [world_pt(-half_center_dist, -r), world_pt(half_center_dist, -r),
               world_pt(half_center_dist, r), world_pt(-half_center_dist, r)]
        wire = Part.makePolygon(pts + [pts[0]])
        mid_face = Part.Face(wire)
        mid_box = mid_face.extrude(n_hat * depth)
        return cyl1.fuse(cyl2).fuse(mid_box)

    raise ValueError(f"unknown feature type: {ftype}")


def grid_samples(u, v, count_u, count_v, pitch_u, pitch_v):
    """Symmetric grid of (i, j, u_i, v_j) instances centered on (u, v)."""
    samples = []
    for i in range(count_u):
        u_i = u + (i - (count_u - 1) / 2.0) * pitch_u
        for j in range(count_v):
            v_j = v + (j - (count_v - 1) / 2.0) * pitch_v
            samples.append((i, j, u_i, v_j))
    return samples


def validate_features(feature_rows, L, W, R, inner_length, inner_width, inner_height):
    """Validates every instance of every feature row independently (no
    geometry touched yet). Returns a list of parsed dicts, each carrying
    its own status/reason plus (if valid) the grid instances needed to
    build its cutters."""
    parsed_rows = []
    for row in feature_rows:
        face = row["face"].strip()
        ftype = row["type"].strip()
        u = float(row["u_mm"])
        v = float(row["v_mm"])
        size1 = float(row["size1_mm"])
        size2 = float(row["size2_mm"]) if row.get("size2_mm") not in (None, "") else 0.0
        rotation = float(row["rotation_deg"]) if row.get("rotation_deg") not in (None, "") else 0.0
        count_u = int(row["count_u"]) if row.get("count_u") not in (None, "") else 1
        count_v = int(row["count_v"]) if row.get("count_v") not in (None, "") else 1
        pitch_u = float(row["pitch_u_mm"]) if row.get("pitch_u_mm") not in (None, "") else 0.0
        pitch_v = float(row["pitch_v_mm"]) if row.get("pitch_v_mm") not in (None, "") else 0.0

        parsed = {
            "variant_id": row["variant_id"], "face": face, "type": ftype,
            "u_mm": u, "v_mm": v, "size1_mm": size1, "size2_mm": size2,
            "rotation_deg": rotation,
            "count_u": count_u, "count_v": count_v,
            "pitch_u_mm": pitch_u, "pitch_v_mm": pitch_v,
            "status": "ok", "reason": "", "instances": [],
        }

        try:
            if face not in ("base", "front", "back", "left", "right"):
                raise ValueError(f"unknown face: {face}")

            w, h = feature_footprint(ftype, size1, size2, rotation)

            if face == "base":
                horiz_max, vert_max = inner_length, inner_width
                vert_top_margin = R
            else:
                horiz_max = inner_length if face in ("front", "back") else inner_width
                vert_max = inner_height
                vert_top_margin = 0.0

            instances = grid_samples(u, v, count_u, count_v, pitch_u, pitch_v)
            failures = []
            for i, j, u_i, v_j in instances:
                ok_u = (R <= u_i - w / 2) and (u_i + w / 2 <= horiz_max - R)
                ok_v = (R <= v_j - h / 2) and (v_j + h / 2 <= vert_max - vert_top_margin)
                if not ok_u:
                    failures.append(
                        f"instance(i={i},j={j}) u out of range: u={u_i} w={w:.4f} "
                        f"allowed=[{R},{horiz_max - R}]"
                    )
                elif not ok_v:
                    failures.append(
                        f"instance(i={i},j={j}) v out of range: v={v_j} h={h:.4f} "
                        f"allowed=[{R},{vert_max - vert_top_margin}]"
                    )

            if failures:
                raise ValueError("; ".join(failures))

            parsed["instances"] = [(u_i, v_j) for _, _, u_i, v_j in instances]

        except Exception as e:
            parsed["status"] = "failed"
            parsed["reason"] = str(e)

        parsed_rows.append(parsed)
    return parsed_rows


CUT_THROUGH_MARGIN_MM = 1.0  # clearance the cutter must exceed on each face of the sheet


def cut_features(box_shape, parsed_rows, L, W, R, T):
    """Cuts every instance of every (already validated) feature into the
    shape, in order. Assumes all rows in parsed_rows have status == 'ok'.

    depth = T + 2*CUT_THROUGH_MARGIN_MM, derived from the sheet thickness
    T -- the previous fixed 4.0mm (base) / 2.0mm (wall) left holes as
    blind pockets whenever T exceeded them (T>4 base, T>2 wall), and at
    T==2 a wall cutter's end faces landed exactly on the sheet's own
    faces (coincident-face boolean -- the same class of defect as the
    hole_dia==min(length,width) exact-equality case in lib/cadkit).
    Base and walls are the same bent sheet, so the same T/margin
    applies to both.

    Overshoot safety: a cutter's n_hat is always orthogonal to the
    feature's own (u, v) plane, where validate_features already keeps an
    R margin from every bend/corner -- extra depth travels only along the
    thickness axis, never into a bend region. Outward (away from the
    part) always exits into free space regardless of margin size. Inward,
    a base cutter exits into the open-topped interior (nothing to hit).
    Inward, a wall cutter travels toward the OPPOSITE wall -- the one
    real risk -- so it's checked against L/W below, which are already
    smaller than the true interior clearance (inner_length/inner_width
    minus 2R) and so a conservative (stricter) stand-in for it."""
    opposite_wall_span = {"front": W, "back": W, "left": L, "right": L}

    shape = box_shape
    depth = T + 2 * CUT_THROUGH_MARGIN_MM
    for row in parsed_rows:
        if row["face"] != "base" and CUT_THROUGH_MARGIN_MM >= opposite_wall_span[row["face"]]:
            raise SystemExit(
                f"cut-through margin {CUT_THROUGH_MARGIN_MM}mm too large for face "
                f"{row['face']} (interior span {opposite_wall_span[row['face']]:.3f}mm) -- "
                f"would reach the opposite wall."
            )
        for u_i, v_j in row["instances"]:
            center, p_hat, q_hat, n_hat = face_geometry(row["face"], u_i, v_j, L, W, R, T, shape)
            cutter = build_feature_cutter(
                row["type"], row["size1_mm"], row["size2_mm"], row["rotation_deg"],
                center, p_hat, q_hat, n_hat, depth,
            )
            new_shape = shape.cut(cutter)
            if not new_shape.isValid():
                raise SystemExit(f"shape invalid after cutting feature {row} instance ({u_i},{v_j}).")
            shape = new_shape
    return shape


def remove_stale_outputs(out_dir, variant_id):
    """Deletes this variant's own previous STEP/DXF (if any) -- called only
    once it's conclusively decided that this run will NOT (re)produce them
    (feature-fit failure or a caught construction-time SystemExit, see
    build_variant), never before or during construction. Leaves every
    other file alone."""
    (out_dir / "step" / f"{variant_id}.step").unlink(missing_ok=True)
    (out_dir / "dxf" / f"{variant_id}.dxf").unlink(missing_ok=True)


# Measured (freecadcmd, this SheetMetal build): a wall's bend unfolds fine
# at bend_radius_mm=0.0015mm but SheetMetalNewUnfolder.getUnfold() raises
# "Can't process non-circular single-edge loop" at bend_radius_mm=0.001mm.
# The floor below is ~100x the measured failure point and still far below
# any physically real bend radius, so it only ever rejects typo-scale
# values, never a legitimate one.
MIN_BEND_RADIUS_MM = 0.1


def validate_box(row):
    """Layer-1 validation for one boxes.csv row, run before any FreeCAD
    document is created (mirrors validate_features()'s contract for
    features.csv rows). Returns (values, errors): values has all of
    NUMERIC_FIELDS as floats (0.0 for any that didn't parse, so the
    manifest/xlsx writer always gets a real number even for a failed row);
    errors is empty iff construction may proceed.

    Checks, in order (each stage assumes the previous one held):
      1. every field present and numeric
      2. thickness_mm > 0 -- required for SMBaseBend at all: thickness<=0
         raises a raw OCCError ("NULL shape") while building the base,
         measured directly (thickness_mm=0 and thickness_mm=-1.5 both do
         this; there is no boundary to find, 0 and below simply don't
         produce a shape).
      3. bend_radius_mm >= MIN_BEND_RADIUS_MM -- see constant above.
      4. L = inner_length_mm - 2*bend_radius_mm > 0, and symmetrically
         W = inner_width_mm - 2*bend_radius_mm > 0 -- the base sketch is a
         rectangle L x W; at L==0 (or W==0) SMBaseBend raises a raw
         OCCError ("Both points are equal", measured) building a
         zero-width rectangle, and negative L/W silently fold the box
         inside-out (still isValid()==True, but crashes measure_interior
         with the exact ValueError this validation exists to prevent).
      5. leg = inner_height_mm - bend_radius_mm > 0 -- SMBendWall's own
         wall-extension length; leg<=0 raises the same raw OCCError as
         thickness<=0 (measured at inner_height_mm==bend_radius_mm and
         below).

    There used to be a 6th check here (both walls' net area > 1000mm^2),
    added because measure_interior()/find_wall_mid() picked their walls'
    faces by an Area<1000 cutoff -- which rejected perfectly buildable
    small enclosures (e.g. 60x40x25mm, R=2mm) along with the actually
    degenerate ones. Fixed at the source instead: those two functions now
    select the wall's outer-skin face by position (find_wall_outer,
    above), which is well-defined at any box size, so there is nothing
    left for this layer to reject.
    """
    values = {}
    errors = []
    for field in NUMERIC_FIELDS:
        raw = (row.get(field) or "").strip()
        if not raw:
            errors.append(f"'{field}' is missing")
            values[field] = 0.0
            continue
        try:
            values[field] = float(raw)
        except ValueError:
            errors.append(f"'{field}' is not a number: {raw!r}")
            values[field] = 0.0
    if errors:
        return values, errors

    inner_length = values["inner_length_mm"]
    inner_width = values["inner_width_mm"]
    inner_height = values["inner_height_mm"]
    T = values["thickness_mm"]
    R = values["bend_radius_mm"]

    if T <= 0:
        errors.append(f"thickness_mm must be > 0 (got {T})")
    if R < MIN_BEND_RADIUS_MM:
        errors.append(f"bend_radius_mm must be >= {MIN_BEND_RADIUS_MM}mm (got {R})")
    if errors:
        return values, errors

    L = inner_length - 2 * R
    W = inner_width - 2 * R
    leg = inner_height - R

    if L <= 0:
        errors.append(
            f"inner_length_mm must be > 2*bend_radius_mm (inner_length_mm={inner_length}, "
            f"bend_radius_mm={R} -> L={L:.4f})"
        )
    if W <= 0:
        errors.append(
            f"inner_width_mm must be > 2*bend_radius_mm (inner_width_mm={inner_width}, "
            f"bend_radius_mm={R} -> W={W:.4f})"
        )
    if leg <= 0:
        errors.append(
            f"inner_height_mm must be > bend_radius_mm (inner_height_mm={inner_height}, "
            f"bend_radius_mm={R} -> leg={leg:.4f})"
        )
    return values, errors


def build_variant(row, features_by_variant, out_dir):
    variant_id = row.get("variant_id", "")
    values, box_errors = validate_box(row)
    inner_length = values["inner_length_mm"]
    inner_width = values["inner_width_mm"]
    inner_height = values["inner_height_mm"]
    T = values["thickness_mm"]
    R = values["bend_radius_mm"]
    K = values["k_factor"]

    L = inner_length - 2 * R
    W = inner_width - 2 * R
    leg = inner_height - R

    def failed_result(reason, measured=None, diff=None, feature_status_rows=None):
        """Common return shape for every way this variant can fail --
        feature-fit (validate_features) or geometry construction (the
        SystemExit points below). Old STEP/DXF for this variant, if any,
        are removed HERE and only here: once the variant is conclusively
        not going to be produced, never speculatively before or during
        construction (see module docstring / README "Failure handling")."""
        remove_stale_outputs(out_dir, variant_id)
        return {
            "variant_id": variant_id, "L": L, "W": W, "leg": leg,
            "measured": measured, "diff": diff,
            "feature_status_rows": feature_status_rows or [],
            "produced": False,
            "manifest_row": {
                "variant_id": variant_id,
                "inner_length_mm": inner_length,
                "inner_width_mm": inner_width,
                "inner_height_mm": inner_height,
                "thickness_mm": T,
                "bend_radius_mm": R,
                "k_factor": K,
                "step_file": "",
                "dxf_file": "",
                "status": f"failed: {reason}",
            },
        }

    if box_errors:
        return failed_result("; ".join(box_errors))

    # Filled in as the pipeline below progresses, so that if a SystemExit
    # lands partway through, the except block can report whatever was
    # actually computed for this variant instead of nothing at all.
    measured = None
    diff = None
    parsed_rows = []

    doc = App.newDocument(f"v_{variant_id}")
    try:
        sk = doc.addObject("Sketcher::SketchObject", "BaseSketch")
        sk.addGeometry(Part.LineSegment(App.Vector(0, 0, 0), App.Vector(L, 0, 0)), False)
        sk.addGeometry(Part.LineSegment(App.Vector(L, 0, 0), App.Vector(L, W, 0)), False)
        sk.addGeometry(Part.LineSegment(App.Vector(L, W, 0), App.Vector(0, W, 0)), False)
        sk.addGeometry(Part.LineSegment(App.Vector(0, W, 0), App.Vector(0, 0, 0)), False)
        doc.recompute()

        base = doc.addObject("Part::FeaturePython", "BaseBend")
        SheetMetalBaseCmd.SMBaseBend(base, sk)
        base.Thickness = T
        base.Length = L
        doc.recompute()

        edge_names = []
        for i, e in enumerate(base.Shape.Edges):
            v0, v1 = e.Vertexes[0].Point, e.Vertexes[1].Point
            if abs(v0.z - T) < 1e-6 and abs(v1.z - T) < 1e-6:
                edge_names.append(f"Edge{i + 1}")

        box = doc.addObject("Part::FeaturePython", "Box")
        SheetMetalCmd.SMBendWall(box, base, edge_names)
        box.radius = R
        box.length = leg
        box.kfactor = K
        box.AutoMiter = True
        doc.recompute()

        if not box.Shape.isValid():
            raise SystemExit(f"{variant_id} produced invalid geometry.")

        # Interior measurement is always computed, even for a variant that
        # will end up failed -- it doesn't touch the filesystem.
        meas_L, meas_W, meas_H = measure_interior(box.Shape, L, W, T)
        measured = (meas_L, meas_W, meas_H)
        diff = {
            "inner_length_mm": meas_L - inner_length,
            "inner_width_mm": meas_W - inner_width,
            "inner_height_mm": meas_H - inner_height,
        }

        feature_rows = features_by_variant.get(variant_id, [])
        parsed_rows = validate_features(feature_rows, L, W, R, inner_length, inner_width, inner_height)
        failed_rows = [r for r in parsed_rows if r["status"] == "failed"]

        if failed_rows:
            reasons = "; ".join(
                f"{r['face']}/{r['type']} u={r['u_mm']} v={r['v_mm']}: {r['reason']}" for r in failed_rows
            )
            return failed_result(reasons, measured=measured, diff=diff, feature_status_rows=parsed_rows)

        final_shape = cut_features(box.Shape, parsed_rows, L, W, R, T)
        final_obj = doc.addObject("Part::Feature", "Final")
        final_obj.Shape = final_shape
        doc.recompute()

        flat_face = select_base_face(final_obj.Shape)
        bac = BendAllowanceCalculator.from_single_value(K, "ansi")
        sel_face, unfolded_shape, bend_lines, root_normal, bend_infodata = SheetMetalNewUnfolder.getUnfold(
            bac, final_obj, flat_face
        )
        sketch_profile, inner_wires, hole_wires = SketchExtraction.extract_manually(unfolded_shape, root_normal)
        transform = SketchExtraction.move_to_origin(sketch_profile, sel_face)
        sketch_profile_t = sketch_profile.transformed(transform)
        inner_t = [w.transformed(transform) for w in inner_wires]
        hole_t = [w.transformed(transform) for w in hole_wires]
        bend_t = bend_lines.transformed(transform)
        cut_compound = Part.makeCompound([sketch_profile_t, *inner_t, *hole_t])

        cut_sketch = SketchExtraction.edges_to_sketch_object(cut_compound.Edges, "CUT", [], "#000080")
        bend_sketch = SketchExtraction.edges_to_sketch_object(bend_t.Edges, "BEND", [], "#c00000")
        doc.recompute()

        bb_shape = Part.makeCompound(cut_compound.Edges + bend_t.Edges)
        dxf_bbox = bb_shape.BoundBox

        cut_geo_count = len(cut_sketch.Geometry)
        bend_geo_count = len(bend_sketch.Geometry)
        cut_types = dict(Counter(g.TypeId for g in cut_sketch.Geometry))
        bend_types = dict(Counter(g.TypeId for g in bend_sketch.Geometry))

        # Both files are written only now, after every step of the pipeline
        # above has succeeded -- a previous STEP/DXF for this variant stays
        # on disk exactly as it was until both new files are ready to
        # replace it (export() overwrites in place; there is nothing to
        # delete first).
        step_rel = f"step/{variant_id}.step"
        step_path = out_dir / step_rel
        step_path.parent.mkdir(parents=True, exist_ok=True)
        Part.export([final_obj], str(step_path))

        dxf_rel = f"dxf/{variant_id}.dxf"
        dxf_path = out_dir / dxf_rel
        dxf_path.parent.mkdir(parents=True, exist_ok=True)
        importDXF.export([cut_sketch, bend_sketch], str(dxf_path))
    except SystemExit as e:
        # The four structural stops this script knows how to name (invalid
        # folded shape, ambiguous base face, missing wall face, cut-through
        # margin too large) all land here -- treated exactly like a
        # feature-fit failure: this one variant is skipped, the batch
        # continues. An exception of any OTHER type is a problem this
        # script has no name for and is NOT caught here; main() still
        # writes the manifest for everything processed so far before it
        # propagates (see README "Failure handling"). measured/diff/
        # parsed_rows reflect whatever this variant's pipeline had already
        # computed before the stop -- None/[] if it stopped before getting
        # that far (e.g. an invalid folded shape, before measurement).
        return failed_result(str(e), measured=measured, diff=diff, feature_status_rows=parsed_rows)
    finally:
        App.closeDocument(doc.Name)

    return {
        "variant_id": variant_id, "L": L, "W": W, "leg": leg,
        "measured": (meas_L, meas_W, meas_H), "diff": diff,
        "feature_status_rows": parsed_rows,
        "produced": True,
        "manifest_row": {
            "variant_id": variant_id,
            "inner_length_mm": inner_length,
            "inner_width_mm": inner_width,
            "inner_height_mm": inner_height,
            "thickness_mm": T,
            "bend_radius_mm": R,
            "k_factor": K,
            "step_file": step_rel,
            "dxf_file": dxf_rel,
            "status": "ok",
        },
        "dxf_bbox": (dxf_bbox.XMin, dxf_bbox.XMax, dxf_bbox.YMin, dxf_bbox.YMax),
        "cut_geo_count": cut_geo_count,
        "cut_geo_types": cut_types,
        "bend_geo_count": bend_geo_count,
        "bend_geo_types": bend_types,
    }


def main():
    args = sys.argv[sys.argv.index("--pass") + 1:]
    boxes_csv = Path(args[0])
    features_csv = Path(args[1])
    out_dir = Path(args[2])
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(boxes_csv, newline="", encoding="utf-8") as f:
        box_rows = list(csv.DictReader(f))

    features_by_variant = defaultdict(list)
    if features_csv.exists():
        with open(features_csv, newline="", encoding="utf-8") as f:
            for frow in csv.DictReader(f):
                features_by_variant[frow["variant_id"]].append(frow)

    manifest_rows = []
    all_feature_status = []

    def write_manifests():
        """Called from `finally` below too: manifest.csv/manifest.xlsx/
        features_manifest.csv are always (re)written for every variant
        processed so far, even if the loop has to stop partway through on
        an error build_variant() didn't recognize as a per-variant failure.
        The on-disk manifest must never be left describing a run that
        didn't happen, or silently missing a variant that did (see README
        "Failure handling")."""
        fields = build_manifest_fields(FORMATS, numeric_fields=NUMERIC_FIELDS)
        write_manifest_csv(manifest_rows, out_dir / "manifest.csv", fields)
        write_manifest_xlsx(manifest_rows, out_dir / "manifest.xlsx", fields, numeric_fields=NUMERIC_FIELDS)
        write_manifest_csv(all_feature_status, out_dir / "features_manifest.csv", FEATURE_FIELDS)

    try:
        for row in box_rows:
            result = build_variant(row, features_by_variant, out_dir)
            manifest_rows.append(result["manifest_row"])
            for fs in result["feature_status_rows"]:
                all_feature_status.append({k: v for k, v in fs.items() if k in FEATURE_FIELDS})

            print(f"--- {result['variant_id']} ---")
            print(f"  SheetMetal input: L={result['L']:.4f} W={result['W']:.4f} leg={result['leg']:.4f}")
            if result["measured"] is not None:
                mL, mW, mH = result["measured"]
                print(f"  measured interior: length={mL:.4f} width={mW:.4f} height={mH:.4f}")
                d = result["diff"]
                print(f"  diff vs CSV (mm): length={d['inner_length_mm']:+.4f} "
                      f"width={d['inner_width_mm']:+.4f} height={d['inner_height_mm']:+.4f}")

            if result["produced"]:
                bx = result["dxf_bbox"]
                print(f"  DXF bbox: x[{bx[0]:.4f},{bx[1]:.4f}] y[{bx[2]:.4f},{bx[3]:.4f}]")
                print(f"  CUT: {result['cut_geo_count']} ({result['cut_geo_types']})")
                print(f"  BEND: {result['bend_geo_count']} ({result['bend_geo_types']})")
            else:
                print(f"  NOT PRODUCED: {result['manifest_row']['status']}")

            for fs in result["feature_status_rows"]:
                print(f"  feature {fs['face']}/{fs['type']} u={fs['u_mm']} v={fs['v_mm']}: "
                      f"{fs['status']}" + (f" ({fs['reason']})" if fs["reason"] else ""))
    finally:
        write_manifests()

    print(f"\nmanifest written: {out_dir / 'manifest.csv'}")
    print(f"features_manifest written: {out_dir / 'features_manifest.csv'}")


try:
    main()
except SystemExit:
    raise
except Exception:
    import traceback
    traceback.print_exc()
    sys.exit(1)
