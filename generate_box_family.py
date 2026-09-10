"""CSV'den sac kutu ailesi (delik/yuva/kesik YOK, sadece kutu).
freecadcmd ile calisir:
  freecadcmd generate_box_family.py --pass <input.csv> <out_dir>

Girdi CSV sutunlari:
  variant_id, inner_length_mm, inner_width_mm, inner_height_mm,
  thickness_mm, bend_radius_mm, k_factor

Donusum (SheetMetal girdisi):
  L = inner_length - 2R, W = inner_width - 2R, leg = inner_height - R

Her varyant icin: STEP + katmanli DXF (CUT/BEND) + manifest (bu repo
icindeki manifest.py, cadkit'e bagimli degil). Taban yuzu secimi:
deterministik kural (merkezi z=0, disa donuk normali -Z olan tek
duzlem yuz; tek eslesme yoksa DUR).

freecadcmd notu: ekstra CLI argumanlari --pass'tan SONRA verilmeli,
yoksa FreeCAD onlari dosya olarak acmaya calisir. Script FreeCAD'in
kendi baslatma baglaminda calistigi icin __name__ "__main__" OLMUYOR
-- main() bu yuzden kosulsuz cagriliyor.
"""
import csv
import sys
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
        raise SystemExit(f"DUR: taban dis yuzu kurali {len(matches)} eslesme buldu (1 beklenirdi).")
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


def build_variant(row, out_dir):
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
        raise SystemExit(f"DUR: {variant_id} gecersiz geometri.")

    # --- interior measurement ---
    meas_L, meas_W, meas_H = measure_interior(box.Shape, L, W, T)
    diff = {
        "inner_length_mm": meas_L - inner_length,
        "inner_width_mm": meas_W - inner_width,
        "inner_height_mm": meas_H - inner_height,
    }

    # --- STEP export ---
    step_rel = f"step/{variant_id}.step"
    step_path = out_dir / step_rel
    step_path.parent.mkdir(parents=True, exist_ok=True)
    Part.export([box], str(step_path))

    # --- unfold + layered DXF ---
    flat_face = select_base_face(box.Shape)
    bac = BendAllowanceCalculator.from_single_value(K, "ansi")
    sel_face, unfolded_shape, bend_lines, root_normal, bend_infodata = SheetMetalNewUnfolder.getUnfold(
        bac, box, flat_face
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

    App.closeDocument(doc.Name)

    manifest_row = {
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

    return {
        "variant_id": variant_id,
        "L": L, "W": W, "leg": leg,
        "measured": (meas_L, meas_W, meas_H),
        "diff": diff,
        "dxf_bbox": (dxf_bbox.XMin, dxf_bbox.XMax, dxf_bbox.YMin, dxf_bbox.YMax),
        "manifest_row": manifest_row,
    }


def main():
    args = sys.argv[sys.argv.index("--pass") + 1:]
    csv_path = Path(args[0])
    out_dir = Path(args[1])
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    manifest_rows = []
    for row in rows:
        result = build_variant(row, out_dir)
        manifest_rows.append(result["manifest_row"])
        print(f"--- {result['variant_id']} ---")
        print(f"  SheetMetal input: L={result['L']:.4f} W={result['W']:.4f} leg={result['leg']:.4f}")
        mL, mW, mH = result["measured"]
        print(f"  measured interior: length={mL:.4f} width={mW:.4f} height={mH:.4f}")
        d = result["diff"]
        print(f"  diff vs CSV (mm): length={d['inner_length_mm']:+.4f} "
              f"width={d['inner_width_mm']:+.4f} height={d['inner_height_mm']:+.4f}")
        bx = result["dxf_bbox"]
        print(f"  DXF bbox: x[{bx[0]:.4f},{bx[1]:.4f}] y[{bx[2]:.4f},{bx[3]:.4f}]")

    fields = build_manifest_fields(FORMATS, numeric_fields=NUMERIC_FIELDS)
    write_manifest_csv(manifest_rows, out_dir / "manifest.csv", fields)
    write_manifest_xlsx(manifest_rows, out_dir / "manifest.xlsx", fields, numeric_fields=NUMERIC_FIELDS)
    print(f"\nmanifest yazildi: {out_dir / 'manifest.csv'}")


try:
    main()
except SystemExit:
    raise
except Exception:
    import traceback
    traceback.print_exc()
    sys.exit(1)
