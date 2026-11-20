# Print Advisor for OrcaSlicer

An educational, zero-risk 3D print analyzer plugin for **OrcaSlicer 2.5+**.

Print Advisor inspects your actual 3D mesh geometry alongside your active printer, nozzle, filament, and process settings—explaining *why* a print might fail or show defects, letting you pick exactly which safe fixes to apply, and keeping every change reversible.

* **Zero external dependencies** (pure Python 3.12 standard library)
* **Zero OS permissions required** (runs 100% offline inside OrcaSlicer's sandbox)
* **Geometry-first** (never guesses or gives blind tips on an empty plate)

---

## Two Ways to Use It

| Mode | Best For | How Changes Are Applied |
| :--- | :--- | :--- |
| **Live Plate Scan** (`Scan Active Plate Project`) | Quick checks on whatever you're slicing right now | **1-Click Apply** to your active process preset (unsaved in memory) with **1-Click Revert** |
| **`.3mf` File Dropzone** | Inspecting MakerWorld / saved projects offline | **Non-destructive export** (`*_advised.3mf`) with stale G-code stripped so OrcaSlicer slices cleanly |

---

## Companion Plugin: MakerWorld `.3mf` Workflow

Printing MakerWorld `.3mf` files on a non-Bambu printer? Print Advisor pairs directly with **[Orca MakerWorld Importer](https://github.com/FleeceFlames/Orca_makerworld_importer.git)**:

1. **Convert the profile first:** Use [Orca MakerWorld Importer](https://github.com/FleeceFlames/Orca_makerworld_importer.git) to cleanly transfer the creator's print process settings onto your active printer and filament profile.
2. **Audit the geometry & settings:** Run **Print Advisor** on the converted project to catch hidden issues (curling bottom fillets, Grid infill collisions, tipping risk, or goal-specific wall/layer tweaks) before slicing.

If you drop an unconverted `.3mf` built for a different machine directly into Print Advisor, the **Printer Mismatch** guardrail will automatically flag it and remind you to convert it first.

---

## What It Checks

1. **Steep Overhangs & Bottom Fillets (55°–85°):** Detects curling-prone curved base edges (bottom 5 mm) and steep overhangs; recommends overhang slowdowns, Inner/Outer wall order, and provides a step-by-step **Variable Layer Height** guide that auto-clears once custom layering is detected (`has_custom_layering` on live plates or `layer_heights_profile.txt` in `.3mf` files).
2. **Nozzle Collision Prevention:** Flags self-intersecting **Grid** infill and recommends **Gyroid**.
3. **Goal-Specific Tuning:**
   * **Engineering / Functional:** Nozzle-aware shell thickness check (`>= 1.2 mm` outer walls).
   * **Figure / Display:** Finer layer height recommendations + optional manual Scarf Seam / Tree Support guide.
   * **Fast Prototype:** Safety checks + opt-in (unchecked) wall/infill trim for faster fit tests.
   * **Flat Sign / Multi-Color:** Enables **Arachne** walls for crisp text + opt-in Top-Surface Ironing.
4. **Tipping & ABS/ASA Warp Guard:** Adds a 5 mm outer brim for tall/narrow footprints or high-shrinkage materials (suppressed when Print-in-Place is ON).
5. **PETG & TPU Stringing Guard:** Recommends **Avoid crossing walls** without touching retraction calibration.
6. **CoreXY-Safe First-Layer Bond:** Flags overly aggressive Layer 1 extrusion speeds while leaving fast travel speeds untouched.
7. **Print-in-Place Clearance Guard:** Disables brims and enables light Elephant's Foot compensation (`0.15 mm`) so hinges and snap-fits don't fuse shut.

### Built-In Safety Guardrails
* **Spiral Vase Protection:** Automatically bypasses wall, infill, layer height, and wall-order rules.
* **Mesh Health Checks:** Detects non-manifold parts, inverted base normals, floating bridges, and skips non-printable modifier/negative volumes.
* **Per-Object Override & Printer Mismatch Warnings:** Alerts you if a part overrides a global setting or if a `.3mf` belongs to a different printer (recommending [Orca MakerWorld Importer](https://github.com/FleeceFlames/Orca_makerworld_importer.git) first).
* **Preset-Locked 1-Click Revert:** Tracks the exact process preset you patched so switching profiles in OrcaSlicer never applies stale revert values to a different preset.

---

## "Do No Harm" Policy

| Print Advisor WILL Patch (Only What You Check) | Print Advisor NEVER Auto-Modifies |
| :--- | :--- |
| Overhang slowdown speeds & Inner/Outer wall order | Nozzle or build plate temperatures |
| Grid → Gyroid infill pattern | Retraction distance or speed |
| Wall loops, layer height, Arachne walls, opt-in ironing/draft trims | Flow ratio, Pressure Advance, or Z-offset |
| Outer brim / Print-in-Place brim suppression & Elephant's Foot | Filament cooling curves (read-only guidance only) |
| Avoid crossing walls & Layer 1 extrusion speeds | Supports, scarf seams, or Variable Layer Height (step-by-step manual guides) |

---

## Manual Tips & Preview Verification Guide

Some fixes require visual judgment or touch hardware-specific calibration, so Print Advisor shows them as **blue `MANUAL` cards** rather than auto-patching them. Here is how to apply and verify them quickly in OrcaSlicer:

### 1. Smoothing Curved Bottom Edges (Variable Layer Height)
Steep rounded edges near the build plate (bottom fillets) often curl at `0.20 mm` layers even after slowing down. Thinning *just* the curved zone gives each outer line much more overlap without slowing down flat walls:
1. Switch to the **Prepare** tab, click your model, and click the **Variable Layer Height** icon (stacked horizontal lines) in the top toolbar.
2. Slide **Quality / Speed** slightly left to **`0.25–0.35`** (or leave at `0.50` for a milder effect) and click **Adaptive** once.
3. Leave **Radius** at `5`, click **Smooth** 1–2 times to soften layer transitions, then click **Done**.
4. Return to **Print Advisor** and click **Re-Scan Plate**—Print Advisor detects custom layering automatically, marks **`VLH: Active`**, and clears the manual tip.

### 2. How to Verify Overhangs & Layer Heights in the Preview Tab
* **Don't panic at red areas:** In **Line Type** view, **Red** is just a normal **Top surface** (completely healthy). Actual problem zones are **Dark Blue (`Overhang wall`)** and **Blue (`Bridge`)**.
* **Orbit underneath the plate:** Bottom fillets are hidden when looking from above—tilt the camera below the build plate in **Preview** to inspect curved bottom edges and overhangs.
* **Verify Variable Layer Height:** Change the top-right Preview color dropdown from **Line Type** to **Layer Height**. Curved bottom edges should show thinner layers (`0.08–0.12 mm`) while straight vertical walls stay at your normal height (`0.20 mm`).

### 3. Overhang Cooling (Read-Only Filament Check)
Print Advisor never edits your filament presets because cooling depends on your toolhead and material. If your PLA or PETG overhang fan is flagged as too low:
* Click the **Edit** icon next to your filament → **Cooling** tab.
* Confirm **Force cooling for overhangs and bridges** is ON, with **Fan speed for overhangs** around **`100%` (PLA)** or **`70–90%` (PETG)** and a **`25–50%` threshold**.
* If your filament already meets those targets, Print Advisor leaves it untouched and lists it under **Already Good**.

### 4. Display Polish (Scarf Seams & Tree Supports)
* **Scarf Joint Seams (`Quality → Seam`):** Setting **Contour** hides vertical seam lines on smooth figures, but works best when your filament **Flow Ratio** is calibrated—always check the white seam markers in **Preview**.
* **Tree (Auto) Organic Supports (`Support` tab):** Great for organic figures, but always inspect **Preview** to make sure branches don't pack into tiny screw holes or tight channels before printing.
* **Using `✓ Mark Done`:** Once you have visually verified a manual step, click **`✓ Mark Done`** on the blue card to move it into **Already Good** (you can click **Undo** there anytime).

### 5. Reading the Mesh & Model Inspector (`Needs Fix` vs. `Protected`)
The numbers inside **Mesh & Model Inspector** (dimensions, bed area, bottom fillet %, overhang area) measure your model's physical 3D geometry—they do **not** drop to `0` after you apply slicer fixes. Instead, watch the status color:
* **Amber (`Needs Fix`)** — Geometry risk detected with pending tips.
* **Green (`Protected` / `Brim Active`)** — Geometry risk is present on the model, and your slicer settings are now configured to handle it safely.

---

## Installation

### Option A: Orca Cloud (Recommended)
1. Open **OrcaSlicer 2.5+** and go to the **Plugins / Orca Cloud** tab.
2. Search for **Print Advisor** and click **Install**.
3. Open the new **Print Advisor** tab—no extra permissions needed.

### Option B: Manual Local Install
1. Download `print_advisor_any.py` from the [Releases](../../releases) page.
2. Place it in your OrcaSlicer plugins folder (or load it via **Plugins → Load Local Plugin**).
3. Restart or reload plugins and open the **Print Advisor** tab.

---

## License

MIT License — see [LICENSE](LICENSE).
