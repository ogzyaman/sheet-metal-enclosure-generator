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

Validation (before production) runs on EVERY grid instance separately.
w,h = the feature's total horizontal/vertical footprint after rotation:
  base: R <= u_i-w/2  and  u_i+w/2 <= inner_length - R
        R <= v_j-h/2  and  v_j+h/2 <= inner_width - R
  wall: R <= u_i-w/2  and  u_i+w/2 <= wall's inner span - R
        R <= v_j-h/2  and  v_j+h/2 <= inner_height   (no -R at the top --
        the open rim is not a bend zone)

If ANY grid instance of ANY feature row for a variant fails validation,
that whole variant is NOT produced: no STEP/DXF are written, and any
STEP/DXF left over from a previous run for that variant are deleted
first. manifest.csv records status="failed: <which feature, which
instance (i,j), why>" for that variant. features_manifest.csv still
records the outcome of every feature row independently, regardless of
whether the variant as a whole was built.

Per successful variant: STEP + layered DXF (CUT/BEND) + manifest.py
(bundled in this repo, no cadkit dependency). Base-face selection:
deterministic rule (the single planar face whose center is at z=0 and
whose outward normal is exactly -Z; anything other than one match is
a hard stop).

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
        raise SystemExit(f"STOP: base-face rule found {len(matches)} matches (expected 1).")
    return f"Face{matches[0]}"


def measure_interior(shape, L, W, T):
    """Interior clear dimensions: gap between the INNER faces of each
    wall pair (x-normal walls give interior length, y-normal walls give
    interior width), and interior height = wall top (shape ZMax) minus
    base top (z=T)."""
    x_faces, y_faces = [], []
    for f in shape.Faces:
        if not isinstance(f.Surface, Part.Plane):
            continue
        n = f.Surface.Axis
        c = f.CenterOfMass
        if f.Area < 1000:
            continue
        if abs(n.x) > 0.99 and abs(n.y) < 0.01:
            x_faces.append(c.x)
        elif abs(n.y) > 0.99 and abs(n.x) < 0.01:
            y_faces.append(c.y)

    left = [v for v in x_faces if v < L / 2]
    right = [v for v in x_faces if v >= L / 2]
    front = [v for v in y_faces if v < W / 2]
    back = [v for v in y_faces if v >= W / 2]

    inner_x_lo, inner_x_hi = max(left), min(right)
    inner_y_lo, inner_y_hi = max(front), min(back)

    inner_length = inner_x_hi - inner_x_lo
    inner_width = inner_y_hi - inner_y_lo
    inner_height = shape.BoundBox.ZMax - T

    return inner_length, inner_width, inner_height


def find_wall_mid(shape, axis, positive_side, threshold):
    """Mean coordinate (along `axis`, 'x' or 'y') of the wall's inner+outer
    skin faces on the given side (positive_side selects > threshold, else
    < threshold). Same face-detection pattern used throughout this
    project's exploratory scripts (normal aligned to axis, area large
    enough to exclude corner-relief slivers)."""
    vals = []
    for f in shape.Faces:
        if not isinstance(f.Surface, Part.Plane):
            continue
        n = f.Surface.Axis
        c = f.CenterOfMass
        if f.Area < 1000:
            continue
        if axis == "x" and abs(n.x) > 0.99 and abs(n.y) < 0.01:
            val = c.x
        elif axis == "y" and abs(n.y) > 0.99 and abs(n.x) < 0.01:
            val = c.y
        else:
            continue
        if (val > threshold) == positive_side:
            vals.append(val)
    if not vals:
        raise SystemExit(f"STOP: no wall face found (axis={axis}, positive_side={positive_side}).")
    return sum(vals) / len(vals)


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
        y_mid = find_wall_mid(box_shape, "y", False, W / 2.0)
        center = App.Vector(cx, y_mid, cz)
        return center, App.Vector(1, 0, 0), App.Vector(0, 0, 1), App.Vector(0, 1, 0)
    if face == "back":
        cx, cz = (L + R) - u, T + v
        y_mid = find_wall_mid(box_shape, "y", True, W / 2.0)
        center = App.Vector(cx, y_mid, cz)
        return center, App.Vector(-1, 0, 0), App.Vector(0, 0, 1), App.Vector(0, 1, 0)
    if face == "left":
        cy, cz = (W + R) - u, T + v
        x_mid = find_wall_mid(box_shape, "x", False, L / 2.0)
        center = App.Vector(x_mid, cy, cz)
        return center, App.Vector(0, -1, 0), App.Vector(0, 0, 1), App.Vector(1, 0, 0)
    if face == "right":
        cy, cz = u - R, T + v
        x_mid = find_wall_mid(box_shape, "x", True, L / 2.0)
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


def cut_features(box_shape, parsed_rows, L, W, R, T):
    """Cuts every instance of every (already validated) feature into the
    shape, in order. Assumes all rows in parsed_rows have status == 'ok'."""
    shape = box_shape
    for row in parsed_rows:
        depth = 4.0 if row["face"] == "base" else 2.0
        for u_i, v_j in row["instances"]:
            center, p_hat, q_hat, n_hat = face_geometry(row["face"], u_i, v_j, L, W, R, T, shape)
            cutter = build_feature_cutter(
                row["type"], row["size1_mm"], row["size2_mm"], row["rotation_deg"],
                center, p_hat, q_hat, n_hat, depth,
            )
            new_shape = shape.cut(cutter)
            if not new_shape.isValid():
                raise SystemExit(f"STOP: shape invalid after cutting feature {row} instance ({u_i},{v_j}).")
            shape = new_shape
    return shape


def remove_stale_outputs(out_dir, variant_id):
    """Deletes this variant's own previous STEP/DXF (if any) before this
    run attempts to (re)produce them -- leaves every other file alone."""
    (out_dir / "step" / f"{variant_id}.step").unlink(missing_ok=True)
    (out_dir / "dxf" / f"{variant_id}.dxf").unlink(missing_ok=True)


def build_variant(row, features_by_variant, out_dir):
    variant_id = row["variant_id"]
    inner_length = float(row["inner_length_mm"])
    inner_width = float(row["inner_width_mm"])
    inner_height = float(row["inner_height_mm"])
    T = float(row["thickness_mm"])
    R = float(row["bend_radius_mm"])
    K = float(row["k_factor"])

    L = inner_length - 2 * R
    W = inner_width - 2 * R
    leg = inner_height - R

    remove_stale_outputs(out_dir, variant_id)

    doc = App.newDocument(f"v_{variant_id}")

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
        raise SystemExit(f"STOP: {variant_id} produced invalid geometry.")

    # Interior measurement is always computed, even for a variant that
    # will end up failed -- it doesn't touch the filesystem.
    meas_L, meas_W, meas_H = measure_interior(box.Shape, L, W, T)
    diff = {
        "inner_length_mm": meas_L - inner_length,
        "inner_width_mm": meas_W - inner_width,
        "inner_height_mm": meas_H - inner_height,
    }

    feature_rows = features_by_variant.get(variant_id, [])
    parsed_rows = validate_features(feature_rows, L, W, R, inner_length, inner_width, inner_height)
    failed_rows = [r for r in parsed_rows if r["status"] == "failed"]

    base_result = {
        "variant_id": variant_id, "L": L, "W": W, "leg": leg,
        "measured": (meas_L, meas_W, meas_H), "diff": diff,
        "feature_status_rows": parsed_rows,
    }

    if failed_rows:
        reasons = "; ".join(f"{r['face']}/{r['type']} u={r['u_mm']} v={r['v_mm']}: {r['reason']}" for r in failed_rows)
        App.closeDocument(doc.Name)
        base_result["manifest_row"] = {
            "variant_id": variant_id,
            "inner_length_mm": inner_length,
            "inner_width_mm": inner_width,
            "inner_height_mm": inner_height,
            "thickness_mm": T,
            "bend_radius_mm": R,
            "k_factor": K,
            "step_file": "",
            "dxf_file": "",
            "status": f"failed: {reasons}",
        }
        base_result["produced"] = False
        return base_result

    final_shape = cut_features(box.Shape, parsed_rows, L, W, R, T)
    final_obj = doc.addObject("Part::Feature", "Final")
    final_obj.Shape = final_shape
    doc.recompute()

    step_rel = f"step/{variant_id}.step"
    step_path = out_dir / step_rel
    step_path.parent.mkdir(parents=True, exist_ok=True)
    Part.export([final_obj], str(step_path))

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

    dxf_rel = f"dxf/{variant_id}.dxf"
    dxf_path = out_dir / dxf_rel
    dxf_path.parent.mkdir(parents=True, exist_ok=True)
    importDXF.export([cut_sketch, bend_sketch], str(dxf_path))

    cut_geo_count = len(cut_sketch.Geometry)
    bend_geo_count = len(bend_sketch.Geometry)
    cut_types = dict(Counter(g.TypeId for g in cut_sketch.Geometry))
    bend_types = dict(Counter(g.TypeId for g in bend_sketch.Geometry))

    App.closeDocument(doc.Name)

    base_result["produced"] = True
    base_result["manifest_row"] = {
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
    }
    base_result["dxf_bbox"] = (dxf_bbox.XMin, dxf_bbox.XMax, dxf_bbox.YMin, dxf_bbox.YMax)
    base_result["cut_geo_count"] = cut_geo_count
    base_result["cut_geo_types"] = cut_types
    base_result["bend_geo_count"] = bend_geo_count
    base_result["bend_geo_types"] = bend_types
    return base_result


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
    for row in box_rows:
        result = build_variant(row, features_by_variant, out_dir)
        manifest_rows.append(result["manifest_row"])
        for fs in result["feature_status_rows"]:
            all_feature_status.append({k: v for k, v in fs.items() if k in FEATURE_FIELDS})

        print(f"--- {result['variant_id']} ---")
        print(f"  SheetMetal input: L={result['L']:.4f} W={result['W']:.4f} leg={result['leg']:.4f}")
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

    fields = build_manifest_fields(FORMATS, numeric_fields=NUMERIC_FIELDS)
    write_manifest_csv(manifest_rows, out_dir / "manifest.csv", fields)
    write_manifest_xlsx(manifest_rows, out_dir / "manifest.xlsx", fields, numeric_fields=NUMERIC_FIELDS)
    write_manifest_csv(all_feature_status, out_dir / "features_manifest.csv", FEATURE_FIELDS)
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
