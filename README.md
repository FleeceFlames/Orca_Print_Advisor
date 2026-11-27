# Print Advisor for OrcaSlicer

An educational, zero-risk 3D print analyzer plugin for **OrcaSlicer 2.5+**.

Print Advisor inspects your actual 3D mesh geometry alongside your active printer, nozzle, filament, and process settings—explaining *why* a print might fail or show defects, letting you pick exactly which safe fixes to apply, and keeping every change reversible.

- **Zero external dependencies** (pure Python 3.12 standard library)
- **Zero OS permissions required** (runs 100% offline inside OrcaSlicer's sandbox)
- **Geometry-first** (never guesses or gives blind tips on an empty plate)

---

## Two Ways to Use It

| Mode | Best For | How Changes Are Applied & Exported |
| :--- | :--- | :--- |
| **Live Plate Scan** (`Scan Active Plate Project`) | Quick checks on whatever you're slicing right now | **1-Click Apply** (`Apply Selected to Active Plate`) to your active process preset (unsaved in memory) with **1-Click Revert**, plus **Download Optimized .3mf** (`*_advised.3mf`) and **Download Report.txt** (`*_report.txt`) once fixes are applied or manual steps are marked done |
| **`.3mf` File Dropzone** | Inspecting MakerWorld / saved projects offline | **Non-destructive export** (`*_advised.3mf`) with stale G-code stripped and modified keys registered so OrcaSlicer slices cleanly, plus **Download Report.txt** (`*_report.txt`) |

---

## Companion Plugin: MakerWorld `.3mf` Workflow

Printing MakerWorld `.3mf` files on a non-Bambu printer? Print Advisor pairs directly with **[Orca MakerWorld Importer](https://github.com/FleeceFlames/Orca_makerworld_importer)**:

1. **Convert the profile first:** Use [Orca MakerWorld Importer](https://github.com/FleeceFlames/Orca_makerworld_importer) to cleanly transfer the creator's print process settings onto your active printer and filament profile.
2. **Audit the geometry & settings:** Run **Print Advisor** on the converted project to catch hidden issues (curling bottom fillets, unsupported bridges, Grid infill collisions, tipping risk, or goal-specific wall/layer tweaks) before slicing.

If you drop an unconverted `.3mf` built for a different machine directly into Print Advisor, the **Printer Mismatch** guardrail will automatically flag it and remind you to convert it first.

---

## What It Checks

1. **Steep Overhangs & Bottom Fillets (55°–85°):** Detects curling-prone curved base edges (bottom 5 mm) and steep overhangs; recommends overhang slowdowns, inner-first wall order, and provides a step-by-step **Variable Layer Height** guide (automatically suppressed when supports are active, with an amber `⚠` warning not to combine VLH with Organic/Tree supports).
2. **Floating Horizontal Bridges (`>85°`):** Detects unsupported mid-air ceilings and bridges (`>= 40 mm²`). When supports are already active, it clears into **Already Good** (`Supports Active`); when supports are OFF, it offers an **opt-in** breakaway **Tree (Auto) · Organic** support fix (`On build plate only`, `2` interface layers) with material-tuned **Top Z distance**.
3. **Nozzle Collision Prevention:** Flags self-intersecting **Grid** infill and recommends **Gyroid**.
4. **Goal-Specific Tuning:**
   - **Engineering / Functional:** Nozzle-aware shell thickness check (`>= 1.2 mm` outer walls).
   - **Figure / Display:** Finer layer height recommendations + optional manual Scarf Seam / Tree Support guide.
   - **Fast Prototype:** Safety checks + opt-in (unchecked) wall/infill trim for faster fit tests.
   - **Flat Sign / Multi-Color:** Enables **Arachne** walls for crisp text + opt-in Top-Surface Ironing.
5. **Tipping & High-Warp Polymer Guard:** Adds a 5 mm outer brim for tall/narrow footprints or high-shrinkage materials (**ABS, ASA, HIPS, PC/PC-ABS, and unfilled Nylon/PA**) — suppressed when Print-in-Place is ON.
6. **PETG, PCTG & TPU Stringing Guard:** Recommends **Avoid crossing walls** without touching retraction calibration.
7. **CoreXY-Safe First-Layer Bond:** Flags overly aggressive Layer 1 extrusion speeds while leaving fast travel speeds untouched.
8. **Print-in-Place Clearance Guard:** Disables brims and enables light Elephant's Foot compensation (`0.15 mm`) so hinges and snap-fits don't fuse shut.
9. **Smooth Surface Finish Modifier (Cross-Profile, All Filaments):** A persistent toggle that layers cosmetic surface rules on top of any goal—capping outer-wall speed and acceleration downward only (respecting slower printers/bedslingers), enforcing `Inner/Outer/Inner` wall order (when `>= 3` walls), enabling **Conditional Scarf Joint Seams** (automatically disabled for **TPU/Flex** and foaming **Aero** filaments), routing travel inside walls, and surfacing a material-specific readiness checklist.

### Built-In Safety Guardrails
- **Spiral Vase Protection:** Automatically bypasses wall, infill, layer height, seam, bridge support, and wall-order rules.
- **VLH + Organic Support Conflict Guard:** Automatically suppresses Variable Layer Height tips when supports are active and warns if both are already enabled on the same model (since OrcaSlicer cannot slice VLH with Organic supports).
- **Mesh Health Checks:** Detects non-manifold parts, inverted base normals, floating bridges, and skips non-printable modifier/negative volumes.
- **Per-Object Override & Printer Mismatch Warnings:** Alerts you if a part overrides a global setting or if a `.3mf` belongs to a different printer (recommending [Orca MakerWorld Importer](https://github.com/FleeceFlames/Orca_makerworld_importer) first).
- **Preset-Locked 1-Click Revert:** Captures your full baseline before patching and tracks the exact process preset you modified so switching profiles in OrcaSlicer never applies stale revert values to a different preset.

---

## "Do No Harm" Policy

| Print Advisor WILL Patch (Only What You Check) | Print Advisor NEVER Auto-Modifies |
| :--- | :--- |
| Overhang slowdown speeds & inner-first wall order | Nozzle, bed, or heated chamber temperatures |
| Grid → Gyroid infill pattern | Retraction distance or speed |
| Wall loops, layer height, Arachne walls, opt-in ironing/draft trims & opt-in Tree bridge supports | Flow ratio, Pressure Advance, or Z-offset |
| Outer brim / Print-in-Place brim suppression & Elephant's Foot | Filament cooling fan curves (read-only guidance matched to your filament) |
| Avoid crossing walls, Layer 1 extrusion speeds & Smooth outer-wall/scarf-seam tuning (when Smooth toggle is ON) | Variable Layer Height (step-by-step manual guide so you can inspect the curve in Preview) |

---

## Smooth Surface Finish Mode (All Filaments & Prerequisites)

Enabling **Smooth Surface Finish (Cosmetic Polish)** layers outer-skin optimizations on top of any goal (*Engineering*, *Figure*, *Prototype*, or *Sign*) and remembers your toggle state across sessions. It inspects both your active `filament_type` and filament preset name to tailor speeds and scarf seams safely across rigid, composite, high-temp, flexible, and foaming materials.

### Before Expecting Flawless Results (Beginner Checklist)
Slicer toolpath tweaks improve wall sheen, corner ringing, and seam transitions, **but they cannot compensate for wet filament or uncalibrated extrusion**. Check these four fundamentals first:

1. **Dry Your Filament First:** PETG, PA/Nylon, PC, TPU, ABS/ASA, CF/GF composites, and even older PLA absorb moisture from the air. Damp filament boils inside the nozzle, leaving micro-pits, fuzzy walls, and stringing that no slicer setting can hide.
2. **Calibrate Flow Ratio (`Calibration → Flow Rate`):** Even 3–5% over-extrusion creates tiny ridges where scarf seams overlap and roughens angled overhangs.
3. **Calibrate Pressure Advance (`Calibration → Pressure Advance`):** Prevents bulging at sharp corners and blobs at seam start/stop points.
4. **Use Variable Layer Height on Rounded Tops & Bevels:** Process settings do not change Z-step height—use **Variable Layer Height → Adaptive + Smooth** on domes, fillets, or shallow chamfers (when not using Organic/Tree supports).

### How Smooth Mode Adapts by Material

| Material Family | Outer Wall Cap | Conditional Scarf Seam | Key Material Notes |
| :--- | :--- | :--- | :--- |
| **PLA / PLA+ / Matte / Wood / PVB** | `60 mm/s` (`3000 mm/s²`) | **ON** (`Contour & hole`) | `80–100%` overhang fan; use a `0.6 mm` nozzle for wood/mineral fills; PVB can also be misted with 90%+ IPA. |
| **Silk / Glossy PLA** | `45 mm/s` (`2000 mm/s²`) | **ON** (`Contour & hole`) | Slower steady outer-wall speed keeps mirror gloss uniform without dull speed-bands. |
| **PETG / PCTG** | `50 mm/s` (`2500 mm/s²`) | **ON** (`Contour & hole`) | `60–80%` overhang fan with low regular cooling (`15–30%`) for uniform gloss and zero stringing. |
| **CF / GF Composites** (`PETG-CF`, `PAHT-CF`, `PET-CF`, `PLA-CF`, `ABS-CF`) | `55 mm/s` (`2500 mm/s²`) | **ON** (`Contour & hole`) | Hardened nozzle required; keep cooling low (`0–30%`) on high-temp PA/PET/ABS/ASA composites. |
| **ABS / ASA / PC / Nylon (PA)** | `45–55 mm/s` (`2000–2500 mm/s²`) | **ON** (`Contour & hole`) | Cooling capped low (`0–35%`) inside a warm enclosure to prevent corner warping or layer splitting. |
| **TPU / Flex & Foaming Aero** (`PLA-AERO`, `ASA-Aero`) | `35–45 mm/s` (`1500–2000 mm/s²`) | **OFF** (`None`) | Disables scarf flow-ramping so flexible filaments don't suffer nozzle pressure lag and Aero filaments keep consistent foaming density. |

---

## Manual Tips & Preview Verification Guide

Some fixes require visual judgment or touch hardware-specific calibration, so Print Advisor shows them as **blue `MANUAL` cards** (or **opt-in** checkboxes) rather than auto-applying them blindly. Here is how to apply and verify them quickly in OrcaSlicer:

### 1. Smoothing Curved Bottom Edges (Variable Layer Height)
Steep rounded edges near the build plate (bottom fillets) often curl at `0.20 mm` layers even after slowing down. Thinning *just* the curved zone gives each outer line much more overlap without slowing down flat walls:
1. Switch to the **Prepare** tab, click your model, and click the **Variable Layer Height** icon (stacked horizontal lines) in the top toolbar.
2. Slide **Quality / Speed** slightly left to **`0.25–0.35`** (or leave at `0.50` for a milder effect) and click **Adaptive** once.
3. Leave **Radius** at `5`, click **Smooth** 1–2 times to soften layer transitions, then click **Done**.
4. Return to **Print Advisor** and click **Re-Scan Plate**—Print Advisor detects custom layering automatically, marks **`VLH: Active`**, and clears the manual tip.

> **Warning:** Do not combine **Variable Layer Height** with **Organic / Tree supports**—OrcaSlicer cannot slice both together. Print Advisor automatically suppresses VLH tips when supports are enabled.

### 2. How to Verify Overhangs, Bridges & Layer Heights in the Preview Tab
- **Don't panic at red areas:** In **Line Type** view, **Red** is just a normal **Top surface** (completely healthy). Actual problem zones are **Dark Blue (`Overhang wall`)** and **Blue (`Bridge`)**.
- **Check floating bridges (`>85°`):** Short straight gaps (`<15 mm`) anchored on both ends can often bridge without supports. If Preview shows unsupported mid-air islands or wide ceilings, tick the opt-in **Enable Tree Supports for Floating Bridges** card (or enable supports in OrcaSlicer and click **Re-Scan Plate**).
- **Orbit underneath the plate:** Bottom fillets are hidden when looking from above—tilt the camera below the build plate in **Preview** to inspect curved bottom edges and overhangs.
- **Verify Variable Layer Height:** Change the top-right Preview color dropdown from **Line Type** to **Layer Height**. Curved bottom edges should show thinner layers (`0.08–0.12 mm`) while straight vertical walls stay at your normal height (`0.20 mm`).

### 3. Overhang Cooling (Read-Only Filament Check)
Print Advisor never edits your filament presets because cooling depends on your toolhead and material. If your PLA or PETG overhang fan is flagged as too low:
- Click the **Edit** icon next to your filament → **Cooling** tab.
- Confirm **Force cooling for overhangs and bridges** is ON, with **Fan speed for overhangs** around **`100%` (PLA)** or **`60–80%` (PETG)** and a **`25–50%` threshold**.
- If your filament already meets those targets, Print Advisor leaves it untouched and lists it under **Already Good**.

### 4. `✓ Mark Done`, Optimized `.3mf` & `report.txt` Export
- **Using `✓ Mark Done`:** Once you have visually verified a manual step, click **`✓ Mark Done`** on the blue card to move it into **Already Good** (click **Undo** there anytime). Marking a manual step done also increments the `(x)` counter on **Download Optimized .3mf (x)** and **Download Report.txt (x)**.
- **Exporting `*_advised.3mf` & `*_report.txt`:** In **Live Plate** mode, applying fixes or marking manual steps done unlocks **Download Optimized .3mf (x)** (packages your live plate geometry and active settings with clean filename stems and modified keys registered in `different_settings_to_system[0]`) and **Download Report.txt (x)** (logs your printer/filament context, mode toggles, `before → after` setting diffs, raw config keys, and completed manual steps).

### 5. Reading the Mesh & Model Inspector (`Needs Fix` vs. `Protected`)
The numbers inside **Mesh & Model Inspector** (dimensions, bed area, bottom fillet %, overhang area, bridge area) measure your model's physical 3D geometry—they do **not** drop to `0` after you apply slicer fixes. Instead, watch the status color:
- **Amber (`Needs Fix` / `Supports OFF`)** — Geometry risk detected with pending tips.
- **Green (`Protected` / `Supports Active` / `Brim Active`)** — Geometry risk is present on the model, and your slicer settings are now configured to handle it safely.

---

## Installation

### Option A: Orca Cloud (Recommended)
1. Open **OrcaSlicer 2.5+** and go to the **Plugins / Orca Cloud** tab.
2. Search for **[Print Advisor](https://cloud.orcaslicer.com/p/3b308f5ef16a)** and click **Install** (or **Update** if you already have an earlier version installed).
3. Open the new **Print Advisor** tab—no extra permissions needed.

### Option B: Manual Local Install
1. Download `print_advisor_any.py` from the [Releases](https://github.com/FleeceFlames/Orca_Print_Advisor/releases) page.
2. Place it in your OrcaSlicer plugins folder (or load it via **Plugins → Load Local Plugin**).
3. Restart or reload plugins and open the **Print Advisor** tab.

---

## Contributing a Translation

Want to translate **Print Advisor** into your language? All UI and rule text lives in a single `TRANSLATIONS` dictionary near the top of `print_advisor_any.py`:

1. Open `print_advisor_any.py` and copy the `"en": { ... }` block inside `TRANSLATIONS`.
2. Paste it right below `"en"`, change the key to your language code (for example `"ru"` or `"pt"`), and set `"_name"` to your language's native name (for example `"Русский"`).
3. Translate the text values (keep `{placeholders}` like `{count}` or `{mm2}` unchanged) and open a Pull Request—the top-bar language selector detects new languages automatically and falls back to English for any missing keys.

---

## Changelog

- **v1.0.2:**
  - **Community Translation Architecture & Language Selector:** Centralized all UI and rule strings into a top-level `TRANSLATIONS` dictionary with automatic English fallback (`tr()`) and an auto-populated top-bar language dropdown.
  - **Variable Layer Height & Organic/Tree Support Conflict Guard:** Automatically suppresses Variable Layer Height recommendations when supports are enabled, adds a slicer-conflict guardrail if both are active, and displays an amber `⚠` warning on the manual bottom-fillets card so users don't combine VLH with Organic/Tree supports.
- **v1.0.1:**
  - **Smooth Surface Finish Modifier (All Filaments):** Added a persistent cross-profile `Smooth` toggle supporting 14 filament families (`PLA`, `PLA+`, `Silk PLA`, `Matte/Wood/PHA`, `PVB`, `PETG/PCTG`, `CF/GF` composites, `ABS/ASA/HIPS`, `PC`, `PA/Nylon`, `TPU/TPE`, foaming `Aero`, and soluble `PVA/BVOH`), downward-only speed/acceleration clamping, `Inner/Outer/Inner` wall order, conditional scarf joint seams, and a beginner drying/calibration guide.
  - **Opt-In Bridge Support Fix & Active Support Detection:** Added an opt-in breakaway `Tree (Auto) · Organic` support fix for floating horizontal bridges (`>85°`) with material-tuned Top Z distance, and auto-clears the warning once supports are active.
  - **Live Plate `.3mf` & `report.txt` Export:** Unlocked **Download Optimized .3mf (`*_advised.3mf`)** and **Download Report.txt (`*_report.txt`)** in Live Plate mode once fixes are applied or manual steps are marked done, stripping `.stl`/mesh extensions from filenames and registering modified keys in `different_settings_to_system[0]`.
- **v1.0.0:** Initial release.

---

## Bugs, Feedback & Plugin Help

Found a bug in the plugin, hit a weird `.3mf` parsing issue, or have an idea for a new check? Feel free to leave a comment on [Orca Cloud](https://cloud.orcaslicer.com/p/3b308f5ef16a) or open an issue on [GitHub Issues](https://github.com/FleeceFlames/Orca_Print_Advisor/issues) — just keep it related to the plugin itself. I check in whenever I have some free time and will gladly take a look!

---

## License

MIT License — see [LICENSE](https://github.com/FleeceFlames/Orca_Print_Advisor/blob/main/LICENSE).
