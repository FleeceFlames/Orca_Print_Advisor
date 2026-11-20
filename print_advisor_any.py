# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Print Advisor"
# description = "Beginner-friendly 3D print analyzer with selective, zero-risk setting recommendations."
# author = "FleeceFlames"
# version = "1.0.0"
# ///

"""
Print Advisor — Architecture Map
================================
Single-file universal plugin for OrcaSlicer 2.5+ (zero external dependencies, zero OS permissions).

1. CONSTANTS & HELPERS       — Watched OrcaSlicer keys, unit scales, safe attribute getters.
2. 3D MATH & GEOMETRY        — 3MF affine matrices, TRS transforms, shared triangle scanner.
3. UnitHelper & ContextReader— Preserves '%' vs 'mm/s' units; merges .3mf over active presets.
4. LivePlateAnalyzer         — Reads live 3D meshes & Variable Layer Height via orca.host.model().
5. MeshAnalyzer              — Parses .3mf ZIP archives (streaming XML, VLH, modifiers, slice_info).
6. RuleEngine                — Evaluates the 7 zero-risk rules, step-by-step manual guides & guardrails.
7. Patchers                  — LivePlatePatcher (apply + revert) & ThreeMFPatcher (.3mf export).
8. APP_HTML (Pages UI)       — Dark-mode 2-tab HTML/CSS/JS frontend (Advisor | Learn & Safety).
9. PrintAdvisorPage & Plugin — JS <-> Python message router, .3mf cache & preset-safe revert tracker.
"""

import base64
import io
import json
import math
import time
import zipfile
import xml.etree.ElementTree as ET
import orca  # type: ignore


# ============================================================================
# 1. CONSTANTS & BASIC HELPERS
# ============================================================================

# 3MF uses a 4x3 affine matrix flattened to 12 floats (row-vector convention):
#   (m00, m01, m02,   m10, m11, m12,   m20, m21, m22,   tx, ty, tz)
IDENTITY_MAT = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)

# Multiplier to normalize any 3MF <model unit="..."> declaration into millimeters.
UNIT_TO_MM = {
    "micron": 0.001,
    "millimeter": 1.0,
    "centimeter": 10.0,
    "inch": 25.4,
    "foot": 304.8,
    "meter": 1000.0,
}

# Settings read from active presets and .3mf project_settings.config.
# Add any new OrcaSlicer key here first if you write a new rule later.
WATCHED_KEYS = (
    "nozzle_diameter",
    "filament_type",
    "printer_settings_id",
    "filament_settings_id",
    "print_settings_id",
    "spiral_mode",
    "wall_loops",
    "outer_wall_line_width",
    "outer_wall_speed",
    "wall_sequence",
    "wall_generator",
    "ironing_type",
    "enable_overhang_speed",
    "overhang_3_4_speed",
    "overhang_4_4_speed",
    "overhang_fan_speed",
    "overhang_fan_threshold",
    "sparse_infill_pattern",
    "sparse_infill_density",
    "brim_type",
    "brim_width",
    "reduce_crossing_wall",
    "initial_layer_speed",
    "initial_layer_infill_speed",
    "elefant_foot_compensation",
    "layer_height",
    "seam_slope_type",
    "enable_support",
    "support_type",
)

# Keys checked for per-object overrides so we warn when a part overrides a global fix.
WATCHED_OVERRIDE_KEYS = {
    "wall_loops",
    "sparse_infill_pattern",
    "sparse_infill_density",
    "wall_sequence",
    "wall_generator",
    "ironing_type",
    "enable_overhang_speed",
    "overhang_3_4_speed",
    "overhang_4_4_speed",
    "brim_type",
    "brim_width",
    "elefant_foot_compensation",
    "spiral_mode",
    "reduce_crossing_wall",
    "layer_height",
    "initial_layer_speed",
    "initial_layer_infill_speed",
}

# Native type hints used by LivePlatePatcher when calling PresetTransaction.set()
BOOL_KEYS = {"enable_overhang_speed", "reduce_crossing_wall", "spiral_mode"}
INT_KEYS = {"wall_loops"}
FLOAT_KEYS = {
    "layer_height",
    "brim_width",
    "elefant_foot_compensation",
    "overhang_3_4_speed",
    "overhang_4_4_speed",
    "initial_layer_speed",
    "initial_layer_infill_speed",
}


def clean_label(name):
    """Strips OrcaSlicer's leading '* ' marker on modified preset names."""
    if not name:
        return ""
    s = str(name).strip()
    return s[2:].strip() if s.startswith("* ") else s


def local_tag(tag):
    """Strips XML namespace prefixes: '{http://...}vertex' -> 'vertex'."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def normalize_val(val):
    """Normalizes OrcaSlicer config values into a clean string (primary slot [0] for lists)."""
    if val is None:
        return None
    if isinstance(val, list):
        if not val:
            return None
        val = val[0]
    if isinstance(val, bool):
        return "1" if val else "0"
    return str(val).strip()


def safe_get(obj, name, default=None):
    """Safely reads either a method or property on orca.host objects."""
    if not hasattr(obj, name):
        return default
    attr = getattr(obj, name)
    try:
        return attr() if callable(attr) else attr
    except Exception:
        return default


# ============================================================================
# 2. 3D MATH & SHARED GEOMETRY ENGINE
# ============================================================================

def parse_matrix(text, unit_scale=1.0):
    """Parses a 12-float 3MF transform attribute string; scales translation into mm."""
    if not text:
        return IDENTITY_MAT
    parts = [float(x) for x in text.split()]
    if len(parts) != 12:
        return IDENTITY_MAT
    return (
        parts[0], parts[1], parts[2],
        parts[3], parts[4], parts[5],
        parts[6], parts[7], parts[8],
        parts[9] * unit_scale, parts[10] * unit_scale, parts[11] * unit_scale,
    )


def mat_mul(a, b):
    """Row-vector affine multiplication: applies transform A first, then B."""
    return (
        a[0]*b[0] + a[1]*b[3] + a[2]*b[6],
        a[0]*b[1] + a[1]*b[4] + a[2]*b[7],
        a[0]*b[2] + a[1]*b[5] + a[2]*b[8],
        a[3]*b[0] + a[4]*b[3] + a[5]*b[6],
        a[3]*b[1] + a[4]*b[4] + a[5]*b[7],
        a[3]*b[2] + a[4]*b[5] + a[5]*b[8],
        a[6]*b[0] + a[7]*b[3] + a[8]*b[6],
        a[6]*b[1] + a[7]*b[4] + a[8]*b[7],
        a[6]*b[2] + a[7]*b[5] + a[8]*b[8],
        a[9]*b[0] + a[10]*b[3] + a[11]*b[6] + b[9],
        a[9]*b[1] + a[10]*b[4] + a[11]*b[7] + b[10],
        a[9]*b[2] + a[10]*b[5] + a[11]*b[8] + b[11],
    )


def mat_det(m):
    """Determinant of upper-left 3x3; negative means mirrored (flips triangle normals)."""
    return (
        m[0]*(m[4]*m[8] - m[5]*m[7])
        - m[1]*(m[3]*m[8] - m[5]*m[6])
        + m[2]*(m[3]*m[7] - m[4]*m[6])
    )


def transform_point(v, m):
    x, y, z = v
    return (
        x*m[0] + y*m[3] + z*m[6] + m[9],
        x*m[1] + y*m[4] + z*m[7] + m[10],
        x*m[2] + y*m[5] + z*m[8] + m[11],
    )


def build_trs_matrix(offset, rotation, scale, mirror):
    """Builds a row-vector affine matrix matching OrcaSlicer's S*M -> Rx -> Ry -> Rz -> T order."""
    ox, oy, oz = (float(offset[0]), float(offset[1]), float(offset[2])) if offset and len(offset) == 3 else (0.0, 0.0, 0.0)
    rx, ry, rz = (float(rotation[0]), float(rotation[1]), float(rotation[2])) if rotation and len(rotation) == 3 else (0.0, 0.0, 0.0)
    sx, sy, sz = (float(scale[0]), float(scale[1]), float(scale[2])) if scale and len(scale) == 3 else (1.0, 1.0, 1.0)

    if mirror and len(mirror) == 3:
        m_signs = [(-1.0 if v else 1.0) if isinstance(v, bool) else (-1.0 if float(v) < 0 else 1.0) for v in mirror]
    else:
        m_signs = [1.0, 1.0, 1.0]

    s_mat = (sx * m_signs[0], 0.0, 0.0,  0.0, sy * m_signs[1], 0.0,  0.0, 0.0, sz * m_signs[2],  0.0, 0.0, 0.0)
    cx, sx_r = math.cos(rx), math.sin(rx)
    cy, sy_r = math.cos(ry), math.sin(ry)
    cz, sz_r = math.cos(rz), math.sin(rz)

    rx_mat = (1.0, 0.0, 0.0,  0.0, cx, sx_r,  0.0, -sx_r, cx,  0.0, 0.0, 0.0)
    ry_mat = (cy, 0.0, -sy_r,  0.0, 1.0, 0.0,  sy_r, 0.0, cy,  0.0, 0.0, 0.0)
    rz_mat = (cz, sz_r, 0.0,  -sz_r, cz, 0.0,  0.0, 0.0, 1.0,  0.0, 0.0, 0.0)
    t_mat = (1.0, 0.0, 0.0,  0.0, 1.0, 0.0,  0.0, 0.0, 1.0,  ox, oy, oz)

    return mat_mul(mat_mul(mat_mul(mat_mul(s_mat, rx_mat), ry_mat), rz_mat), t_mat)


def summarize_item_geometry(index, transformed_meshes, stride, has_vlh=False):
    """
    Shared triangle scanner used by both LivePlateAnalyzer and MeshAnalyzer.
    Pass 1 finds the world bounding box; Pass 2 classifies bed contact, bottom-5mm fillets
    (55-85 deg), overhangs, floating bridges (>85 deg), tipping ratio, and inverted normals.
    """
    min_x = min_y = min_z = float("inf")
    max_x = max_y = max_z = float("-inf")

    for t_verts, _, _ in transformed_meshes:
        coords = t_verts.values() if isinstance(t_verts, dict) else t_verts
        for x, y, z in coords:
            if x < min_x: min_x = x
            if x > max_x: max_x = x
            if y < min_y: min_y = y
            if y > max_y: max_y = y
            if z < min_z: min_z = z
            if z > max_z: max_z = z

    if min_z == float("inf"):
        return None, 0, False

    total_area = bed_area = upward_bed_area = 0.0
    steep_overhang_area = bottom_fillet_area = bottom_5mm_total_area = bridge_area = 0.0
    bed_min_x = bed_min_y = float("inf")
    bed_max_x = bed_max_y = float("-inf")
    sampled = 0

    for t_verts, tris, flip in transformed_meshes:
        for v0_i, v1_i, v2_i in tris:
            p0, p1, p2 = t_verts[v0_i], t_verts[v1_i], t_verts[v2_i]
            sampled += 1

            ux, uy, uz = p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]
            vx, vy, vz = p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2]
            nx = (uy * vz - uz * vy) * flip
            ny = (uz * vx - ux * vz) * flip
            nz = (ux * vy - uy * vx) * flip

            mag = math.sqrt(nx * nx + ny * ny + nz * nz)
            if mag < 1e-9:
                continue
            area = 0.5 * mag * stride  # Scale by stride to preserve true mm^2 on sampled meshes
            nz_unit = nz / mag
            total_area += area

            z0, z1, z2 = p0[2] - min_z, p1[2] - min_z, p2[2] - min_z
            z_max_tri, z_min_tri = max(z0, z1, z2), min(z0, z1, z2)
            z_avg_tri = (z0 + z1 + z2) / 3.0

            if z_avg_tri <= 5.0 and z_max_tri > 0.35:
                bottom_5mm_total_area += area

            # Base faces pointing upward indicate inside-out mesh normals
            if z_max_tri <= 0.35 and nz_unit >= 0.95:
                upward_bed_area += area
                continue

            if nz_unit < 0.0:
                if z_max_tri <= 0.35 and nz_unit <= -0.95:
                    bed_area += area
                    for px, py, _ in (p0, p1, p2):
                        if px < bed_min_x: bed_min_x = px
                        if px > bed_max_x: bed_max_x = px
                        if py < bed_min_y: bed_min_y = py
                        if py > bed_max_y: bed_max_y = py
                    continue

                if z_min_tri > 0.15 or z_avg_tri > 0.35:
                    overhang_deg = math.degrees(math.asin(min(1.0, abs(nz_unit))))
                    if 55.0 <= overhang_deg <= 85.0:
                        steep_overhang_area += area
                        if z_avg_tri <= 5.0:
                            bottom_fillet_area += area
                    elif overhang_deg > 85.0 and z_min_tri > 0.4:
                        bridge_area += area

    inverted = upward_bed_area > bed_area and upward_bed_area > 10.0
    size_x, size_y, size_z = max_x - min_x, max_y - min_y, max_z - min_z
    if bed_area > 0.1 and bed_max_x > bed_min_x and bed_max_y > bed_min_y:
        eff_footprint = min(bed_max_x - bed_min_x, bed_max_y - bed_min_y, math.sqrt(bed_area) * 1.5)
    else:
        eff_footprint = max(1.0, math.sqrt(max(0.0, bed_area)))

    summary = {
        "index": index,
        "size_mm": (round(size_x, 1), round(size_y, 1), round(size_z, 1)),
        "bed_area_mm2": round(bed_area, 1),
        "steep_overhang_pct": round((steep_overhang_area / total_area * 100.0) if total_area > 0 else 0.0, 1),
        "steep_overhang_mm2": round(steep_overhang_area, 1),
        "bottom_fillet_pct": round((bottom_fillet_area / bottom_5mm_total_area * 100.0) if bottom_5mm_total_area > 0 else 0.0, 1),
        "bottom_fillet_mm2": round(bottom_fillet_area, 1),
        "bridge_mm2": round(bridge_area, 1),
        "tipping_ratio": round(size_z / max(1.0, eff_footprint), 2),
        "has_vlh": bool(has_vlh),
    }
    return summary, sampled, inverted


def build_mesh_report(
    item_summaries, source_unit, plate_count, total_triangles, sampled_triangles,
    skipped_modifiers, non_manifold_parts, per_object_overrides, any_inverted_normals,
    slice_info, has_project_settings, project_settings,
):
    """Aggregates per-item geometry metrics into the unified report format used by RuleEngine & UI."""
    fillet_needs_vlh = any(
        (i["bottom_fillet_pct"] >= 3.0 or i["bottom_fillet_mm2"] >= 25.0) and not i["has_vlh"]
        for i in item_summaries
    )
    return {
        "empty": False,
        "source_unit": source_unit,
        "object_count": len(item_summaries),
        "plate_count": plate_count,
        "total_triangles": total_triangles,
        "sampled_triangles": sampled_triangles,
        "skipped_modifiers": skipped_modifiers,
        "non_manifold_parts": non_manifold_parts,
        "per_object_overrides": per_object_overrides,
        "inverted_normals": any_inverted_normals,
        "has_variable_layer_height": any(i["has_vlh"] for i in item_summaries),
        "fillet_needs_vlh": fillet_needs_vlh,
        "max_size_mm": (
            max(i["size_mm"][0] for i in item_summaries),
            max(i["size_mm"][1] for i in item_summaries),
            max(i["size_mm"][2] for i in item_summaries),
        ),
        "min_bed_area_mm2": min(i["bed_area_mm2"] for i in item_summaries),
        "total_bed_area_mm2": round(sum(i["bed_area_mm2"] for i in item_summaries), 1),
        "worst_overhang_pct": max(i["steep_overhang_pct"] for i in item_summaries),
        "total_steep_mm2": round(sum(i["steep_overhang_mm2"] for i in item_summaries), 1),
        "worst_bottom_fillet_pct": max(i["bottom_fillet_pct"] for i in item_summaries),
        "total_bottom_fillet_mm2": round(sum(i["bottom_fillet_mm2"] for i in item_summaries), 1),
        "total_bridge_mm2": round(sum(i["bridge_mm2"] for i in item_summaries), 1),
        "worst_tipping_ratio": max(i["tipping_ratio"] for i in item_summaries),
        "slice_info": slice_info,
        "has_project_settings": has_project_settings,
        "project_settings": project_settings,
        "items": item_summaries,
    }


# ============================================================================
# 3. UNIT PRESERVATION & CONTEXT READER
# ============================================================================

class UnitHelper:
    """Evaluates OrcaSlicer settings in absolute units while preserving '%' vs 'mm/s' on write."""

    @staticmethod
    def is_percent(raw):
        return isinstance(raw, str) and raw.strip().endswith("%")

    @staticmethod
    def to_float(raw, default=0.0):
        if raw is None:
            return default
        s = str(raw).strip().rstrip("%").strip()
        try:
            return float(s)
        except ValueError:
            return default

    @staticmethod
    def to_absolute(raw, reference, zero_means_ref=False, default=0.0):
        if raw is None or str(raw).strip() == "":
            return default
        s = str(raw).strip()
        if s.endswith("%"):
            pct = UnitHelper.to_float(s, 0.0)
            return reference if (pct <= 0.0 and zero_means_ref) else (pct / 100.0) * reference
        val = UnitHelper.to_float(s, default)
        return reference if (val <= 0.0 and zero_means_ref) else val

    @staticmethod
    def format_target(raw_orig, target_abs, reference, unit_suffix="mm/s"):
        """Returns (patch_value, display_before, display_after)."""
        orig_str = str(raw_orig).strip() if raw_orig is not None else ""
        if UnitHelper.is_percent(orig_str) and reference > 0:
            pct_val = max(1, round((target_abs / reference) * 100))
            new_raw = f"{pct_val}%"
            old_abs = round(UnitHelper.to_absolute(orig_str, reference, zero_means_ref=True))
            return new_raw, f"{orig_str} (~{old_abs} {unit_suffix})", f"{new_raw} (~{round(target_abs)} {unit_suffix})"

        new_raw = str(int(target_abs)) if target_abs == int(target_abs) else f"{target_abs:g}"
        if orig_str == "0":
            disp_before = f"0 (Unlimited / ~{round(reference)} {unit_suffix})"
        elif orig_str:
            disp_before = f"{orig_str} {unit_suffix}".strip()
        else:
            disp_before = "Default"
        return new_raw, disp_before, f"{new_raw} {unit_suffix}".strip()


class ContextReader:
    """Reads active OrcaSlicer presets and overlays embedded .3mf settings when a file is loaded."""

    @staticmethod
    def get_active_summary():
        bundle = orca.host.preset_bundle()
        printer = bundle.current_printer_preset()
        process = bundle.current_process_preset()
        filaments = bundle.current_filament_preset_names() or []

        values = {}
        for k in WATCHED_KEYS:
            try:
                nv = normalize_val(bundle.full_config_value(k))
                if nv is not None:
                    values[k] = nv
            except Exception:
                pass

        return {
            "printer": clean_label(printer.label()) if printer else "None",
            "nozzle": values.get("nozzle_diameter", "0.4"),
            "filament": clean_label(filaments[0]) if filaments else "None",
            "filament_type": values.get("filament_type", "Unknown"),
            "process": clean_label(process.label()) if process else "None",
            "source_label": "Active OrcaSlicer Project",
            "settings": values,
        }

    @staticmethod
    def build_effective_context(project_settings):
        active = ContextReader.get_active_summary()
        effective = dict(active["settings"])
        has_proj = isinstance(project_settings, dict)
        proj_printer_id = None

        if has_proj:
            for k in WATCHED_KEYS:
                if k in project_settings:
                    nv = normalize_val(project_settings[k])
                    if nv is not None:
                        effective[k] = nv
            if "printer_settings_id" in project_settings:
                proj_printer_id = clean_label(normalize_val(project_settings["printer_settings_id"]))

        printer_name = proj_printer_id if (has_proj and proj_printer_id) else active["printer"]
        filament_name = (
            clean_label(effective["filament_settings_id"])
            if has_proj and effective.get("filament_settings_id")
            else active["filament"]
        )
        process_name = (
            clean_label(effective["print_settings_id"])
            if has_proj and effective.get("print_settings_id")
            else active["process"]
        )

        printer_mismatch = None
        if (
            has_proj
            and proj_printer_id
            and active["printer"] not in ("None", "")
            and proj_printer_id.lower() != active["printer"].lower()
        ):
            printer_mismatch = {"file_printer": proj_printer_id, "active_printer": active["printer"]}

        return {
            "active_meta": {
                "printer": printer_name,
                "nozzle": effective.get("nozzle_diameter", active["nozzle"]),
                "filament": filament_name,
                "filament_type": effective.get("filament_type", active["filament_type"]),
                "process": process_name,
                "source_label": "Loaded .3mf (fallback: Active Project)" if has_proj else "Active OrcaSlicer Project",
            },
            "printer_mismatch": printer_mismatch,
            "settings": effective,
            "has_project_settings": has_proj,
        }


# ============================================================================
# 4. LIVE PLATE ANALYZER (orca.host.model())
# ============================================================================

class LivePlateAnalyzer:
    """
    Scans live 3D objects directly on the active OrcaSlicer project via orca.host.model().
    Reads per-object Variable Layer Height via `obj.has_custom_layering` and mesh health
    via `obj.mesh_errors_count` + `mesh.is_manifold`.
    """

    MAX_SAMPLED_TRIANGLES = 25000

    @staticmethod
    def _is_printable_volume(vol):
        is_part = safe_get(vol, "is_model_part", None)
        if is_part is not None:
            return bool(is_part)
        for neg_flag in ("is_modifier", "is_negative_volume", "is_support_enforcer", "is_support_blocker"):
            if safe_get(vol, neg_flag, False):
                return False
        vtype = safe_get(vol, "type", None)
        if vtype is not None:
            vt_str = str(vtype).lower()
            if any(w in vt_str for w in ("modifier", "negative", "enforcer", "blocker", "invalid")):
                return False
        return True

    @staticmethod
    def analyze():
        try:
            model = orca.host.model()
        except Exception as e:
            return {"empty": True, "reason": f"Could not access active OrcaSlicer plate ({e})."}

        if model is None:
            return {"empty": True, "reason": "No active project is open in OrcaSlicer."}

        objects = safe_get(model, "objects", []) or []
        if not objects:
            return {"empty": True, "reason": "Your active OrcaSlicer plate is empty."}

        total_triangles = sampled_triangles = skipped_modifiers = non_manifold_parts = 0
        per_object_overrides = set()
        item_summaries = []
        any_inverted_normals = False
        item_idx = 0

        for obj in objects:
            for k in (safe_get(obj, "config_keys", []) or []):
                if k in WATCHED_OVERRIDE_KEYS:
                    per_object_overrides.add(k)

            # Detect per-object Variable Layer Height directly from OrcaSlicer
            obj_has_vlh = bool(safe_get(obj, "has_custom_layering", False))
            obj_has_mesh_errors = int(safe_get(obj, "mesh_errors_count", 0) or 0) > 0

            volumes = safe_get(obj, "volumes", []) or []
            instances = safe_get(obj, "instances", []) or []
            if not volumes or not instances:
                continue

            printable_vols = []
            obj_counted_non_manifold = False
            for vol in volumes:
                for k in (safe_get(vol, "config_keys", []) or []):
                    if k in WATCHED_OVERRIDE_KEYS:
                        per_object_overrides.add(k)

                if not LivePlateAnalyzer._is_printable_volume(vol):
                    skipped_modifiers += 1
                    continue

                mesh = safe_get(vol, "mesh", None)
                if mesh is None or safe_get(mesh, "is_empty", False):
                    continue
                t_count = int(safe_get(mesh, "triangle_count", 0) or 0)
                if t_count <= 0:
                    continue

                if (safe_get(mesh, "is_manifold", True) is False or obj_has_mesh_errors) and not obj_counted_non_manifold:
                    non_manifold_parts += 1
                    obj_counted_non_manifold = True

                vol_mat = build_trs_matrix(
                    safe_get(vol, "offset", None),
                    safe_get(vol, "rotation", None),
                    safe_get(vol, "scaling_factor", None),
                    safe_get(vol, "mirror", None),
                )
                printable_vols.append((mesh, t_count, vol_mat))

            if not printable_vols:
                continue

            for inst in instances:
                if safe_get(inst, "printable", True) is False:
                    continue

                inst_mat = build_trs_matrix(
                    safe_get(inst, "offset", None),
                    safe_get(inst, "rotation", None),
                    safe_get(inst, "scaling_factor", None),
                    safe_get(inst, "mirror", None),
                )
                inst_tris_count = sum(tc for _, tc, _ in printable_vols)
                if inst_tris_count <= 0:
                    continue

                total_triangles += inst_tris_count
                stride = max(1, inst_tris_count // LivePlateAnalyzer.MAX_SAMPLED_TRIANGLES)
                transformed_meshes = []

                for mesh, t_count, vol_mat in printable_vols:
                    combined = mat_mul(vol_mat, inst_mat)
                    flip = -1.0 if mat_det(combined) < 0 else 1.0
                    sampled_tris = []
                    t_verts = {}  # Lazy cache: only transform vertices used by sampled triangles
                    for i in range(0, t_count, stride):
                        tri = mesh.triangle(i)
                        sampled_tris.append(tri)
                        for vi in tri:
                            if vi not in t_verts:
                                t_verts[vi] = transform_point(mesh.vertex(vi), combined)
                    transformed_meshes.append((t_verts, sampled_tris, flip))

                item_idx += 1
                summary, sampled_n, inverted = summarize_item_geometry(
                    item_idx, transformed_meshes, stride, has_vlh=obj_has_vlh
                )
                if summary:
                    item_summaries.append(summary)
                    sampled_triangles += sampled_n
                    if inverted:
                        any_inverted_normals = True

        if not item_summaries:
            return {"empty": True, "reason": "No printable 3D geometry found on your active OrcaSlicer plate."}

        return build_mesh_report(
            item_summaries=item_summaries,
            source_unit="millimeter",
            plate_count=None,  # orca.host.model() spans all plates in the active project
            total_triangles=total_triangles,
            sampled_triangles=sampled_triangles,
            skipped_modifiers=skipped_modifiers,
            non_manifold_parts=non_manifold_parts,
            per_object_overrides=sorted(per_object_overrides),
            any_inverted_normals=any_inverted_normals,
            slice_info={"has_slice_info": False, "time_sec": None, "weight_g": None},
            has_project_settings=True,
            project_settings=None,
        )


# ============================================================================
# 5. .3MF ARCHIVE ANALYZER (Pure Python ZIP + Streaming XML)
# ============================================================================

class MeshAnalyzer:
    """
    Parses a .3mf ZIP archive in memory (multi-file assemblies, transforms, modifiers,
    Variable Layer Height via Metadata/layer_heights_profile.txt, and slice_info).
    """

    MAX_SAMPLED_TRIANGLES = 25000

    def __init__(self, zf: zipfile.ZipFile):
        self.zf = zf
        self.zip_map = {name.lstrip("/").lower(): name for name in zf.namelist()}
        self._model_cache = {}

    def _find_entry(self, path):
        return self.zip_map.get(path.lstrip("/").lower())

    def _parse_layer_heights_profile(self):
        """
        Reads Metadata/layer_heights_profile.txt (OrcaSlicer / Bambu Studio format):
          object_id=1|0.0,0.2,0.24,0.44,...
        Returns the set of object IDs that contain a non-empty Variable Layer Height curve.
        """
        vlh_ids = set()
        entry = self._find_entry("Metadata/layer_heights_profile.txt")
        if not entry:
            return vlh_ids

        try:
            text = self.zf.read(entry).decode("utf-8", errors="replace")
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line or "|" not in line:
                    continue
                head, profile = line.split("|", 1)
                head, profile = head.strip(), profile.strip()
                if profile and any(sep in profile for sep in (",", ";", " ")):
                    if head.lower().startswith("object_id="):
                        oid = head.split("=", 1)[1].strip()
                        if oid:
                            vlh_ids.add(oid)
                    elif head:
                        vlh_ids.add(head)
        except Exception:
            pass
        return vlh_ids

    def _parse_model_settings(self):
        """Extracts non-printable modifier parts, per-object overrides, plate count, and XML VLH/height-range flags."""
        non_normal_parts = set()
        per_object_overrides = set()
        vlh_ids_xml = set()
        plate_count = 1

        entry = self._find_entry("Metadata/model_settings.config")
        if not entry:
            return non_normal_parts, [], vlh_ids_xml, plate_count

        try:
            root = ET.fromstring(self.zf.read(entry))
            plates = [el for el in root if local_tag(el.tag) == "plate"]
            if plates:
                plate_count = len(plates)
            for obj in root:
                if local_tag(obj.tag) != "object":
                    continue
                obj_id = obj.attrib.get("id", "")
                for child in obj:
                    tag = local_tag(child.tag)
                    if tag == "metadata":
                        k = child.attrib.get("key", "")
                        if k in WATCHED_OVERRIDE_KEYS:
                            per_object_overrides.add(k)
                        if k == "layer_height_profile" and child.attrib.get("value", "").strip():
                            vlh_ids_xml.add(obj_id)
                    elif tag == "layer_config_ranges":
                        for rng in child.iter():
                            if local_tag(rng.tag) == "metadata" and rng.attrib.get("key") == "layer_height":
                                vlh_ids_xml.add(obj_id)
                                break
                    elif tag == "part":
                        part_id = child.attrib.get("id", "")
                        if child.attrib.get("subtype", "normal_part") != "normal_part":
                            non_normal_parts.add((obj_id, part_id))
                        for pmeta in child:
                            if local_tag(pmeta.tag) == "metadata":
                                k = pmeta.attrib.get("key", "")
                                if k in WATCHED_OVERRIDE_KEYS:
                                    per_object_overrides.add(k)
        except Exception:
            pass

        return non_normal_parts, sorted(per_object_overrides), vlh_ids_xml, plate_count

    def _parse_slice_info(self):
        entry = self._find_entry("Metadata/slice_info.config")
        if not entry:
            return {"has_slice_info": False, "time_sec": None, "weight_g": None}

        total_sec = total_g = 0.0
        found_time = found_weight = False
        try:
            root = ET.fromstring(self.zf.read(entry))
            for plate in root:
                if local_tag(plate.tag) != "plate":
                    continue
                plate_weight_added = False
                for child in plate:
                    tag = local_tag(child.tag)
                    if tag == "metadata":
                        k, v = child.attrib.get("key", ""), child.attrib.get("value", "")
                        if k == "prediction" and v:
                            total_sec += float(v)
                            found_time = True
                        elif k == "weight" and v:
                            total_g += float(v)
                            found_weight = True
                            plate_weight_added = True
                    elif tag == "filament" and not plate_weight_added:
                        used_g = child.attrib.get("used_g", "")
                        if used_g:
                            total_g += float(used_g)
                            found_weight = True
        except Exception:
            pass

        return {
            "has_slice_info": found_time or found_weight,
            "time_sec": round(total_sec) if found_time else None,
            "weight_g": round(total_g, 1) if found_weight else None,
        }

    def parse_project_settings(self):
        entry = self._find_entry("Metadata/project_settings.config")
        if not entry:
            return None
        try:
            return json.loads(self.zf.read(entry).decode("utf-8", errors="replace"))
        except Exception:
            return None

    def _load_model_file(self, path):
        norm = path.lstrip("/").lower()
        if norm in self._model_cache:
            return self._model_cache[norm]

        entry = self._find_entry(path)
        if not entry:
            return {"unit_name": "millimeter", "unit_scale": 1.0, "objects": {}, "build_items": []}

        unit_name, unit_scale = "millimeter", 1.0
        objects, build_items = {}, []
        current_obj_id = None
        current_verts, current_tris, current_comps = [], [], []
        in_build = False

        with self.zf.open(entry, "r") as stream:
            for event, elem in ET.iterparse(stream, events=("start", "end")):
                tag = local_tag(elem.tag)
                if event == "start":
                    if tag == "model":
                        unit_name = elem.attrib.get("unit", "millimeter").lower()
                        unit_scale = UNIT_TO_MM.get(unit_name, 1.0)
                    elif tag == "object":
                        current_obj_id = elem.attrib.get("id", "")
                        current_verts, current_tris, current_comps = [], [], []
                    elif tag == "build":
                        in_build = True
                else:
                    if tag == "vertex" and current_obj_id is not None:
                        current_verts.append((
                            float(elem.attrib["x"]) * unit_scale,
                            float(elem.attrib["y"]) * unit_scale,
                            float(elem.attrib["z"]) * unit_scale,
                        ))
                        elem.clear()
                    elif tag == "triangle" and current_obj_id is not None:
                        current_tris.append((
                            int(elem.attrib["v1"]),
                            int(elem.attrib["v2"]),
                            int(elem.attrib["v3"]),
                        ))
                        elem.clear()
                    elif tag == "component" and current_obj_id is not None:
                        comp_path = next(
                            (v for k, v in elem.attrib.items() if k == "path" or k.endswith("}path")),
                            path,
                        )
                        current_comps.append({
                            "objectid": elem.attrib.get("objectid", ""),
                            "path": comp_path,
                            "transform": parse_matrix(elem.attrib.get("transform"), unit_scale),
                        })
                        elem.clear()
                    elif tag in ("vertices", "triangles", "mesh", "components"):
                        elem.clear()
                    elif tag == "object" and current_obj_id is not None:
                        objects[current_obj_id] = {
                            "type": elem.attrib.get("type", "model"),
                            "vertices": current_verts,
                            "triangles": current_tris,
                            "components": current_comps,
                        }
                        current_obj_id = None
                        elem.clear()
                    elif tag == "item" and in_build:
                        build_items.append({
                            "objectid": elem.attrib.get("objectid", ""),
                            "transform": parse_matrix(elem.attrib.get("transform"), unit_scale),
                        })
                        elem.clear()
                    elif tag == "build":
                        in_build = False
                        elem.clear()

        parsed = {"unit_name": unit_name, "unit_scale": unit_scale, "objects": objects, "build_items": build_items}
        self._model_cache[norm] = parsed
        return parsed

    def _collect_item_meshes(self, model_path, obj_id, mat, non_normal_parts, root_obj_id, out_meshes, depth=0):
        if depth > 8:
            return
        obj = self._load_model_file(model_path)["objects"].get(obj_id)
        if not obj or obj["type"] not in ("model", ""):
            return
        if obj["vertices"] and obj["triangles"]:
            out_meshes.append((obj["vertices"], obj["triangles"], mat))
        for comp in obj["components"]:
            comp_id = comp["objectid"]
            if (root_obj_id, comp_id) in non_normal_parts:
                continue
            self._collect_item_meshes(
                comp["path"], comp_id, mat_mul(comp["transform"], mat),
                non_normal_parts, root_obj_id, out_meshes, depth + 1,
            )

    def analyze(self):
        non_normal_parts, per_object_overrides, vlh_ids_xml, plate_count = self._parse_model_settings()
        vlh_object_ids = self._parse_layer_heights_profile() | vlh_ids_xml
        slice_info = self._parse_slice_info()
        project_settings = self.parse_project_settings()

        root_path = self._find_entry("3D/3dmodel.model")
        if not root_path:
            models = [n for n in self.zf.namelist() if n.lower().endswith(".model")]
            root_path = models[0] if models else None
        if not root_path:
            return {"error": "No 3D .model file found inside this .3mf archive."}

        root_model = self._load_model_file(root_path)
        build_items = root_model["build_items"] or [
            {"objectid": oid, "transform": IDENTITY_MAT} for oid in root_model["objects"]
        ]

        total_triangles = sampled_triangles = 0
        item_summaries = []
        any_inverted_normals = False
        item_idx = 0

        for item in build_items:
            root_oid = item["objectid"]
            meshes = []
            self._collect_item_meshes(root_path, root_oid, item["transform"], non_normal_parts, root_oid, meshes)
            item_tris_count = sum(len(tris) for _, tris, _ in meshes)
            if item_tris_count == 0:
                continue

            total_triangles += item_tris_count
            stride = max(1, item_tris_count // self.MAX_SAMPLED_TRIANGLES)

            transformed_meshes = []
            for verts, tris, mat in meshes:
                t_verts = [transform_point(v, mat) for v in verts]
                flip = -1.0 if mat_det(mat) < 0 else 1.0
                sampled_tris = tris if stride == 1 else tris[::stride]
                transformed_meshes.append((t_verts, sampled_tris, flip))

            item_has_vlh = (root_oid in vlh_object_ids) or (len(build_items) == 1 and bool(vlh_object_ids))

            item_idx += 1
            summary, sampled_n, inverted = summarize_item_geometry(
                item_idx, transformed_meshes, stride, has_vlh=item_has_vlh
            )
            if summary:
                item_summaries.append(summary)
                sampled_triangles += sampled_n
                if inverted:
                    any_inverted_normals = True

        if not item_summaries:
            return {"error": "No printable mesh triangles found in this .3mf."}

        return build_mesh_report(
            item_summaries=item_summaries,
            source_unit=root_model["unit_name"],
            plate_count=plate_count,
            total_triangles=total_triangles,
            sampled_triangles=sampled_triangles,
            skipped_modifiers=len(non_normal_parts),
            non_manifold_parts=0,
            per_object_overrides=per_object_overrides,
            any_inverted_normals=any_inverted_normals,
            slice_info=slice_info,
            has_project_settings=project_settings is not None,
            project_settings=project_settings,
        )


# ============================================================================
# 6. RULE ENGINE ("DO NO HARM" PHYSICS & INTENT RULES)
# ============================================================================

class RuleEngine:
    """
    Evaluates zero-risk physics & intent rules ONLY when real 3D geometry is present.
    - Auto-patchable tips touch ONLY safe process keys (never filament calibration or supports).
    - Manual tips (`manual_only=True`) provide step-by-step instructions and auto-clear once
      detected (e.g., Variable Layer Height) or when the user clicks 'Mark Done'.
    """

    @staticmethod
    def evaluate(context, mesh_metrics, intent="engineering", print_in_place=False):
        if not mesh_metrics or mesh_metrics.get("empty"):
            return {"tips": [], "good": [], "guardrails": []}

        s = context["settings"]
        overrides = set(mesh_metrics.get("per_object_overrides", []))
        inverted_normals = bool(mesh_metrics.get("inverted_normals"))
        trusted_mesh = None if inverted_normals else mesh_metrics

        tips, good, guardrails = [], [], []
        nozzle = max(0.15, UnitHelper.to_float(s.get("nozzle_diameter", "0.4"), 0.4))
        layer_h = UnitHelper.to_float(s.get("layer_height", "0.2"), 0.2)
        filament_type = str(s.get("filament_type", "Unknown")).upper()
        spiral_mode = s.get("spiral_mode", "0") == "1"
        outer_speed = max(20.0, UnitHelper.to_float(s.get("outer_wall_speed", "150"), 150.0))

        # --- Guardrail Banners ---
        if context.get("printer_mismatch"):
            pm = context["printer_mismatch"]
            guardrails.append(
                f"Printer Mismatch: This .3mf is saved for '{pm['file_printer']}', while your active OrcaSlicer printer is "
                f"'{pm['active_printer']}'. Convert it to your printer first before printing."
            )
        if inverted_normals:
            guardrails.append(
                "Possible inverted mesh normals detected — geometry checks were paused to prevent false alarms. "
                "Right-click the model in OrcaSlicer and click 'Fix model' first."
            )
        if mesh_metrics.get("non_manifold_parts", 0) > 0:
            guardrails.append(
                f"Non-manifold mesh detected ({mesh_metrics['non_manifold_parts']} part(s)) — right-click the model in OrcaSlicer and run 'Fix model' before slicing."
            )
        if spiral_mode:
            guardrails.append(
                "Spiral Vase Mode is active — Wall Strength, Infill, Layer Height, and Wall Order rules are bypassed."
            )
        if (mesh_metrics.get("plate_count") or 1) > 1:
            guardrails.append(
                f"{mesh_metrics['plate_count']} plates detected — geometry checks reflect the most demanding part across plates."
            )
        if not mesh_metrics.get("has_project_settings", False):
            guardrails.append(
                "Raw CAD .3mf (no embedded slicer profile) — analyzing your currently active OrcaSlicer settings. "
                "Save the project in OrcaSlicer first to enable 1-click .3mf export."
            )

        def override_note(patch_keys):
            hit = sorted(k for k in patch_keys if k in overrides)
            return (
                f"Per-object override detected for: {', '.join(hit)}. Check the Objects tab in OrcaSlicer."
                if hit else None
            )

        # --------------------------------------------------------------------
        # RULE 1: Steep Overhang & Bottom Fillet Guard (Priority 1)
        # --------------------------------------------------------------------
        has_bottom_fillet = bool(
            trusted_mesh and (
                trusted_mesh["worst_bottom_fillet_pct"] >= 3.0
                or trusted_mesh["total_bottom_fillet_mm2"] >= 25.0
            )
        )
        has_steep_overhang = bool(
            trusted_mesh and (
                trusted_mesh["worst_overhang_pct"] >= 5.0
                or trusted_mesh["total_steep_mm2"] >= 50.0
            )
        )

        en_overhang = s.get("enable_overhang_speed", "1")
        wall_seq = s.get("wall_sequence", "inner wall/outer wall")
        raw_ov3 = s.get("overhang_3_4_speed", "30")
        raw_ov4 = s.get("overhang_4_4_speed", "10")
        abs_ov3 = UnitHelper.to_absolute(raw_ov3, outer_speed, zero_means_ref=True, default=30.0)
        abs_ov4 = UnitHelper.to_absolute(raw_ov4, outer_speed, zero_means_ref=True, default=15.0)

        r1_patches, r1_diffs = {}, []
        if has_bottom_fillet or has_steep_overhang:
            if en_overhang == "0":
                r1_patches["enable_overhang_speed"] = "1"
                r1_diffs.append({"label": "Slow down for overhangs", "before": "OFF", "after": "ON"})
            if not spiral_mode and wall_seq == "outer wall/inner wall":
                r1_patches["wall_sequence"] = "inner wall/outer wall"
                r1_diffs.append({"label": "Order of walls", "before": "Outer/Inner", "after": "Inner/Outer"})
            if abs_ov4 > 15.5:
                nv, b_s, a_s = UnitHelper.format_target(raw_ov4, 15.0, outer_speed, "mm/s")
                r1_patches["overhang_4_4_speed"] = nv
                r1_diffs.append({"label": "Overhang speed (75–100%)", "before": b_s, "after": a_s})
            if (has_bottom_fillet or intent == "figure") and abs_ov3 > 20.5:
                nv, b_s, a_s = UnitHelper.format_target(raw_ov3, 20.0, outer_speed, "mm/s")
                r1_patches["overhang_3_4_speed"] = nv
                r1_diffs.append({"label": "Overhang speed (50–75%)", "before": b_s, "after": a_s})

            if r1_patches:
                if has_bottom_fillet:
                    detected = f"Detected steep bottom-5mm curved edges ({trusted_mesh['worst_bottom_fillet_pct']}% / {trusted_mesh['total_bottom_fillet_mm2']} mm²) where plastic tends to curl upward."
                else:
                    detected = f"Detected steep downward overhangs ({trusted_mesh['worst_overhang_pct']}% / {trusted_mesh['total_steep_mm2']} mm²) with fast overhang settings."
                tips.append({
                    "id": "steep_overhang", "bucket": "safety", "priority": 1,
                    "default_checked": True, "manual_only": False,
                    "title": "Fix Steep Bottom Curves & Overhangs",
                    "detected": detected,
                    "why": "Rounded bottom edges print mostly in mid-air. Slowing down gives hot plastic time to cool before curling up, and Inner/Outer wall order gives the outer edge a solid anchor.",
                    "manual_path": "Speed -> Overhang speed  |  Quality -> Order of walls",
                    "diffs": r1_diffs, "patches": r1_patches,
                    "impact": {"time_pct": 3.5, "time_min": 0, "filament_pct": 0.0, "filament_g": 0.0, "label": "+2–5% time | +0g filament"},
                    "override_warning": override_note(r1_patches.keys()),
                })
            else:
                good.append(f"Overhang Speeds & Wall Order: Slowdown is active ({round(abs_ov3)} / {round(abs_ov4)} mm/s) with Inner/Outer wall order.")
        elif trusted_mesh:
            good.append("Overhang Geometry: No significant steep overhangs or bottom fillets detected.")

        # Read-Only Step-by-Step Manual Guide for Bottom Fillets (Auto-clears when VLH is detected)
        if has_bottom_fillet and not spiral_mode:
            manual_actions = []
            manual_steps = []
            fillet_needs_vlh = bool(trusted_mesh.get("fillet_needs_vlh", True))

            if layer_h > 0.14:
                if fillet_needs_vlh:
                    manual_actions.append({
                        "label": "Variable Layer Height (bottom 5mm)",
                        "before": f"Fixed {layer_h:g} mm",
                        "after": "Adaptive + Smooth (0.08–0.12 mm on curve)",
                    })
                    manual_steps.extend([
                        "Select your model in the Prepare tab and click the Variable Layer Height icon (stacked horizontal lines) in the top toolbar.",
                        "Drag 'Quality / Speed' slightly left to 0.25–0.35 (or keep 0.50 for a lighter effect) and click 'Adaptive' once.",
                        "Leave 'Radius' at 5, click 'Smooth' 1–2 times to blend the transitions, then click 'Done'.",
                        "Click 'Slice plate' -> Preview tab -> change the top-right color dropdown from 'Line Type' to 'Layer Height' and orbit under the bottom curves to verify thinner layers (0.08–0.12 mm).",
                    ])
                else:
                    good.append("Variable Layer Height: Custom layer height curve detected on curved bottom-fillet geometry.")

            if "PLA" in filament_type or "PETG" in filament_type:
                raw_fan = s.get("overhang_fan_speed")
                raw_thresh = s.get("overhang_fan_threshold")
                fan_val = UnitHelper.to_float(raw_fan, 0.0) if raw_fan is not None else None
                thresh_val = UnitHelper.to_float(raw_thresh, 50.0) if raw_thresh is not None else None
                target_fan = "70–90%" if "PETG" in filament_type else "100%"

                needs_cooling_tweak = (
                    (fan_val is None)
                    or (fan_val < 70.0)
                    or (thresh_val is not None and thresh_val >= 75.0)
                )
                if needs_cooling_tweak:
                    cur_desc = f"Fan {int(fan_val) if fan_val is not None else '?'}% @ {raw_thresh or '?'} threshold"
                    manual_actions.append({
                        "label": f"{filament_type} Overhang Cooling (Filament Tab)",
                        "before": cur_desc,
                        "after": f"{target_fan} fan @ 25–50% threshold",
                    })
                    manual_steps.append(
                        f"Click the Edit icon next to your filament -> Cooling tab -> enable 'Force cooling for overhangs and bridges' and set 'Fan speed for overhangs' to {target_fan} with a 25–50% threshold."
                    )
                else:
                    good.append(
                        f"{filament_type} Overhang Cooling: Already set to {int(fan_val)}% fan @ {raw_thresh or 'default'} threshold — no filament change needed."
                    )

            if manual_actions:
                tips.append({
                    "id": "bottom_fillet_manual", "bucket": "safety", "priority": 1,
                    "default_checked": False, "manual_only": True,
                    "title": "Manual Action: Smooth Bottom Fillets (Curved Base Edges)",
                    "detected": f"Bottom-5mm curved edges detected ({trusted_mesh['worst_bottom_fillet_pct']}% / {trusted_mesh['total_bottom_fillet_mm2']} mm²) at {layer_h:g}mm layer height.",
                    "why": "Speed alone cannot fully fix steep bottom fillets at 0.20mm layers—each outer line still hangs mostly in mid-air. Variable Layer Height thins just the curved section so each layer has far more overlap without slowing down flat walls.",
                    "manual_path": "Prepare -> Select Model -> Top Toolbar: Variable Layer Height (Adaptive + Smooth)",
                    "diffs": manual_actions,
                    "manual_steps": manual_steps,
                    "patches": {},
                    "impact": {"time_pct": 0.0, "time_min": 0, "filament_pct": 0.0, "filament_g": 0.0, "label": "Manual step in OrcaSlicer"},
                    "override_warning": None,
                })

        # --------------------------------------------------------------------
        # RULE 2: Nozzle Collision Prevention — Grid -> Gyroid (Priority 1)
        # --------------------------------------------------------------------
        infill_pat = s.get("sparse_infill_pattern", "gyroid").lower()
        infill_dens = UnitHelper.to_float(s.get("sparse_infill_density", "15%"), 15.0)
        if not spiral_mode:
            if infill_pat == "grid" and infill_dens > 0:
                r2_patches = {"sparse_infill_pattern": "gyroid"}
                tips.append({
                    "id": "grid_infill", "bucket": "safety", "priority": 1,
                    "default_checked": True, "manual_only": False,
                    "title": "Switch Infill from Grid to Gyroid",
                    "detected": f"Infill is set to Grid ({int(infill_dens)}%), which crosses over itself on the same layer.",
                    "why": "Grid infill creates tiny plastic bumps where lines intersect. The nozzle can strike those bumps and knock your print loose. Gyroid never crosses itself.",
                    "manual_path": "Strength -> Sparse infill pattern -> Gyroid",
                    "diffs": [{"label": "Sparse infill pattern", "before": "grid", "after": "gyroid"}],
                    "patches": r2_patches,
                    "impact": {"time_pct": 1.5, "time_min": 0, "filament_pct": 0.0, "filament_g": 0.0, "label": "+0–3% time | ~0g filament"},
                    "override_warning": override_note(r2_patches.keys()),
                })
            elif infill_dens > 0:
                good.append(f"Infill Pattern: Using '{infill_pat}' ({int(infill_dens)}%) — avoids self-intersecting grid collisions.")

        # --------------------------------------------------------------------
        # RULE 3: Intent-Specific Recommendations (Priority 2 & 3)
        # --------------------------------------------------------------------
        wall_loops = int(round(UnitHelper.to_float(s.get("wall_loops", "2"), 2.0)))
        line_width = UnitHelper.to_absolute(s.get("outer_wall_line_width", ""), nozzle, default=nozzle * 1.05)
        if line_width <= 0.05:
            line_width = nozzle * 1.05
        wall_thickness = round(wall_loops * line_width, 2)

        if not spiral_mode:
            if intent == "engineering":
                if wall_thickness < 1.19:
                    target_loops = max(wall_loops + 1, int(math.ceil(1.2 / line_width)))
                    r3_patches = {"wall_loops": str(target_loops)}
                    tips.append({
                        "id": "wall_strength", "bucket": "recommended", "priority": 2,
                        "default_checked": True, "manual_only": False,
                        "title": "Reinforce Walls for Functional Strength",
                        "detected": f"Engineering intent selected, but {wall_loops} wall loops on a {nozzle}mm nozzle gives only ~{wall_thickness}mm shell thickness.",
                        "why": "Outer walls carry almost all the structural load on brackets and mounts. Reaching ~1.2mm+ shell thickness adds far more strength than raising infill percentage.",
                        "manual_path": "Strength -> Walls -> Wall loops",
                        "diffs": [{"label": "Wall loops", "before": f"{wall_loops} (~{wall_thickness}mm)", "after": f"{target_loops} (~{round(target_loops * line_width, 2)}mm)"}],
                        "patches": r3_patches,
                        "impact": {"time_pct": 12.0, "time_min": 0, "filament_pct": 11.0, "filament_g": 0.0, "label": "+10–15% time | +8–15% filament"},
                        "override_warning": override_note(r3_patches.keys()),
                    })
                else:
                    good.append(f"Functional Wall Strength: {wall_loops} walls (~{wall_thickness}mm shell on {nozzle}mm nozzle) is solid for structural loads.")

            elif intent == "figure":
                if layer_h > round(nozzle * 0.41, 3):
                    target_lh = max(0.08, round(nozzle * 0.30, 2))
                    est_pct = max(15, min(80, round(((layer_h / target_lh) - 1.0) * 80)))
                    r3b_patches = {"layer_height": f"{target_lh:g}"}
                    tips.append({
                        "id": "figure_detail", "bucket": "recommended", "priority": 2,
                        "default_checked": True, "manual_only": False,
                        "title": "Finer Layer Height for Smooth Display Detail",
                        "detected": f"Figure / Display intent selected with coarse {layer_h:g}mm layers on a {nozzle}mm nozzle.",
                        "why": "Thinner layers reduce visible stair-stepping on organic curves, faces, and angled tops without using extra filament.",
                        "manual_path": "Quality -> Layer height",
                        "diffs": [{"label": "Layer height", "before": f"{layer_h:g} mm", "after": f"{target_lh:g} mm"}],
                        "patches": r3b_patches,
                        "impact": {"time_pct": float(est_pct), "time_min": 0, "filament_pct": 0.0, "filament_g": 0.0, "label": f"~+{est_pct}% time | +0g filament"},
                        "override_warning": override_note(r3b_patches.keys()),
                    })
                else:
                    good.append(f"Figure Layer Detail: {layer_h:g}mm layer height is well suited for crisp visual detail.")

                # Read-Only Step-by-Step Display Polish Tips (Scarf Seam & Tree Supports)
                cur_scarf = s.get("seam_slope_type", "none").lower()
                cur_sup_en = s.get("enable_support", "0")
                cur_sup_type = s.get("support_type", "normal(auto)").lower()
                needs_scarf = cur_scarf in ("none", "")
                needs_tree = bool(
                    (has_steep_overhang or (trusted_mesh and trusted_mesh["total_bridge_mm2"] >= 40.0))
                    and (cur_sup_en == "0" or "tree" not in cur_sup_type)
                )

                fig_manual = []
                fig_steps = []
                if needs_scarf:
                    fig_manual.append({"label": "Hide vertical Z-seams", "before": "Standard seam", "after": "Scarf joint seam (Contour)"})
                    fig_steps.append("Open Quality -> Seam -> set 'Scarf joint seam' to 'Contour' (best when your filament Flow Ratio is calibrated).")
                else:
                    good.append(f"Display Seam Polish: Scarf joint seam ('{cur_scarf}') is already enabled.")

                if needs_tree:
                    fig_manual.append({"label": "Clean support removal", "before": "Supports OFF / Normal", "after": "Tree (Auto) Organic (0.2mm Top Z)"})
                    fig_steps.append("Open Support tab -> enable Support -> set Type to 'Tree (auto)', Style to 'Organic', and 'Top Z distance' to 0.2 mm.")

                if fig_manual:
                    fig_steps.append("Slice the plate and inspect Preview (confirm supports don't pack inside tight holes and white seam dots look clean).")
                    tips.append({
                        "id": "figure_finish_manual", "bucket": "recommended", "priority": 3,
                        "default_checked": False, "manual_only": True,
                        "title": "Display Polish Tips (Optional Manual Tweaks)",
                        "detected": "Figure / Display mode selected — optional cosmetic upgrades available in OrcaSlicer.",
                        "why": "Scarf seams taper outer loops to hide vertical seam lines on smooth statues, and Tree (Organic) supports leave smaller contact marks than grid supports. Left manual because support placement and scarf seams should always be visually checked in Preview.",
                        "manual_path": "Quality -> Seam -> Scarf joint seam  |  Support -> Type -> Tree (auto)",
                        "diffs": fig_manual,
                        "manual_steps": fig_steps,
                        "patches": {},
                        "impact": {"time_pct": 0.0, "time_min": 0, "filament_pct": 0.0, "filament_g": 0.0, "label": "Optional manual polish"},
                        "override_warning": None,
                    })

            elif intent == "prototype":
                r3c_patches, r3c_diffs = {}, []
                saved_t = saved_f = 0.0
                if wall_loops > 2:
                    r3c_patches["wall_loops"] = "2"
                    r3c_diffs.append({"label": "Wall loops", "before": str(wall_loops), "after": "2"})
                    saved_t -= 10.0
                    saved_f -= 10.0
                if infill_dens > 15.0:
                    raw_dens = s.get("sparse_infill_density", "15%")
                    r3c_patches["sparse_infill_density"] = "10%"
                    r3c_diffs.append({"label": "Sparse infill density", "before": raw_dens, "after": "10%"})
                    saved_t -= 8.0
                    saved_f -= 8.0
                if r3c_patches:
                    tips.append({
                        "id": "prototype_fast", "bucket": "recommended", "priority": 2,
                        "default_checked": False, "manual_only": False,
                        "title": "Trim Walls / Infill for a Faster Test Fit (Opt-In)",
                        "detected": f"Fast Prototype selected with {wall_loops} walls and {int(infill_dens)}% infill.",
                        "why": "For quick dimensional fit checks, 2 walls and 10% infill save time and plastic without changing exterior accuracy. Unchecked by default so you decide.",
                        "manual_path": "Strength -> Wall loops & Sparse infill density",
                        "diffs": r3c_diffs, "patches": r3c_patches,
                        "impact": {"time_pct": saved_t, "time_min": 0, "filament_pct": saved_f, "filament_g": 0.0, "label": f"{int(saved_t)}% time | {int(saved_f)}% filament"},
                        "override_warning": override_note(r3c_patches.keys()),
                    })
                else:
                    good.append(f"Fast Prototype Efficiency: {wall_loops} walls and {int(infill_dens)}% infill are already lean.")

            elif intent == "sign":
                wall_gen = s.get("wall_generator", "classic").lower()
                if wall_gen != "arachne":
                    r3d_patches = {"wall_generator": "arachne"}
                    tips.append({
                        "id": "sign_arachne", "bucket": "recommended", "priority": 2,
                        "default_checked": True, "manual_only": False,
                        "title": "Enable Arachne Walls for Crisp Text & Logos",
                        "detected": f"Flat Sign / Multi-Color selected while Wall Generator is set to '{wall_gen}'.",
                        "why": "Classic fixed-width walls can leave tiny gaps in thin lettering and logos. Arachne dynamically varies line width to fill fine text cleanly.",
                        "manual_path": "Quality -> Wall generator -> Arachne",
                        "diffs": [{"label": "Wall generator", "before": wall_gen, "after": "arachne"}],
                        "patches": r3d_patches,
                        "impact": {"time_pct": 1.0, "time_min": 0, "filament_pct": 0.0, "filament_g": 0.0, "label": "~0–2% time | +0g filament"},
                        "override_warning": override_note(r3d_patches.keys()),
                    })
                else:
                    good.append("Sign Text Crispness: Arachne wall generator is already enabled.")

                ironing = s.get("ironing_type", "no ironing").lower()
                if trusted_mesh and trusted_mesh["max_size_mm"][2] <= 20.0 and ironing in ("no ironing", ""):
                    r3e_patches = {"ironing_type": "top"}
                    tips.append({
                        "id": "sign_ironing", "bucket": "recommended", "priority": 3,
                        "default_checked": False, "manual_only": False,
                        "title": "Enable Top-Surface Ironing for a Smooth Sign Face (Opt-In)",
                        "detected": "Flat sign profile detected with Top-Surface Ironing currently OFF.",
                        "why": "Ironing passes the hot nozzle lightly across flat top surfaces for a smooth, matte finish. Unchecked by default—best when filament flow is calibrated.",
                        "manual_path": "Quality -> Ironing -> Ironing type -> Top surfaces",
                        "diffs": [{"label": "Ironing type", "before": "No ironing", "after": "Top surfaces"}],
                        "patches": r3e_patches,
                        "impact": {"time_pct": 10.0, "time_min": 0, "filament_pct": 0.0, "filament_g": 0.5, "label": "+8–15% time | +0.5g filament"},
                        "override_warning": override_note(r3e_patches.keys()),
                    })

        # --------------------------------------------------------------------
        # RULE 4: Tipping & ABS/ASA Warp Guard — Outer Brim (Priority 1)
        # --------------------------------------------------------------------
        brim_type = s.get("brim_type", "auto_brim").lower()
        is_warp_mat = ("ABS" in filament_type) or ("ASA" in filament_type)
        is_tippy = False
        if trusted_mesh:
            tip_ratio, min_bed, max_z = trusted_mesh["worst_tipping_ratio"], trusted_mesh["min_bed_area_mm2"], trusted_mesh["max_size_mm"][2]
            is_tippy = tip_ratio >= 2.8 or (min_bed < 250.0 and max_z >= 40.0)

        if not print_in_place:
            if (is_tippy or is_warp_mat) and brim_type in ("no_brim", ""):
                r4_patches = {"brim_type": "outer_only", "brim_width": "5"}
                if is_tippy and is_warp_mat:
                    title, detected = f"Add Outer Brim (Tall Part + {filament_type} Warp Guard)", f"Tall/narrow geometry ({tip_ratio}x) in {filament_type} with Brim OFF."
                elif is_warp_mat:
                    title, detected = f"Add Outer Brim to Prevent {filament_type} Corner Warping", f"Primary filament is {filament_type} (high thermal shrinkage) with Brim OFF."
                else:
                    title, detected = "Add an Outer Brim for Bed Stability", f"Tall/narrow geometry (aspect ratio {tip_ratio}x, base {min_bed} mm²) with Brim OFF."
                tips.append({
                    "id": "brim_guard", "bucket": "safety", "priority": 1,
                    "default_checked": True, "manual_only": False,
                    "title": title, "detected": detected,
                    "why": "Adds a thin, peel-off 5mm 'foot' around Layer 1 to anchor corners against warping and keep tall parts from tipping over.",
                    "manual_path": "Others -> Bed adhesion -> Brim type -> Outer brim only (5mm)",
                    "diffs": [
                        {"label": "Brim type", "before": "No-brim", "after": "Outer brim only"},
                        {"label": "Brim width", "before": s.get("brim_width", "0") + " mm", "after": "5 mm"},
                    ],
                    "patches": r4_patches,
                    "impact": {"time_pct": 0.0, "time_min": 1.5, "filament_pct": 0.0, "filament_g": 2.0, "label": "+1–2 min | +1–3g filament"},
                    "override_warning": override_note(r4_patches.keys()),
                })
            elif trusted_mesh and not is_tippy and not is_warp_mat:
                good.append(f"Bed Stability: Wide footprint (tipping ratio {tip_ratio}x, {min_bed} mm² contact) — no extra brim required.")
            elif is_tippy or is_warp_mat:
                good.append(f"Bed Adhesion: Brim is already set to '{brim_type}'.")

        # --------------------------------------------------------------------
        # RULE 5: PETG / TPU Stringing Guard (Priority 2)
        # --------------------------------------------------------------------
        if ("PETG" in filament_type) or ("TPU" in filament_type):
            mat_name = "TPU" if "TPU" in filament_type else "PETG"
            if s.get("reduce_crossing_wall", "0") == "0":
                r5_patches = {"reduce_crossing_wall": "1"}
                tips.append({
                    "id": "stringing_guard", "bucket": "recommended", "priority": 2,
                    "default_checked": True, "manual_only": False,
                    "title": f"Reduce {mat_name} Stringing (Avoid Crossing Walls)",
                    "detected": f"Primary filament is {filament_type}, and 'Avoid crossing walls' is OFF.",
                    "why": f"{mat_name} tends to ooze fine hairs across open gaps. Routing travel inside the part keeps outer walls clean without changing retraction calibration.",
                    "manual_path": "Quality -> Precision / Travel -> Avoid crossing walls",
                    "diffs": [{"label": "Avoid crossing walls", "before": "OFF", "after": "ON"}],
                    "patches": r5_patches,
                    "impact": {"time_pct": 3.0, "time_min": 0, "filament_pct": 0.0, "filament_g": 0.0, "label": "+2–5% time | +0g filament"},
                    "override_warning": override_note(r5_patches.keys()),
                })
            else:
                good.append(f"{mat_name} Stringing Guard: 'Avoid crossing walls' is already enabled.")

        # --------------------------------------------------------------------
        # RULE 6: First-Layer Bond — CoreXY-Safe (Priority 1)
        # --------------------------------------------------------------------
        raw_fl_speed = s.get("initial_layer_speed", "50")
        raw_fl_infill = s.get("initial_layer_infill_speed", "105")
        abs_fl_speed = UnitHelper.to_absolute(raw_fl_speed, outer_speed, default=50.0)
        abs_fl_infill = UnitHelper.to_absolute(raw_fl_infill, outer_speed, default=105.0)

        r6_patches, r6_diffs = {}, []
        if abs_fl_speed > 60.5:
            nv, b_s, a_s = UnitHelper.format_target(raw_fl_speed, 50.0, outer_speed, "mm/s")
            r6_patches["initial_layer_speed"] = nv
            r6_diffs.append({"label": "Initial layer speed", "before": b_s, "after": a_s})
        if abs_fl_infill > 115.5:
            nv, b_s, a_s = UnitHelper.format_target(raw_fl_infill, 105.0, outer_speed, "mm/s")
            r6_patches["initial_layer_infill_speed"] = nv
            r6_diffs.append({"label": "Initial layer infill speed", "before": b_s, "after": a_s})

        if r6_patches:
            tips.append({
                "id": "first_layer_bond", "bucket": "safety", "priority": 1,
                "default_checked": True, "manual_only": False,
                "title": "Slow Down Layer 1 Extrusion for Reliable Adhesion",
                "detected": f"First-layer print ({round(abs_fl_speed)} mm/s) or infill ({round(abs_fl_infill)} mm/s) speed is aggressive.",
                "why": "Only affects Layer 1. Giving extruded base lines slightly more time to bond prevents peeling corners without slowing down the rest of the print.",
                "manual_path": "Speed -> Initial layer speed",
                "diffs": r6_diffs, "patches": r6_patches,
                "impact": {"time_pct": 0.0, "time_min": 1.5, "filament_pct": 0.0, "filament_g": 0.0, "label": "+1–2 min | +0g filament"},
                "override_warning": override_note(r6_patches.keys()),
            })
        else:
            good.append(f"First-Layer Bond: Layer 1 speeds ({round(abs_fl_speed)} / {round(abs_fl_infill)} mm/s) are in a safe range.")

        # --------------------------------------------------------------------
        # RULE 7: Print-in-Place Guard (Priority 1)
        # --------------------------------------------------------------------
        if print_in_place:
            ef_comp = UnitHelper.to_float(s.get("elefant_foot_compensation", "0"), 0.0)
            r7_patches, r7_diffs = {}, []
            if brim_type != "no_brim":
                r7_patches["brim_type"] = "no_brim"
                r7_diffs.append({"label": "Brim type", "before": brim_type, "after": "No-brim"})
            if ef_comp < 0.12:
                r7_patches["elefant_foot_compensation"] = "0.15"
                r7_diffs.append({"label": "Elephant foot compensation", "before": f"{ef_comp:g} mm", "after": "0.15 mm"})
            if r7_patches:
                tips.append({
                    "id": "print_in_place", "bucket": "safety", "priority": 1,
                    "default_checked": True, "manual_only": False,
                    "title": "Protect Hinges & Snap-Fits (Print-in-Place)",
                    "detected": "Print-in-Place mode is ON while Brim or low Elephant's Foot compensation could weld clearances shut.",
                    "why": "Brims and first-layer squish fuse hinges and snap-fit lids together. Disabling brims and adding 0.15mm compensation keeps bottom tolerances crisp.",
                    "manual_path": "Quality -> Precision -> Elephant foot compensation  |  Others -> Brim type",
                    "diffs": r7_diffs, "patches": r7_patches,
                    "impact": {"time_pct": 0.0, "time_min": -1.5, "filament_pct": 0.0, "filament_g": -2.0, "label": "-1–2 min | -1–3g filament"},
                    "override_warning": override_note(r7_patches.keys()),
                })
            else:
                good.append("Print-in-Place Clearances: Brim is OFF and Elephant's Foot compensation is active.")

        if trusted_mesh and trusted_mesh.get("total_bridge_mm2", 0.0) >= 40.0:
            guardrails.append(
                f"Floating horizontal bridges detected ({trusted_mesh['total_bridge_mm2']} mm²) — check the Preview tab to confirm whether supports are needed."
            )

        return {"tips": tips, "good": good, "guardrails": guardrails}


# ============================================================================
# 7. PATCHERS (SAFE LIVE PROCESS APPLY/REVERT + NON-DESTRUCTIVE .3MF EXPORT)
# ============================================================================

class LivePlatePatcher:
    """
    Applies (or reverts) zero-risk process settings on the active OrcaSlicer process preset
    via orca.host.preset.process.begin_update(). Edits stay unsaved in memory so saved
    system/user profiles on disk are never overwritten.
    """

    @staticmethod
    def coerce_native(key, val):
        s = str(val).strip()
        if key in BOOL_KEYS:
            return s in ("1", "true", "True")
        if key in INT_KEYS:
            return int(round(float(s)))
        if key in FLOAT_KEYS and not s.endswith("%"):
            return float(s)
        return s

    @staticmethod
    def apply(ordered_patches):
        if not ordered_patches:
            return {"applied": 0, "target_name": ""}

        tx = orca.host.preset.process.begin_update()
        for key, raw_val in ordered_patches.items():
            native_val = LivePlatePatcher.coerce_native(key, raw_val)
            try:
                tx.set(key, native_val)
            except (TypeError, ValueError):
                try:
                    tx.set(key, str(raw_val))
                except Exception as e:
                    raise ValueError(f"{key}={raw_val!r} -> {e}") from e
            except Exception as e:
                raise ValueError(f"{key}={raw_val!r} -> {e}") from e

        result = tx.commit()
        applied_keys = safe_get(result, "keys", None)
        return {
            "applied": len(applied_keys) if applied_keys is not None else len(ordered_patches),
            "target_name": clean_label(safe_get(result, "target_name", "") or ""),
        }


class ThreeMFPatcher:
    """Patches ONLY selected process keys into a copy of the .3mf and strips stale G-code caches."""

    @staticmethod
    def _is_stale_slice_cache(filename):
        low = filename.lstrip("/").lower()
        return low == "metadata/slice_info.config" or (
            low.startswith("metadata/plate_") and (low.endswith(".gcode") or low.endswith(".gcode.md5"))
        )

    @staticmethod
    def patch(raw_3mf_bytes, ordered_patches):
        in_buf, out_buf = io.BytesIO(raw_3mf_bytes), io.BytesIO()
        with zipfile.ZipFile(in_buf, "r") as zin:
            target_entry = next(
                (n for n in zin.namelist() if n.lstrip("/").lower() == "metadata/project_settings.config"),
                None,
            )
            if not target_entry:
                raise ValueError("This .3mf has no Metadata/project_settings.config (raw CAD file). Save it in OrcaSlicer first.")

            cfg = json.loads(zin.read(target_entry).decode("utf-8", errors="replace"))
            for key, val in ordered_patches.items():
                if key in cfg and isinstance(cfg[key], list):
                    cfg[key] = [val] + cfg[key][1:] if len(cfg[key]) > 1 else [val]
                else:
                    cfg[key] = val

            if isinstance(cfg.get("different_settings_to_system"), list) and cfg["different_settings_to_system"]:
                existing = [k for k in str(cfg["different_settings_to_system"][0]).split(";") if k]
                for key in ordered_patches:
                    if key not in existing:
                        existing.append(key)
                cfg["different_settings_to_system"][0] = ";".join(existing)

            patched_bytes = json.dumps(cfg, indent=4, ensure_ascii=False).encode("utf-8")
            with zipfile.ZipFile(out_buf, "w", compression=zipfile.ZIP_DEFLATED) as zout:
                for info in zin.infolist():
                    if ThreeMFPatcher._is_stale_slice_cache(info.filename):
                        continue
                    zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                    zi.compress_type = zipfile.ZIP_DEFLATED
                    if info.is_dir():
                        zout.writestr(zi, b"")
                    elif info.filename == target_entry:
                        zout.writestr(zi, patched_bytes)
                    else:
                        zout.writestr(zi, zin.read(info.filename))

        return out_buf.getvalue()


# ============================================================================
# 8. EMBEDDED PAGES UI (HTML / CSS / JS)
# ============================================================================

APP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<style>
  :root {
    --bg:#1e2227; --panel:#282d35; --panel-dark:#21262d; --border:#3a414d;
    --text:#e6edf3; --muted:#9da7b3; --teal:#2dd4bf; --teal-bg:#1b3636;
    --amber:#fbbf24; --amber-bg:#3b2f1a; --green:#4ade80; --green-bg:#1a3327;
    --blue:#60a5fa; --blue-bg:#1d2d44; --danger:#f87171;
  }
  * { box-sizing:border-box; }
  body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; background:var(--bg); color:var(--text); margin:0; padding:20px 24px 40px; line-height:1.45; }
  .wrap { max-width:920px; margin:0 auto; }
  .topbar { display:flex; align-items:center; justify-content:space-between; margin-bottom:16px; flex-wrap:wrap; gap:12px; }
  .brand h1 { margin:0; font-size:20px; color:var(--teal); }
  .brand p { margin:2px 0 0; font-size:12px; color:var(--muted); }
  .tabs { display:flex; background:var(--panel-dark); border:1px solid var(--border); border-radius:8px; padding:3px; gap:4px; }
  .tab-btn { background:transparent; color:var(--muted); border:none; padding:7px 14px; border-radius:6px; font-weight:600; font-size:13px; cursor:pointer; }
  .tab-btn.active { background:var(--teal); color:#10161b; }
  .card { background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:16px 18px; margin-bottom:14px; }
  .section-title { font-size:13px; font-weight:700; text-transform:uppercase; letter-spacing:0.6px; color:var(--muted); margin:0 0 10px; }
  .ctx-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; }
  .mesh-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; }
  .stat { background:var(--panel-dark); border:1px solid #303640; border-radius:6px; padding:9px 11px; font-size:12px; }
  .stat span { display:block; font-size:10px; color:var(--muted); text-transform:uppercase; margin-bottom:3px; }
  .stat strong { color:var(--text); font-size:13px; word-break:break-word; }
  .stat.warn { border-color:#d97706; }
  .stat.warn strong { color:var(--amber); }
  .stat.ok { border-color:#2b4a39; }
  .stat.ok strong { color:var(--green); }
  .input-row { display:grid; grid-template-columns:1fr auto; gap:12px; align-items:stretch; }
  .dropzone { border:2px dashed var(--teal); border-radius:8px; padding:16px; text-align:center; cursor:pointer; background:var(--panel-dark); }
  .dropzone:hover, .dropzone.drag { background:#243238; }
  .dropzone strong { display:block; font-size:14px; }
  .dropzone small { color:var(--muted); font-size:12px; }
  .btn { background:var(--teal); color:#0e1418; border:none; border-radius:7px; padding:10px 16px; font-weight:700; font-size:13px; cursor:pointer; display:inline-flex; align-items:center; justify-content:center; gap:6px; }
  .btn:hover { filter:brightness(1.06); }
  .btn:disabled { opacity:0.45; cursor:not-allowed; }
  .btn-secondary { background:#333a45; color:var(--text); border:1px solid var(--border); }
  .btn-done { background:#1a3327; color:var(--green); border:1px solid #2b6645; padding:5px 10px; font-size:11px; }
  .btn-done:hover { background:#224232; }
  .btn-undo { background:transparent; color:var(--blue); border:1px solid #2d4a6e; border-radius:4px; padding:1px 7px; font-size:11px; cursor:pointer; margin-left:8px; }
  .btn-undo:hover { background:#1d2d44; }
  .btn-revert { background:#332629; color:#fca5a5; border:1px solid #7f3f48; padding:6px 12px; font-size:12px; }
  .btn-revert:hover { background:#442b30; }
  .btn-sm { padding:6px 11px; font-size:12px; }
  .intent-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; }
  .intent-card { background:var(--panel-dark); border:2px solid #303640; border-radius:8px; padding:10px 12px; cursor:pointer; text-align:left; color:var(--text); }
  .intent-card:hover { border-color:#4b5563; }
  .intent-card.active { border-color:var(--teal); background:#1d3033; }
  .intent-card strong { display:block; font-size:13px; margin-bottom:3px; }
  .intent-card span { display:block; font-size:11px; color:var(--muted); }
  .toggle-row { display:flex; align-items:center; justify-content:space-between; margin-top:12px; padding-top:12px; border-top:1px solid #323842; gap:12px; }
  .switch { position:relative; display:inline-block; width:42px; height:22px; flex-shrink:0; }
  .switch input { opacity:0; width:0; height:0; }
  .slider { position:absolute; cursor:pointer; inset:0; background:#3f4754; border-radius:22px; transition:.2s; }
  .slider:before { position:absolute; content:""; height:16px; width:16px; left:3px; bottom:3px; background:#fff; border-radius:50%; transition:.2s; }
  input:checked + .slider { background:var(--teal); }
  input:checked + .slider:before { transform:translateX(20px); }
  .mode-bar { display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap; background:#1b2c2e; border:1px solid #2b5452; border-radius:8px; padding:10px 14px; margin-bottom:12px; font-size:12px; }
  .mode-bar.live-plate { background:#1c2633; border-color:#3b5575; }
  .mode-bar.empty-plate { background:#2b271c; border-color:#6b5424; }
  .mode-bar strong { color:var(--teal); }
  .mode-bar.live-plate strong { color:var(--blue); }
  .mode-bar.empty-plate strong { color:var(--amber); }
  .empty-card { background:var(--panel); border:1px dashed #6b5424; border-radius:10px; padding:24px 20px; text-align:center; margin-bottom:14px; }
  .empty-card h3 { margin:0 0 8px; color:var(--amber); font-size:16px; }
  .empty-card p { margin:0 auto; max-width:560px; font-size:13px; color:#cbd5e1; }
  .badge-strip { display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }
  .badge { font-size:11px; font-weight:600; padding:3px 8px; border-radius:5px; background:var(--teal-bg); color:var(--teal); }
  .badge.amber { background:var(--amber-bg); color:var(--amber); }
  .badge.green { background:var(--green-bg); color:var(--green); }
  .badge.blue { background:var(--blue-bg); color:var(--blue); }
  .guardrail-box { background:#2b271c; border:1px solid #6b5424; color:#fde68a; border-radius:7px; padding:9px 12px; font-size:12px; margin-top:10px; }
  .guardrail-box div + div { margin-top:5px; }
  .impact-bar { background:#1b282b; border:1px solid #2b5452; border-radius:8px; padding:12px 14px; margin-bottom:14px; display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px; }
  .impact-main { font-size:13px; }
  .impact-main strong { color:var(--teal); }
  .impact-note { font-size:11px; color:var(--muted); width:100%; }
  .bucket-header { font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:0.6px; margin:14px 0 8px; }
  .bucket-header.safety { color:var(--amber); }
  .bucket-header.recommended { color:var(--teal); }
  .tip-card { background:var(--panel); border:1px solid var(--border); border-left:4px solid var(--teal); border-radius:8px; padding:13px 15px; margin-bottom:10px; }
  .tip-card.safety { border-left-color:var(--amber); }
  .tip-card.manual { border-left-color:var(--blue); }
  .tip-card.unchecked { opacity:0.68; }
  .tip-top { display:flex; align-items:flex-start; gap:10px; }
  .tip-top input[type="checkbox"] { width:17px; height:17px; margin-top:2px; accent-color:var(--teal); cursor:pointer; }
  .manual-tag { font-size:10px; font-weight:700; padding:3px 6px; border-radius:4px; background:var(--blue-bg); color:var(--blue); text-transform:uppercase; margin-top:2px; white-space:nowrap; }
  .tip-body { flex:1; }
  .tip-title-row { display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:6px; }
  .tip-title { font-weight:700; font-size:14px; }
  .impact-pill { font-size:11px; font-weight:600; background:var(--panel-dark); border:1px solid var(--border); border-radius:12px; padding:2px 8px; color:#c9d1d9; }
  .tip-detected { font-size:12px; color:#cbd5e1; margin:4px 0 8px; }
  .diff-list { background:var(--panel-dark); border:1px solid #303640; border-radius:6px; padding:8px 10px; margin-bottom:8px; font-size:12px; }
  .diff-row { display:flex; justify-content:space-between; gap:12px; padding:2px 0; flex-wrap:wrap; }
  .diff-label { color:var(--muted); }
  .diff-vals .old { color:#f87171; text-decoration:line-through; margin-right:6px; }
  .diff-vals .old.plain { text-decoration:none; color:var(--muted); }
  .diff-vals .new { color:var(--green); font-weight:600; }
  .tip-why { font-size:12px; color:#b6c2cf; margin-bottom:6px; }
  .tip-manual { font-size:11px; color:var(--muted); font-family:monospace; }
  .tip-override { margin-top:6px; font-size:11px; color:var(--amber); background:var(--amber-bg); padding:4px 8px; border-radius:4px; display:inline-block; }

  .manual-steps-box {
    margin-top:8px;
    background:#1b2432;
    border:1px solid #2d415c;
    border-radius:6px;
    padding:9px 12px;
    font-size:12px;
  }
  .manual-steps-title { font-size:11px; font-weight:700; text-transform:uppercase; color:var(--blue); margin-bottom:5px; }
  .manual-steps-box ol { margin:0 0 0 18px; padding:0; color:#dbeafe; }
  .manual-steps-box li { margin-bottom:4px; }
  .manual-steps-box li:last-child { margin-bottom:0; }
  .manual-footer {
    margin-top:9px;
    display:flex;
    align-items:center;
    justify-content:space-between;
    flex-wrap:wrap;
    gap:8px;
  }

  details.applied-box { background:var(--panel); border:1px solid #2b5452; border-radius:8px; padding:10px 14px; margin-top:12px; }
  details.applied-box summary { cursor:pointer; font-weight:700; font-size:13px; color:var(--teal); }
  details.applied-box ul { margin:8px 0 6px 18px; padding:0; font-size:12px; color:#d2f6f2; }
  details.applied-box li { margin-bottom:6px; }
  .applied-diffs { display:block; font-size:11px; color:var(--muted); margin-top:2px; font-family:monospace; }
  .applied-footer {
    margin-top:10px;
    padding-top:9px;
    border-top:1px solid #2e3d42;
    display:flex;
    align-items:center;
    justify-content:space-between;
    flex-wrap:wrap;
    gap:8px;
    font-size:11px;
    color:var(--muted);
  }

  details.good-box { background:var(--panel); border:1px solid #2b4a39; border-radius:8px; padding:10px 14px; margin-top:12px; }
  details.good-box summary { cursor:pointer; font-weight:700; font-size:13px; color:var(--green); }
  details.good-box ul { margin:8px 0 2px 18px; padding:0; font-size:12px; color:#c7e6d2; }
  details.good-box li { margin-bottom:5px; }
  details.items-box { margin-top:12px; background:var(--panel-dark); border:1px solid #303640; border-radius:7px; padding:8px 12px; }
  details.items-box summary { cursor:pointer; font-weight:600; font-size:12px; color:var(--teal); }
  .items-table { width:100%; border-collapse:collapse; margin-top:8px; font-size:12px; }
  .items-table th, .items-table td { text-align:left; padding:6px 8px; border-bottom:1px solid #2f3540; }
  .items-table th { color:var(--muted); font-size:11px; text-transform:uppercase; }
  .items-table tr:last-child td { border-bottom:none; }
  .footer-bar { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px; margin:16px 0; padding-top:14px; border-top:1px solid var(--border); }
  #statusMsg { font-size:12px; color:var(--muted); max-width:520px; }
  #statusMsg.ok { color:var(--green); }
  #statusMsg.err { color:var(--danger); }
  .learn-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  .learn-box { background:var(--panel-dark); border:1px solid #303640; border-radius:8px; padding:12px 14px; }
  .learn-box h3 { margin:0 0 8px; font-size:14px; }
  .learn-box ul { margin:0 0 0 18px; padding:0; font-size:12px; color:#cbd5e1; }
  .learn-box li { margin-bottom:5px; }
  .rule-ref { margin-bottom:10px; padding-bottom:10px; border-bottom:1px solid #303640; font-size:12px; }
  .rule-ref:last-child { border-bottom:none; margin-bottom:0; padding-bottom:0; }
  .rule-ref strong { color:var(--teal); font-size:13px; display:block; margin-bottom:2px; }
  .hidden { display:none !important; }
  @media (max-width:720px) { .ctx-grid,.intent-grid,.learn-grid,.input-row,.mesh-grid { grid-template-columns:1fr 1fr; } }
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div class="brand">
      <h1>Print Advisor</h1>
      <p>Educational, zero-risk 3D print analysis — never guesses without real geometry</p>
    </div>
    <div class="tabs">
      <button class="tab-btn active" id="tabBtnAdvisor" onclick="switchTab('advisor')">Advisor</button>
      <button class="tab-btn" id="tabBtnLearn" onclick="switchTab('learn')">Learn &amp; Safety</button>
    </div>
  </div>

  <!-- TAB 1: ADVISOR -->
  <div id="tabAdvisor">
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;flex-wrap:wrap;gap:8px;">
        <div style="display:flex;align-items:center;gap:8px;">
          <div class="section-title" style="margin:0;">Active Context</div>
          <span class="badge blue" id="ctxSource">Active OrcaSlicer Project</span>
        </div>
        <button class="btn btn-secondary btn-sm" onclick="refreshPresets()">Refresh Active Presets</button>
      </div>
      <div class="ctx-grid">
        <div class="stat"><span>Printer</span><strong id="ctxPrinter">Loading...</strong></div>
        <div class="stat"><span>Nozzle</span><strong id="ctxNozzle">...</strong></div>
        <div class="stat"><span>Primary Filament</span><strong id="ctxFilament">...</strong></div>
        <div class="stat"><span>Process</span><strong id="ctxProcess">...</strong></div>
      </div>
    </div>

    <div class="card">
      <div class="section-title">1. Choose What to Analyze</div>
      <div class="input-row">
        <div class="dropzone" id="dropzone" onclick="document.getElementById('fileInput').click()">
          <strong id="dropTitle">Click or Drop a .3mf File Here</strong>
          <small id="dropSub">Scans .3mf geometry + embedded settings and exports a non-destructive _advised.3mf</small>
          <input type="file" id="fileInput" accept=".3mf" style="display:none" />
        </div>
        <button class="btn btn-secondary" onclick="scanLivePlate()">
          Scan Active<br>Plate Project
        </button>
      </div>
      <div id="meshBadges" class="badge-strip hidden"></div>
      <div id="guardrailBox" class="guardrail-box hidden"></div>
    </div>

    <div class="card">
      <div class="section-title">2. What Are You Printing?</div>
      <div class="intent-grid">
        <button class="intent-card active" data-intent="engineering" onclick="setIntent('engineering')">
          <strong>Engineering / Functional</strong>
          <span>Brackets, mounts, hooks — reinforces shell walls</span>
        </button>
        <button class="intent-card" data-intent="figure" onclick="setIntent('figure')">
          <strong>Figure / Display</strong>
          <span>Statues &amp; curves — finer layers &amp; clean seams</span>
        </button>
        <button class="intent-card" data-intent="prototype" onclick="setIntent('prototype')">
          <strong>Fast Prototype</strong>
          <span>Test fits — safety checks + opt-in speed trim</span>
        </button>
        <button class="intent-card" data-intent="sign" onclick="setIntent('sign')">
          <strong>Flat Sign / Multi-Color</strong>
          <span>Coasters &amp; text — Arachne walls &amp; opt-in ironing</span>
        </button>
      </div>

      <div class="toggle-row">
        <div>
          <strong style="font-size:13px;">Has moving parts or tight snap-fits? (Print-in-Place)</strong>
          <div style="font-size:11px;color:var(--muted);">
            Suppresses brims and adds light Elephant's Foot compensation so hinges and gears don't weld shut.
          </div>
        </div>
        <label class="switch">
          <input type="checkbox" id="pipToggle" onchange="reEvaluate()" />
          <span class="slider"></span>
        </label>
      </div>
    </div>

    <div id="resultsWrap" class="hidden">
      <div id="modeBar" class="mode-bar"></div>

      <div id="emptyStateBox" class="empty-card hidden">
        <h3>Nothing on the Active Plate to Analyze</h3>
        <p id="emptyStateMsg">
          No 3D model was found on your active OrcaSlicer plate. Add a model in the Prepare tab (or drop a .3mf file above) so Print Advisor can scan real geometry before making recommendations.
        </p>
      </div>

      <div id="analysisContent">
        <div class="impact-bar" id="impactBar"></div>
        <div id="tipsContainer"></div>

        <!-- Applied Fixes collapsible + 1-Click Revert (Live Plate mode, right above Already Good) -->
        <details class="applied-box hidden" id="appliedBox">
          <summary id="appliedSummary">Applied to Active Plate (0)</summary>
          <ul id="appliedList"></ul>
          <div class="applied-footer hidden" id="revertRow">
            <span>Edits are unsaved in memory — your saved profiles on disk were not modified.</span>
            <button class="btn btn-revert" onclick="revertLiveChanges()">Revert Applied Changes</button>
          </div>
        </details>

        <details class="good-box hidden" id="goodBox">
          <summary id="goodSummary">Already Good (0)</summary>
          <ul id="goodList"></ul>
        </details>

        <div class="footer-bar">
          <div id="statusMsg">Ready.</div>
          <div style="display:flex;gap:8px;flex-wrap:wrap;">
            <button class="btn btn-secondary" onclick="copySummary()">Copy Summary</button>
            <button class="btn" id="primaryActionBtn" onclick="applyOrExport()">Apply Selected</button>
          </div>
        </div>

        <div class="card hidden" id="meshDetailsCard">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:8px;">
            <div class="section-title" style="margin:0;">Mesh &amp; Model Inspector</div>
            <span style="font-size:11px;color:var(--muted);" id="meshInspectorSub"></span>
          </div>
          <div class="mesh-grid" id="meshDetailsGrid"></div>
          <details class="items-box hidden" id="itemsBreakdownBox">
            <summary id="itemsBreakdownSummary">Per-Object Breakdown</summary>
            <table class="items-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Size (X &times; Y &times; Z)</th>
                  <th>Bed Area</th>
                  <th>Bottom Fillets</th>
                  <th>Overhangs</th>
                  <th>Tipping Ratio</th>
                  <th>VLH</th>
                </tr>
              </thead>
              <tbody id="itemsBreakdownBody"></tbody>
            </table>
          </details>
        </div>
      </div>
    </div>
  </div>

  <!-- TAB 2: LEARN & SAFETY -->
  <div id="tabLearn" class="hidden">
    <div class="card">
      <div class="section-title">Our "Do No Harm" Philosophy</div>
      <div class="learn-grid">
        <div class="learn-box">
          <h3 style="color:var(--green);">What Print Advisor WILL Patch</h3>
          <ul>
            <li>Slowing down steep overhangs &amp; bottom fillets + Inner/Outer wall order.</li>
            <li>Switching from self-intersecting <strong>Grid</strong> infill to <strong>Gyroid</strong>.</li>
            <li>Intent upgrades: ~1.2mm+ walls (Engineering), finer layers (Figures), Arachne walls (Signs), or opt-in draft trims (Prototypes).</li>
            <li>Outer brims for tall/narrow parts or ABS/ASA warp protection (unless Print-in-Place is ON).</li>
            <li>PETG / TPU <strong>Avoid crossing walls</strong> and safe Layer 1 extrusion speeds.</li>
            <li>Live-plate edits stay unsaved in memory and include <strong>1-Click Revert</strong>.</li>
          </ul>
        </div>
        <div class="learn-box">
          <h3 style="color:var(--danger);">What We NEVER Auto-Modify</h3>
          <ul>
            <li>Nozzle or build plate temperatures</li>
            <li>Retraction distance, speed, flow ratio, Pressure Advance, or Z-offset</li>
            <li>Filament cooling fan curves (shown as read-only guidance if too low)</li>
            <li>Supports, scarf seams, or Variable Layer Height (shown as step-by-step manual guides so you can verify them visually in Preview)</li>
          </ul>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="section-title">Core Rules &amp; Built-In Safety Guardrails</div>
      <div class="rule-ref"><strong>0. Geometry-First Rule (No Blind Tips)</strong>Print Advisor refuses to suggest settings on an empty plate. Every tip requires real 3D geometry—either live from your active OrcaSlicer plate or from a dropped <code>.3mf</code> file.</div>
      <div class="rule-ref"><strong>1. Steep Overhang &amp; Bottom Fillet Guard</strong>Detects 55°–85° downward faces (especially in the bottom 5mm). Slows overhang speeds, enforces Inner/Outer wall order, and provides a step-by-step Variable Layer Height guide that auto-clears once custom layering is detected on the plate or in the <code>.3mf</code>.</div>
      <div class="rule-ref"><strong>2. Nozzle Collision Prevention (Grid -&gt; Gyroid)</strong>Grid infill crosses over itself on the same layer, creating bumps the nozzle can strike. Gyroid never crosses itself.</div>
      <div class="rule-ref"><strong>3. Intent-Tailored Optimization</strong>Engineering: nozzle-aware shell walls (&gt;=1.2mm). Figure: finer layers + manual Scarf seam / Tree support guide. Prototype: opt-in wall/infill trim. Sign: Arachne walls + opt-in ironing.</div>
      <div class="rule-ref"><strong>4. Tipping &amp; ABS/ASA Warp Guard</strong>Adds a 5mm outer brim when geometry is tall/narrow or printing high-shrinkage ABS/ASA (suppressed if Print-in-Place is ON).</div>
      <div class="rule-ref"><strong>5. PETG &amp; TPU Stringing Guard</strong>Enables <em>Avoid crossing walls</em> so sticky or flexible filaments stay inside the part during travel.</div>
      <div class="rule-ref"><strong>6. First-Layer Bond (CoreXY-Safe)</strong>Flags aggressive Layer 1 extrusion speeds without penalizing fast modern travel speeds.</div>
      <div class="rule-ref"><strong>7. Print-in-Place &amp; Safety Checks</strong>Disables brims + adds 0.15mm Elephant's Foot compensation for moving parts; protects Spiral Vase mode; checks non-manifold/inverted meshes, per-object overrides, printer mismatches, and strips stale G-code caches on export.</div>
    </div>
  </div>
</div>

<script>
  const MAX_WARN_MB = 30;
  const MAX_BLOCK_MB = 60;

  let state = {
    mode: "none",          // "none" | "3mf" | "live"
    intent: "engineering",
    canExport3mf: false,
    canRevert: false,
    filename: "",
    mesh: null,
    tips: [],
    appliedFixes: [],
    good: [],
    guardrails: [],
    selectedIds: new Set(),
    doneManualIds: new Set(), // Tracks manual cards checked off with 'Mark Done' for current scan
  };

  function esc(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function setStatus(text, tone = "") {
    const el = document.getElementById("statusMsg");
    el.textContent = text;
    el.className = tone;
  }

  function switchTab(tab) {
    const isAdv = tab === "advisor";
    document.getElementById("tabAdvisor").classList.toggle("hidden", !isAdv);
    document.getElementById("tabLearn").classList.toggle("hidden", isAdv);
    document.getElementById("tabBtnAdvisor").classList.toggle("active", isAdv);
    document.getElementById("tabBtnLearn").classList.toggle("active", !isAdv);
  }

  function requestContext() {
    window.orca.postMessage({ type: "get_context" });
  }

  function refreshPresets() {
    if (state.mode === "3mf" || state.mode === "live") reEvaluate();
    else requestContext();
  }

  function resetResultsUI(hideWrap, clearDoneManual = false) {
    state.canExport3mf = false;
    state.canRevert = false;
    state.mesh = null;
    state.tips = [];
    state.appliedFixes = [];
    state.good = [];
    state.guardrails = [];
    state.selectedIds = new Set();
    if (clearDoneManual) state.doneManualIds = new Set();

    document.getElementById("meshBadges").innerHTML = "";
    document.getElementById("meshBadges").classList.add("hidden");
    document.getElementById("guardrailBox").innerHTML = "";
    document.getElementById("guardrailBox").classList.add("hidden");
    document.getElementById("tipsContainer").innerHTML = "";
    document.getElementById("appliedList").innerHTML = "";
    document.getElementById("appliedBox").classList.add("hidden");
    document.getElementById("appliedBox").open = false;
    document.getElementById("revertRow").classList.add("hidden");
    document.getElementById("goodList").innerHTML = "";
    document.getElementById("goodBox").classList.add("hidden");
    document.getElementById("meshDetailsGrid").innerHTML = "";
    document.getElementById("meshDetailsCard").classList.add("hidden");
    document.getElementById("itemsBreakdownBox").classList.add("hidden");
    document.getElementById("emptyStateBox").classList.add("hidden");
    document.getElementById("analysisContent").classList.remove("hidden");

    if (hideWrap) document.getElementById("resultsWrap").classList.add("hidden");
  }

  function clearAll() {
    state.mode = "none";
    state.filename = "";
    document.getElementById("fileInput").value = "";
    document.getElementById("dropTitle").textContent = "Click or Drop a .3mf File Here";
    document.getElementById("dropSub").textContent =
      "Scans .3mf geometry + embedded settings and exports a non-destructive _advised.3mf";
    resetResultsUI(true, true);
    window.orca.postMessage({ type: "clear_3mf" });
    requestContext();
  }

  function setIntent(intent) {
    state.intent = intent;
    document.querySelectorAll(".intent-card").forEach((el) => {
      el.classList.toggle("active", el.dataset.intent === intent);
    });
    if (state.mode === "3mf" || state.mode === "live") reEvaluate();
  }

  function reEvaluate() {
    if (state.mode === "live") {
      window.orca.postMessage({
        type: "scan_live_plate",
        intent: state.intent,
        print_in_place: document.getElementById("pipToggle").checked,
      });
    } else if (state.mode === "3mf") {
      window.orca.postMessage({
        type: "evaluate_3mf",
        intent: state.intent,
        print_in_place: document.getElementById("pipToggle").checked,
      });
    }
  }

  // Explicit scan / re-scan clears manual 'Mark Done' state so swapping models never hides new warnings
  function scanLivePlate() {
    state.mode = "live";
    state.filename = "";
    document.getElementById("fileInput").value = "";
    document.getElementById("dropTitle").textContent = "Click or Drop a .3mf File Here";
    document.getElementById("dropSub").textContent =
      "Switched to Active OrcaSlicer Plate — drop a .3mf anytime for offline file export";
    resetResultsUI(false, true);
    setStatus("Scanning live 3D geometry on your active plate...");
    window.orca.postMessage({
      type: "scan_live_plate",
      intent: state.intent,
      print_in_place: document.getElementById("pipToggle").checked,
    });
  }

  function revertLiveChanges() {
    if (state.mode !== "live" || !state.canRevert) return;
    setStatus("Reverting applied settings back to your original values...");
    window.orca.postMessage({
      type: "revert_live",
      intent: state.intent,
      print_in_place: document.getElementById("pipToggle").checked,
    });
  }

  function markManualDone(tipId) {
    state.doneManualIds.add(tipId);
    renderResults(false);
    setStatus("Manual step marked done (moved to Already Good — click Undo inside Already Good to restore).", "ok");
  }

  function undoManualDone(tipId) {
    state.doneManualIds.delete(tipId);
    renderResults(false);
    setStatus("Manual step restored.");
  }

  function formatDuration(totalSec) {
    const sec = Math.max(60, Math.round(totalSec));
    const h = Math.floor(sec / 3600);
    const m = Math.round((sec % 3600) / 60);
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
  }

  function updateContextStrip(meta) {
    document.getElementById("ctxPrinter").textContent = meta.printer;
    document.getElementById("ctxNozzle").textContent = meta.nozzle + " mm";
    document.getElementById("ctxFilament").textContent = `${meta.filament} (${meta.filament_type})`;
    document.getElementById("ctxProcess").textContent = meta.process;
    if (meta.source_label) document.getElementById("ctxSource").textContent = meta.source_label;
  }

  // Shared geometry protection status used by BOTH top #meshBadges and bottom Mesh & Model Inspector
  function getGeometryStatus(m) {
    const pendingIds = new Set(
      state.tips
        .filter((t) => !(t.manual_only && state.doneManualIds.has(t.id)))
        .map((t) => t.id)
    );
    const overhangPending = pendingIds.has("steep_overhang");
    const filletPending = overhangPending || pendingIds.has("bottom_fillet_manual");
    const brimPending = pendingIds.has("brim_guard");

    const hasFillet = !m.inverted_normals && (m.worst_bottom_fillet_pct >= 3.0 || m.total_bottom_fillet_mm2 >= 25.0);
    const hasOverhang = !m.inverted_normals && (m.worst_overhang_pct >= 5.0 || m.total_steep_mm2 >= 50.0);
    const isTippy = !m.inverted_normals && (
      m.worst_tipping_ratio >= 2.8 || (m.min_bed_area_mm2 < 250.0 && m.max_size_mm[2] >= 40.0)
    );
    const bridgeWarn = !m.inverted_normals && m.total_bridge_mm2 >= 40.0;
    const pipOn = document.getElementById("pipToggle").checked;

    return { overhangPending, filletPending, brimPending, hasFillet, hasOverhang, isTippy, bridgeWarn, pipOn };
  }

  function renderModeBar(isEmpty) {
    const bar = document.getElementById("modeBar");
    if (isEmpty) {
      bar.className = "mode-bar empty-plate";
      bar.innerHTML = `
        <div><strong>Active OrcaSlicer Plate &mdash; Empty</strong> (No 3D models detected)</div>
        <button class="btn btn-secondary btn-sm" onclick="scanLivePlate()">Re-Scan Plate</button>
      `;
    } else if (state.mode === "3mf") {
      bar.className = "mode-bar";
      bar.innerHTML = `
        <div><strong>Mode: Loaded .3mf File</strong> &mdash; ${esc(state.filename)} (3D Geometry + Embedded Settings)</div>
        <button class="btn btn-secondary btn-sm" onclick="clearAll()">Clear .3mf</button>
      `;
    } else {
      bar.className = "mode-bar live-plate";
      bar.innerHTML = `
        <div><strong>Mode: Active OrcaSlicer Plate</strong> &mdash; Live 3D Geometry + Active Process Preset</div>
        <button class="btn btn-secondary btn-sm" onclick="scanLivePlate()">Re-Scan Plate</button>
      `;
    }
  }

  function renderMeshInspector() {
    const card = document.getElementById("meshDetailsCard");
    if (!state.mesh) {
      card.classList.add("hidden");
      return;
    }
    const m = state.mesh;
    const g = getGeometryStatus(m);
    card.classList.remove("hidden");

    const srcLabel = state.mode === "3mf" ? state.filename : "Live OrcaSlicer Plate";
    document.getElementById("meshInspectorSub").textContent =
      `${srcLabel} • ${m.total_triangles.toLocaleString()} triangles (sampled ${m.sampled_triangles.toLocaleString()})`;

    const filletCls = g.hasFillet ? (g.filletPending ? "warn" : "ok") : "";
    const filletTag = g.hasFillet ? (g.filletPending ? " · Needs Fix" : " · Protected") : "";

    const overhangCls = g.hasOverhang ? (g.overhangPending ? "warn" : "ok") : "";
    const overhangTag = g.hasOverhang ? (g.overhangPending ? " · Needs Fix" : " · Protected") : "";

    let tipCls = "ok";
    let tipTag = "(Stable)";
    if (g.isTippy) {
      if (g.pipOn) {
        tipCls = "";
        tipTag = "(Tall · Brim Suppressed by PiP)";
      } else if (g.brimPending) {
        tipCls = "warn";
        tipTag = "(Tall/Narrow · Needs Brim)";
      } else {
        tipCls = "ok";
        tipTag = "(Tall · Brim Active)";
      }
    }

    const baselineStr = m.slice_info && m.slice_info.has_slice_info
      ? `${m.slice_info.time_sec ? formatDuration(m.slice_info.time_sec) : "?"} / ${m.slice_info.weight_g ?? "?"} g`
      : (state.mode === "live" ? "Slice in OrcaSlicer" : "Not sliced yet");

    const scopeStr = m.plate_count !== null && m.plate_count !== undefined
      ? `${esc(m.object_count)} object(s) &middot; ${esc(m.plate_count)} plate(s)`
      : `${esc(m.object_count)} object(s) &middot; Active Project`;

    const overridesStr = m.per_object_overrides && m.per_object_overrides.length
      ? `${m.per_object_overrides.length} key(s)`
      : "None";

    document.getElementById("meshDetailsGrid").innerHTML = `
      <div class="stat">
        <span>Max Size (X &times; Y &times; Z)</span>
        <strong>${esc(m.max_size_mm[0])} &times; ${esc(m.max_size_mm[1])} &times; ${esc(m.max_size_mm[2])} mm</strong>
      </div>
      <div class="stat">
        <span>Bed Contact Area</span>
        <strong>${esc(m.total_bed_area_mm2)} mm&sup2; (min ${esc(m.min_bed_area_mm2)})</strong>
      </div>
      <div class="stat ${tipCls}">
        <span>Tipping Aspect Ratio (H / Foot)</span>
        <strong>${esc(m.worst_tipping_ratio)}x ${tipTag}</strong>
      </div>
      <div class="stat ${filletCls}">
        <span>Bottom-5mm Steep Fillets (55°–85°)</span>
        <strong>${esc(m.worst_bottom_fillet_pct)}% (${esc(m.total_bottom_fillet_mm2)} mm&sup2;)${filletTag}</strong>
      </div>
      <div class="stat ${overhangCls}">
        <span>Total Steep Overhangs (55°–85°)</span>
        <strong>${esc(m.worst_overhang_pct)}% (${esc(m.total_steep_mm2)} mm&sup2;)${overhangTag}</strong>
      </div>
      <div class="stat ${g.bridgeWarn ? "warn" : ""}">
        <span>Floating Bridges (&gt;85°)</span>
        <strong>${esc(m.total_bridge_mm2)} mm&sup2;</strong>
      </div>
      <div class="stat">
        <span>Baseline Slice Info</span>
        <strong>${esc(baselineStr)}</strong>
      </div>
      <div class="stat">
        <span>Objects / Scope</span>
        <strong>${scopeStr}</strong>
      </div>
      <div class="stat">
        <span>Model Metadata</span>
        <strong>Unit: ${esc(m.source_unit)} &middot; VLH: ${m.has_variable_layer_height ? "Active" : "None"} &middot; Modifiers: ${esc(m.skipped_modifiers)} &middot; Overrides: ${esc(overridesStr)}</strong>
      </div>
    `;

    const itemsBox = document.getElementById("itemsBreakdownBox");
    if (m.items && m.items.length > 0) {
      document.getElementById("itemsBreakdownSummary").textContent =
        `Per-Object Breakdown (${m.items.length} part${m.items.length > 1 ? "s" : ""}) — click to inspect`;
      document.getElementById("itemsBreakdownBody").innerHTML = m.items.map((it) => `
        <tr>
          <td>#${esc(it.index)}</td>
          <td>${esc(it.size_mm[0])} &times; ${esc(it.size_mm[1])} &times; ${esc(it.size_mm[2])} mm</td>
          <td>${esc(it.bed_area_mm2)} mm&sup2;</td>
          <td>${esc(it.bottom_fillet_pct)}% (${esc(it.bottom_fillet_mm2)} mm&sup2;)</td>
          <td>${esc(it.steep_overhang_pct)}% (${esc(it.steep_overhang_mm2)} mm&sup2;)</td>
          <td>${esc(it.tipping_ratio)}x</td>
          <td>${it.has_vlh ? "Active" : "Fixed"}</td>
        </tr>
      `).join("");
      itemsBox.classList.remove("hidden");
    } else {
      itemsBox.classList.add("hidden");
    }
  }

  function renderImpactBar() {
    const bar = document.getElementById("impactBar");
    const chosen = state.tips.filter((t) => !t.manual_only && state.selectedIds.has(t.id));

    let sumTimePct = 0, sumTimeMin = 0, sumFilPct = 0, sumFilG = 0;
    chosen.forEach((t) => {
      sumTimePct += t.impact.time_pct || 0;
      sumTimeMin += t.impact.time_min || 0;
      sumFilPct += t.impact.filament_pct || 0;
      sumFilG += t.impact.filament_g || 0;
    });

    const slice = state.mesh && state.mesh.slice_info ? state.mesh.slice_info : null;
    let html = "";

    if (chosen.length === 0) {
      html = `<div class="impact-main"><strong>0 safe fixes selected.</strong> Check any card below to preview its estimated impact.</div>`;
    } else if (slice && slice.has_slice_info && slice.time_sec) {
      const estSec = Math.max(60, slice.time_sec * (1 + sumTimePct / 100) + sumTimeMin * 60);
      const baseG = slice.weight_g || 0;
      const estG = baseG ? Math.max(0.1, baseG * (1 + sumFilPct / 100) + sumFilG) : null;
      const dPct = Math.round(((estSec - slice.time_sec) / slice.time_sec) * 100);
      const gText = estG !== null
        ? ` / ${baseG.toFixed(1)}g &rarr; <strong>~${estG.toFixed(1)}g</strong> (${estG - baseG >= 0 ? "+" : ""}${(estG - baseG).toFixed(1)}g)`
        : "";
      html = `<div class="impact-main"><strong>${chosen.length} fix${chosen.length > 1 ? "es" : ""} selected</strong> &nbsp;|&nbsp; Baseline: ${formatDuration(slice.time_sec)} &rarr; <strong>~${formatDuration(estSec)}</strong> (${dPct >= 0 ? "+" : ""}${dPct}%)${gText}</div>`;
    } else {
      const tParts = [], fParts = [];
      if (sumTimePct) tParts.push(`${sumTimePct > 0 ? "+" : ""}${Math.round(sumTimePct)}%`);
      if (sumTimeMin) tParts.push(`${sumTimeMin > 0 ? "+" : ""}${sumTimeMin} min`);
      if (sumFilPct) fParts.push(`${sumFilPct > 0 ? "+" : ""}${Math.round(sumFilPct)}%`);
      if (sumFilG) fParts.push(`${sumFilG > 0 ? "+" : ""}${sumFilG}g`);
      html = `<div class="impact-main"><strong>${chosen.length} fix${chosen.length > 1 ? "es" : ""} selected</strong> &nbsp;|&nbsp; Estimated Delta: Time <strong>${tParts.length ? tParts.join(" ") : "~0%"}</strong> &middot; Filament <strong>${fParts.length ? fParts.join(" ") : "+0g"}</strong></div>`;
    }

    html += `<div class="impact-note">Estimates are directional — slice in OrcaSlicer for exact totals.</div>`;
    bar.innerHTML = html;

    const btn = document.getElementById("primaryActionBtn");
    if (state.mode === "live") {
      if (chosen.length === 0) {
        btn.disabled = true;
        btn.title = "Select at least one safe fix to apply to your active plate";
        btn.textContent = "Select Fixes to Apply";
      } else {
        btn.disabled = false;
        btn.title = "Applies only your checked zero-risk fixes to the active process preset (unsaved — 1-click Revert available)";
        btn.textContent = `Apply Selected to Active Plate (${chosen.length})`;
      }
    } else if (state.mode === "3mf") {
      if (!state.canExport3mf) {
        btn.disabled = true;
        btn.title = "Raw CAD .3mf has no embedded slicer profile — save in OrcaSlicer first or apply manually";
        btn.textContent = "Raw CAD .3mf (Apply Manually)";
      } else if (chosen.length === 0) {
        btn.disabled = true;
        btn.title = "Select at least one fix to export";
        btn.textContent = "Select Fixes to Export";
      } else {
        btn.disabled = false;
        btn.title = "Saves a new non-destructive _advised.3mf with only your checked fixes";
        btn.textContent = `Download Optimized .3mf (${chosen.length})`;
      }
    } else {
      btn.disabled = true;
      btn.textContent = "No Model Loaded";
    }
  }

  function toggleTip(id, checked) {
    if (checked) state.selectedIds.add(id);
    else state.selectedIds.delete(id);
    const card = document.querySelector(`.tip-card[data-id="${id}"]`);
    if (card) card.classList.toggle("unchecked", !checked);
    renderImpactBar();
  }

  function renderTipCard(t) {
    const isManual = !!t.manual_only;
    const checked = !isManual && state.selectedIds.has(t.id);
    const diffsHtml = t.diffs.map((d) => `
      <div class="diff-row">
        <span class="diff-label">${esc(d.label)}</span>
        <span class="diff-vals"><span class="old ${isManual ? "plain" : ""}">${esc(d.before)}</span>&rarr; <span class="new">${esc(d.after)}</span></span>
      </div>
    `).join("");

    const stepsHtml = (isManual && t.manual_steps && t.manual_steps.length)
      ? `<div class="manual-steps-box">
          <div class="manual-steps-title">Step-by-Step in OrcaSlicer</div>
          <ol>${t.manual_steps.map((step) => `<li>${esc(step)}</li>`).join("")}</ol>
        </div>`
      : "";

    const ovHtml = t.override_warning ? `<div class="tip-override">&#9888; ${esc(t.override_warning)}</div>` : "";
    const safeId = esc(t.id);
    const leftCtrl = isManual
      ? `<span class="manual-tag">Manual</span>`
      : `<input type="checkbox" ${checked ? "checked" : ""} onchange="toggleTip('${safeId}', this.checked)" />`;

    const pathOrFooter = isManual
      ? `<div class="manual-footer">
           <div class="tip-manual">Manual path: ${esc(t.manual_path)}</div>
           <button class="btn btn-done" onclick="markManualDone('${safeId}')">&#10003; Mark Done</button>
         </div>`
      : `<div class="tip-manual">Manual path: ${esc(t.manual_path)}</div>`;

    return `
      <div class="tip-card ${isManual ? "manual" : `${esc(t.bucket)} ${checked ? "" : "unchecked"}`}" data-id="${safeId}">
        <div class="tip-top">
          ${leftCtrl}
          <div class="tip-body">
            <div class="tip-title-row">
              <span class="tip-title">${esc(t.title)}</span>
              <span class="impact-pill">${esc(t.impact.label)}</span>
            </div>
            <div class="tip-detected">${esc(t.detected)}</div>
            <div class="diff-list">${diffsHtml}</div>
            <div class="tip-why"><strong>Why:</strong> ${esc(t.why)}</div>
            ${stepsHtml}
            ${pathOrFooter}
            ${ovHtml}
          </div>
        </div>
      </div>
    `;
  }

  function renderEmptyPlate(reason) {
    document.getElementById("resultsWrap").classList.remove("hidden");
    renderModeBar(true);
    document.getElementById("emptyStateMsg").textContent =
      (reason || "No 3D model was found on your active OrcaSlicer plate.") +
      " Add a model in the Prepare tab (or drop a .3mf file above) so Print Advisor can scan real geometry before giving tips.";
    document.getElementById("emptyStateBox").classList.remove("hidden");
    document.getElementById("analysisContent").classList.add("hidden");
    document.getElementById("meshBadges").classList.add("hidden");
    document.getElementById("guardrailBox").classList.add("hidden");
  }

  function renderResults(autoOpenApplied = false) {
    document.getElementById("resultsWrap").classList.remove("hidden");
    document.getElementById("emptyStateBox").classList.add("hidden");
    document.getElementById("analysisContent").classList.remove("hidden");
    renderModeBar(false);

    const gBox = document.getElementById("guardrailBox");
    if (state.guardrails.length > 0) {
      gBox.innerHTML = state.guardrails.map((g) => `<div>&#9432; ${esc(g)}</div>`).join("");
      gBox.classList.remove("hidden");
    } else {
      gBox.classList.add("hidden");
    }

    const mBadges = document.getElementById("meshBadges");
    if (state.mesh) {
      const m = state.mesh;
      const g = getGeometryStatus(m);
      if (state.mode === "3mf") {
        document.getElementById("dropSub").textContent =
          `Loaded (${m.total_triangles.toLocaleString()} triangles) — click or drop another .3mf to replace`;
      }
      const plateBadge = (m.plate_count !== null && m.plate_count !== undefined)
        ? `<span class="badge ${m.plate_count > 1 ? "amber" : ""}">${esc(m.plate_count)} Plate${m.plate_count > 1 ? "s" : ""}</span>`
        : `<span class="badge blue">Active Project</span>`;

      const b = [
        `<span class="badge">${esc(m.max_size_mm[0])} &times; ${esc(m.max_size_mm[1])} &times; ${esc(m.max_size_mm[2])} mm</span>`,
        `<span class="badge">Objects: ${esc(m.object_count)}</span>`,
        plateBadge,
      ];
      if (m.has_variable_layer_height) {
        b.push(`<span class="badge green">Variable Layer Height: Active</span>`);
      }
      if (m.inverted_normals) {
        b.push(`<span class="badge amber">Inverted Normals Detected</span>`);
      } else {
        if (g.hasFillet) {
          b.push(g.filletPending
            ? `<span class="badge amber">Bottom Fillets: ${esc(m.worst_bottom_fillet_pct)}% (${esc(m.total_bottom_fillet_mm2)} mm&sup2;)</span>`
            : `<span class="badge green">Bottom Fillets: ${esc(m.worst_bottom_fillet_pct)}% &middot; Protected</span>`);
        }
        if (g.isTippy) {
          if (g.pipOn) {
            b.push(`<span class="badge blue">Tall/Narrow: ${esc(m.worst_tipping_ratio)}x (PiP)</span>`);
          } else if (g.brimPending) {
            b.push(`<span class="badge amber">Tall/Narrow: ${esc(m.worst_tipping_ratio)}x</span>`);
          } else {
            b.push(`<span class="badge green">Tall/Narrow: ${esc(m.worst_tipping_ratio)}x &middot; Brim Active</span>`);
          }
        } else {
          b.push(`<span class="badge green">Stable Base: ${esc(m.worst_tipping_ratio)}x</span>`);
        }
      }
      if (m.non_manifold_parts > 0) b.push(`<span class="badge amber">Non-Manifold: ${esc(m.non_manifold_parts)}</span>`);
      if (m.skipped_modifiers > 0) b.push(`<span class="badge">Skipped ${esc(m.skipped_modifiers)} modifier(s)</span>`);
      mBadges.innerHTML = b.join("");
      mBadges.classList.remove("hidden");
    } else {
      mBadges.classList.add("hidden");
    }

    const visibleTips = state.tips.filter((t) => !(t.manual_only && state.doneManualIds.has(t.id)));
    const doneManualTips = state.tips.filter((t) => t.manual_only && state.doneManualIds.has(t.id));

    const safetyTips = visibleTips.filter((t) => t.bucket === "safety");
    const recTips = visibleTips.filter((t) => t.bucket !== "safety");
    let html = "";
    if (visibleTips.length === 0) {
      html = `<div class="card" style="border-color:#2b4a39;color:var(--green);font-weight:600;">&#10003; Looking great! No remaining issues detected for this model and setup.</div>`;
    } else {
      if (safetyTips.length) {
        html += `<div class="bucket-header safety">&#9650; Likely to Fail / Visible Defects (${safetyTips.length})</div>`;
        html += safetyTips.map(renderTipCard).join("");
      }
      if (recTips.length) {
        html += `<div class="bucket-header recommended">&#9670; Recommended for Your Goal (${recTips.length})</div>`;
        html += recTips.map(renderTipCard).join("");
      }
    }
    document.getElementById("tipsContainer").innerHTML = html;

    const appliedBox = document.getElementById("appliedBox");
    const revertRow = document.getElementById("revertRow");
    if (state.mode === "live" && (state.appliedFixes.length > 0 || state.canRevert)) {
      const count = state.appliedFixes.length;
      document.getElementById("appliedSummary").textContent =
        `✓ Applied to Active Plate (${count}) — click to view / revert`;
      document.getElementById("appliedList").innerHTML = state.appliedFixes.map((f) => {
        const diffText = (f.diffs || [])
          .map((d) => `${d.label}: ${d.before} -> ${d.after}`)
          .join("  |  ");
        return `<li><strong>${esc(f.title)}</strong>${diffText ? `<span class="applied-diffs">${esc(diffText)}</span>` : ""}</li>`;
      }).join("");
      revertRow.classList.toggle("hidden", !state.canRevert);
      appliedBox.classList.remove("hidden");
      if (autoOpenApplied) appliedBox.open = true;
    } else {
      appliedBox.classList.add("hidden");
      appliedBox.open = false;
      revertRow.classList.add("hidden");
    }

    const goodBox = document.getElementById("goodBox");
    const totalGood = state.good.length + doneManualTips.length;
    if (totalGood > 0) {
      document.getElementById("goodSummary").textContent = `✓ Already Good (${totalGood}) — click to view`;
      const doneItemsHtml = doneManualTips.map((t) =>
        `<li>Manual Step Completed: <strong>${esc(t.title)}</strong><button class="btn-undo" onclick="undoManualDone('${esc(t.id)}')">Undo</button></li>`
      );
      const autoGoodHtml = state.good.map((g) => `<li>${esc(g)}</li>`);
      document.getElementById("goodList").innerHTML = doneItemsHtml.concat(autoGoodHtml).join("");
      goodBox.classList.remove("hidden");
    } else {
      goodBox.classList.add("hidden");
    }

    renderImpactBar();
    renderMeshInspector();
  }

  function applyOrExport() {
    const ids = Array.from(state.selectedIds);
    if (!ids.length) return;
    if (state.mode === "live") {
      setStatus("Applying selected settings to active OrcaSlicer process preset...");
      window.orca.postMessage({
        type: "apply_live",
        selected_ids: ids,
        intent: state.intent,
        print_in_place: document.getElementById("pipToggle").checked,
      });
    } else if (state.mode === "3mf") {
      setStatus("Building optimized .3mf...");
      window.orca.postMessage({
        type: "export_3mf",
        selected_ids: ids,
        intent: state.intent,
        print_in_place: document.getElementById("pipToggle").checked,
      });
    }
  }

  function copySummary() {
    const chosen = state.tips.filter(
      (t) => (t.manual_only && !state.doneManualIds.has(t.id)) || (!t.manual_only && state.selectedIds.has(t.id))
    );
    const lines = [
      `Print Advisor Summary — ${state.mode === "3mf" ? state.filename : "Active OrcaSlicer Plate"}`,
      `Intent: ${state.intent} | Print-in-Place: ${document.getElementById("pipToggle").checked ? "ON" : "OFF"}`,
      "",
    ];
    if (state.mode === "live" && state.appliedFixes.length) {
      lines.push("Applied to Active Plate:");
      state.appliedFixes.forEach((f, i) => {
        lines.push(`${i + 1}. ${f.title}`);
        (f.diffs || []).forEach((d) => lines.push(`   - ${d.label}: ${d.before} -> ${d.after}`));
      });
      lines.push("");
    }
    if (!chosen.length) {
      lines.push("No pending fixes selected.");
    } else {
      lines.push("Pending / Manual Items:");
      chosen.forEach((t, i) => {
        lines.push(`${i + 1}. ${t.manual_only ? "[MANUAL] " : ""}${t.title} (${t.impact.label})`);
        t.diffs.forEach((d) => lines.push(`   - ${d.label}: ${d.before} -> ${d.after}`));
        lines.push(`   Manual: ${t.manual_path}`);
      });
    }
    const ta = document.createElement("textarea");
    ta.value = lines.join("\\n");
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) {}
    document.body.removeChild(ta);
    setStatus("Summary copied to clipboard!", "ok");
  }

  function triggerBlobDownload(filename, b64) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const url = URL.createObjectURL(new Blob([bytes], { type: "application/octet-stream" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }

  window.orca.onMessage((msg) => {
    if (msg.type === "context") {
      updateContextStrip(msg.data);
    } else if (msg.type === "evaluation") {
      if (msg.error) {
        document.getElementById("dropSub").textContent = "Error: " + msg.error;
        setStatus("Error: " + msg.error, "err");
        return;
      }
      updateContextStrip(msg.meta);
      if (msg.empty_plate) {
        state.mesh = null;
        state.canExport3mf = false;
        state.canRevert = false;
        state.tips = [];
        state.appliedFixes = [];
        state.doneManualIds = new Set();
        state.good = [];
        state.guardrails = [];
        renderEmptyPlate(msg.empty_reason);
        return;
      }
      state.mode = msg.source_mode || state.mode;
      state.mesh = msg.mesh || null;
      state.canExport3mf = !!msg.can_export_3mf;
      state.canRevert = !!msg.can_revert;
      state.tips = msg.tips || [];
      state.appliedFixes = msg.applied_fixes || [];
      state.good = msg.good || [];
      state.guardrails = msg.guardrails || [];
      state.selectedIds = new Set(state.tips.filter((t) => !t.manual_only && t.default_checked).map((t) => t.id));
      renderResults(!!msg.just_applied);
      if (msg.status_override) {
        setStatus(msg.status_override, msg.status_tone || "ok");
      } else {
        const extra = msg.elapsed_ms !== undefined ? ` (scanned in ${msg.elapsed_ms} ms)` : "";
        setStatus(state.mode === "3mf" ? `Loaded .3mf analysis ready${extra}.` : `Live plate geometry analysis ready${extra}.`);
      }
    } else if (msg.type === "apply_live_error") {
      setStatus("Could not apply directly: " + msg.error, "err");
    } else if (msg.type === "export_ready") {
      if (msg.error) {
        setStatus("Export error: " + msg.error, "err");
        return;
      }
      triggerBlobDownload(msg.filename, msg.content_b64);
      setStatus(`Saved ${msg.filename} (${msg.keys_patched} setting(s) updated; stale G-code stripped). Open in OrcaSlicer and slice!`, "ok");
    }
  });

  function handleFile(file) {
    if (!file) return;
    const sizeMb = file.size / (1024 * 1024);
    if (sizeMb > MAX_BLOCK_MB) {
      document.getElementById("dropTitle").textContent = "File too large (" + sizeMb.toFixed(1) + " MB)";
      document.getElementById("dropSub").textContent =
        "Files over 60 MB can freeze the UI. Re-save without G-code or use 'Scan Active Plate Project'.";
      return;
    }
    state.mode = "3mf";
    state.filename = file.name;
    state.doneManualIds = new Set();
    document.getElementById("dropTitle").textContent = file.name;
    document.getElementById("dropSub").textContent = sizeMb > MAX_WARN_MB
      ? `Large file (${sizeMb.toFixed(1)} MB) — scanning may take a few seconds...`
      : `Scanning ${Math.round(file.size / 1024)} KB...`;

    const reader = new FileReader();
    reader.onload = () => {
      window.orca.postMessage({
        type: "load_3mf",
        filename: file.name,
        content_b64: reader.result.split(",")[1],
        intent: state.intent,
        print_in_place: document.getElementById("pipToggle").checked,
      });
    };
    reader.readAsDataURL(file);
  }

  document.getElementById("fileInput").addEventListener("change", (e) => {
    handleFile(e.target.files[0]);
    e.target.value = "";
  });
  const dz = document.getElementById("dropzone");
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("drag"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    dz.classList.remove("drag");
    handleFile(e.dataTransfer.files[0]);
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.mode !== "3mf") requestContext();
  });

  requestContext();
</script>
</body>
</html>
"""


# ============================================================================
# 9. ORCASLICER PAGES CAPABILITY & PLUGIN ENTRYPOINT
# ============================================================================

class PrintAdvisorPage(orca.pages.PagesPluginCapabilityBase):
    """Routes UI messages, caches the loaded .3mf, and manages preset-safe live apply/revert."""

    def __init__(self):
        super().__init__()
        self._cached_filename = ""
        self._cached_bytes = None
        self._cached_mesh = None
        self._cached_project_settings = None
        self._live_applied_fixes = []    # Applied tips shown in the Live Plate collapsible
        self._live_revert_snapshot = {}  # Original pre-patch process values for 1-click Revert
        self._live_revert_preset = ""    # Clean process preset name the snapshot belongs to

    def get_name(self):
        return "Print Advisor"

    def get_ui(self):
        return APP_HTML

    def _clear_3mf_cache(self):
        self._cached_filename = ""
        self._cached_bytes = None
        self._cached_mesh = None
        self._cached_project_settings = None

    def _clear_live_history(self):
        self._live_applied_fixes = []
        self._live_revert_snapshot = {}
        self._live_revert_preset = ""

    def _collect_selected_and_patches(self, ctx, mesh, selected_ids, intent, print_in_place):
        """Re-evaluates rules and returns (chosen_tips, merged_patches) with Priority 1 winning conflicts."""
        res = RuleEngine.evaluate(ctx, mesh, intent=intent, print_in_place=print_in_place)
        chosen = [t for t in res["tips"] if not t.get("manual_only") and t["id"] in selected_ids]
        chosen_ordered = sorted(chosen, key=lambda t: t["priority"], reverse=True)
        merged = {}
        for t in chosen_ordered:
            merged.update(t["patches"])
        return chosen, merged

    def _record_live_applied(self, chosen_tips):
        """Adds or updates applied tips in the live session list without duplicates."""
        by_id = {item["id"]: item for item in self._live_applied_fixes}
        order = [item["id"] for item in self._live_applied_fixes]
        for t in chosen_tips:
            entry = {"id": t["id"], "title": t["title"], "diffs": t.get("diffs", [])}
            by_id[t["id"]] = entry
            if t["id"] not in order:
                order.append(t["id"])
        self._live_applied_fixes = [by_id[tid] for tid in order]

    def _emit_evaluation(
        self, source_mode, mesh, proj_settings, intent, print_in_place,
        elapsed_ms=None, status_override=None, status_tone="ok", just_applied=False,
    ):
        ctx = ContextReader.build_effective_context(proj_settings)

        if not mesh or mesh.get("empty"):
            self._clear_live_history()
            self.post_message({
                "type": "evaluation",
                "source_mode": source_mode,
                "meta": ctx["active_meta"],
                "empty_plate": True,
                "empty_reason": (mesh or {}).get("reason", "No 3D model found on the active plate."),
            })
            return

        res = RuleEngine.evaluate(ctx, mesh, intent=intent, print_in_place=print_in_place)
        mesh_public = {k: v for k, v in mesh.items() if k != "project_settings"}

        # Keep live applied history & revert snapshot scoped strictly to the same process preset
        applied_for_ui = []
        can_revert = False
        if source_mode == "live":
            current_proc = ctx["active_meta"]["process"]
            if self._live_revert_preset and current_proc != self._live_revert_preset:
                self._clear_live_history()
            else:
                active_ids = {t["id"] for t in res["tips"] if not t.get("manual_only")}
                self._live_applied_fixes = [f for f in self._live_applied_fixes if f["id"] not in active_ids]
                if self._live_revert_snapshot and all(
                    ctx["settings"].get(k) == orig_v for k, orig_v in self._live_revert_snapshot.items()
                ):
                    self._live_revert_snapshot = {}
                    self._live_revert_preset = ""
                applied_for_ui = list(self._live_applied_fixes)
                can_revert = bool(self._live_revert_snapshot)

        payload = {
            "type": "evaluation",
            "source_mode": source_mode,
            "empty_plate": False,
            "meta": ctx["active_meta"],
            "mesh": mesh_public,
            "can_export_3mf": bool(source_mode == "3mf" and self._cached_bytes and proj_settings is not None),
            "can_revert": can_revert,
            "tips": res["tips"],
            "applied_fixes": applied_for_ui,
            "just_applied": just_applied,
            "good": res["good"],
            "guardrails": res["guardrails"],
        }
        if elapsed_ms is not None:
            payload["elapsed_ms"] = elapsed_ms
        if status_override:
            payload["status_override"] = status_override
            payload["status_tone"] = status_tone
        self.post_message(payload)

    def on_message(self, data):
        msg_type = data.get("type")

        if msg_type == "get_context":
            self.post_message({"type": "context", "data": ContextReader.get_active_summary()})

        elif msg_type == "clear_3mf":
            self._clear_3mf_cache()
            self._clear_live_history()

        elif msg_type == "scan_live_plate":
            self._clear_3mf_cache()
            t0 = time.perf_counter()
            intent = data.get("intent", "engineering")
            pip = bool(data.get("print_in_place", False))
            live_mesh = LivePlateAnalyzer.analyze()
            elapsed_ms = round((time.perf_counter() - t0) * 1000)
            self._emit_evaluation("live", live_mesh, None, intent, pip, elapsed_ms=elapsed_ms)

        elif msg_type == "apply_live":
            selected_ids = set(data.get("selected_ids", []))
            intent = data.get("intent", "engineering")
            pip = bool(data.get("print_in_place", False))
            try:
                live_mesh = LivePlateAnalyzer.analyze()
                if live_mesh.get("empty"):
                    self.post_message({"type": "apply_live_error", "error": "No model found on the active plate."})
                    return

                ctx = ContextReader.build_effective_context(None)
                current_proc = ctx["active_meta"]["process"]
                if self._live_revert_preset and self._live_revert_preset != current_proc:
                    self._clear_live_history()

                chosen, merged = self._collect_selected_and_patches(ctx, live_mesh, selected_ids, intent, pip)
                if not merged:
                    self.post_message({"type": "apply_live_error", "error": "No applicable settings selected."})
                    return

                # Capture first-seen original values before applying so Revert always restores true baseline
                pending_snapshot = {
                    k: ctx["settings"][k]
                    for k in merged
                    if k not in self._live_revert_snapshot and k in ctx["settings"]
                }

                res = LivePlatePatcher.apply(merged)
                self._live_revert_preset = current_proc
                self._live_revert_snapshot.update(pending_snapshot)
                self._record_live_applied(chosen)

                target = f" '{res['target_name']}'" if res.get("target_name") else ""
                notice = (
                    f"Applied {res['applied']} setting(s) to active process{target} "
                    f"(unsaved — switch to Prepare/Preview to slice, or click Revert above)."
                )
                self._emit_evaluation(
                    "live", live_mesh, None, intent, pip,
                    status_override=notice, just_applied=True,
                )
            except Exception as e:
                self.post_message({"type": "apply_live_error", "error": str(e)})

        elif msg_type == "revert_live":
            intent = data.get("intent", "engineering")
            pip = bool(data.get("print_in_place", False))
            if not self._live_revert_snapshot:
                self.post_message({"type": "apply_live_error", "error": "Nothing to revert."})
                return
            try:
                current_proc = ContextReader.get_active_summary()["process"]
                if self._live_revert_preset and current_proc != self._live_revert_preset:
                    old_proc = self._live_revert_preset
                    self._clear_live_history()
                    live_mesh = LivePlateAnalyzer.analyze()
                    self._emit_evaluation(
                        "live", live_mesh, None, intent, pip,
                        status_override=f"Revert skipped: active process changed from '{old_proc}' to '{current_proc}'.",
                        status_tone="err",
                    )
                    return

                res = LivePlatePatcher.apply(self._live_revert_snapshot)
                self._clear_live_history()
                live_mesh = LivePlateAnalyzer.analyze()
                notice = f"Reverted {res['applied']} setting(s) back to your original process values."
                self._emit_evaluation(
                    "live", live_mesh, None, intent, pip,
                    status_override=notice, status_tone="ok",
                )
            except Exception as e:
                self.post_message({"type": "apply_live_error", "error": f"Revert failed: {e}"})

        elif msg_type == "load_3mf":
            self._clear_live_history()
            t0 = time.perf_counter()
            filename = data.get("filename", "model.3mf")
            intent = data.get("intent", "engineering")
            pip = bool(data.get("print_in_place", False))
            try:
                raw_bytes = base64.b64decode(data.get("content_b64", ""))
                with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as zf:
                    mesh_result = MeshAnalyzer(zf).analyze()
                if "error" in mesh_result:
                    self.post_message({"type": "evaluation", "error": mesh_result["error"]})
                    return

                self._cached_filename = filename
                self._cached_bytes = raw_bytes
                self._cached_mesh = mesh_result
                self._cached_project_settings = mesh_result.get("project_settings")

                elapsed_ms = round((time.perf_counter() - t0) * 1000)
                self._emit_evaluation("3mf", mesh_result, self._cached_project_settings, intent, pip, elapsed_ms=elapsed_ms)
            except Exception as e:
                self.post_message({"type": "evaluation", "error": str(e)})

        elif msg_type == "evaluate_3mf":
            intent = data.get("intent", "engineering")
            pip = bool(data.get("print_in_place", False))
            if self._cached_mesh is None:
                self._emit_evaluation("live", {"empty": True, "reason": "No .3mf loaded."}, None, intent, pip)
                return
            self._emit_evaluation("3mf", self._cached_mesh, self._cached_project_settings, intent, pip)

        elif msg_type == "export_3mf":
            selected_ids = set(data.get("selected_ids", []))
            intent = data.get("intent", "engineering")
            pip = bool(data.get("print_in_place", False))

            if not self._cached_bytes or not self._cached_project_settings:
                self.post_message({"type": "export_ready", "error": "No patchable .3mf loaded."})
                return

            try:
                ctx = ContextReader.build_effective_context(self._cached_project_settings)
                _, merged = self._collect_selected_and_patches(ctx, self._cached_mesh, selected_ids, intent, pip)
                patched_bytes = ThreeMFPatcher.patch(self._cached_bytes, merged)
                stem = self._cached_filename[:-4] if self._cached_filename.lower().endswith(".3mf") else self._cached_filename

                self.post_message({
                    "type": "export_ready",
                    "filename": f"{stem}_advised.3mf",
                    "keys_patched": len(merged),
                    "content_b64": base64.b64encode(patched_bytes).decode("ascii"),
                })
            except Exception as e:
                self.post_message({"type": "export_ready", "error": str(e)})


@orca.plugin
class PrintAdvisorPlugin(orca.base):
    def register_capabilities(self):
        orca.register_capability(PrintAdvisorPage)