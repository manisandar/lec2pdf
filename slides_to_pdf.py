#!/usr/bin/env python3
"""
lec2pdf — Convert lecture slide photos (HEIC/JPG/JPEG) into a clean, cropped PDF.

Uses OpenCV-based document scanner detection (Canny edges + contour finding +
perspective warp) to handle photos taken at any angle or rotation.  Falls back
to a brightness-based crop if no clear slide boundary is found.

Robust orientation: every image is processed at all four rotations (0°, 90°,
180°, 270°) and the best result is selected automatically — using OCR
(pytesseract) when installed, or a text-structure heuristic otherwise.

Usage:  python3 slides_to_pdf.py <photo_folder> [output.pdf]
Docs:   See README.md for full instructions and examples.
"""

from __future__ import annotations

import sys
import os
import glob
import subprocess
import tempfile
import shutil
import argparse
from typing import List, Optional, Tuple


# ── Constants ─────────────────────────────────────────────────────────────────

# Width used when down-scaling images for contour / edge detection
WORK_WIDTH: int = 1200

# A slide contour must cover at least this fraction of the working frame
MIN_SLIDE_AREA_FRACTION: float = 0.10

# Brightness thresholds tried (descending) in pass-1 contour search
BRIGHT_THRESHOLDS: List[int] = [200, 180, 160, 140]

# (low, high) Canny threshold pairs tried in pass-2 fallback search
CANNY_PARAMS: List[Tuple[int, int]] = [(0, 50), (30, 100), (50, 150), (10, 80)]

# Epsilon factors tried when approximating a contour to a quadrilateral
POLY_EPS_FACTORS: List[float] = [0.02, 0.03, 0.04, 0.05]

# Thumbnail size used for fast orientation scoring (width, height)
SCORE_THUMB: Tuple[int, int] = (800, 600)

# Rotation angles applied to each input image before detection
ROTATION_ANGLES: List[int] = [0, 90, 180, 270]

# Whether pytesseract was successfully imported (set by ensure_deps)
_OCR_AVAILABLE: bool = False


# ── Dependency check ──────────────────────────────────────────────────────────

def ensure_deps() -> None:
    """Install Pillow, numpy and opencv-python-headless if not already present.

    Also probes for pytesseract (optional — used for orientation scoring).
    Does NOT install tesseract; only the Python binding is checked.
    """
    global _OCR_AVAILABLE

    missing: List[str] = []
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        missing.append("pillow")
    try:
        import numpy  # noqa: F401
    except ImportError:
        missing.append("numpy")
    try:
        import cv2  # noqa: F401
    except ImportError:
        missing.append("opencv-python-headless")

    if missing:
        print(f" Installing required libraries ({', '.join(missing)})...")
        flags = ["--quiet", "--user"]
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--break-system-packages"]
            + flags + missing,
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            subprocess.run(
                [sys.executable, "-m", "pip", "install"] + flags + missing,
                capture_output=True, text=True,
            )

        import site
        sys.path.insert(0, site.getusersitepackages())
        try:
            from PIL import Image  # noqa: F401
            import numpy  # noqa: F401
            import cv2  # noqa: F401
            print(" Libraries ready.\n")
        except ImportError as exc:
            print(f" Could not install dependencies: {exc}")
            print("   Please run:  pip3 install pillow numpy opencv-python-headless")
            sys.exit(1)

    # Probe for optional OCR support (pytesseract + tesseract binary)
    try:
        import pytesseract
        pytesseract.get_tesseract_version()   # raises if binary missing
        _OCR_AVAILABLE = True
        print(" OCR (pytesseract) detected — using it for orientation scoring.\n")
    except Exception:
        _OCR_AVAILABLE = False


# ── Image preparation (HEIC → JPEG via sips, JPG/JPEG pass-through) ──────────

def prepare_images(all_files: List[str], tmp_dir: str) -> List[str]:
    """Convert HEIC files to JPEG via macOS *sips*; JPEG files pass through.

    Returns a sorted list of JPEG paths ready for processing.
    """
    JPEG_EXTS = {".jpg", ".jpeg"}
    HEIC_EXTS = {".heic"}

    heic_files = [f for f in all_files if os.path.splitext(f)[1].lower() in HEIC_EXTS]
    jpeg_files = [f for f in all_files if os.path.splitext(f)[1].lower() in JPEG_EXTS]
    ready_files: List[str] = list(jpeg_files)

    if heic_files:
        print(f" Converting {len(heic_files)} HEIC photo(s) to JPEG...")
        for i, heic in enumerate(heic_files):
            name = os.path.splitext(os.path.basename(heic))[0] + ".jpg"
            out = os.path.join(tmp_dir, name)
            subprocess.run(
                [
                    "sips", "-s", "format", "jpeg",
                    "-s", "formatOptions", "85",
                    heic, "--out", out,
                ],
                capture_output=True, text=True,
            )
            if os.path.exists(out):
                ready_files.append(out)
            else:
                print(f"  Failed to convert: {os.path.basename(heic)}")

            if (i + 1) % 10 == 0 or (i + 1) == len(heic_files):
                print(f"  [{i + 1}/{len(heic_files)}] HEIC converted")
        print(" HEIC conversion done.\n")

    if jpeg_files:
        print(f" {len(jpeg_files)} JPG/JPEG file(s) will be used directly.\n")

    ready_files.sort(key=lambda p: os.path.basename(p).lower())
    return ready_files


# ── Perspective transform helpers ─────────────────────────────────────────────

def _order_points(pts) -> "np.ndarray":
    """Order four (x, y) points as: top-left, top-right, bottom-right, bottom-left.

    Based on coordinate sums and differences — works regardless of the input order.
    """
    import numpy as np

    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    diff = np.diff(pts, axis=1)

    rect[0] = pts[np.argmin(s)]     # top-left     → smallest x+y
    rect[2] = pts[np.argmax(s)]     # bottom-right → largest  x+y
    rect[1] = pts[np.argmin(diff)]  # top-right    → smallest x-y
    rect[3] = pts[np.argmax(diff)]  # bottom-left  → largest  x-y
    return rect


def _four_point_transform(image, pts) -> "np.ndarray":
    """Warp the quadrilateral defined by *pts* into a clean rectangular image.

    All maths are inlined — no external module required.
    """
    import numpy as np
    import cv2

    rect = _order_points(pts)
    tl, tr, br, bl = rect

    width_top    = np.linalg.norm(tr - tl)
    width_bottom = np.linalg.norm(br - bl)
    dst_w = int(max(width_top, width_bottom))

    height_left  = np.linalg.norm(bl - tl)
    height_right = np.linalg.norm(br - tr)
    dst_h = int(max(height_left, height_right))

    dst = np.array(
        [[0, 0], [dst_w - 1, 0], [dst_w - 1, dst_h - 1], [0, dst_h - 1]],
        dtype="float32",
    )

    M = cv2.getPerspectiveTransform(rect, dst)
    warped = cv2.warpPerspective(image, M, (dst_w, dst_h))
    return warped


# ── EXIF orientation correction ───────────────────────────────────────────────

def _fix_exif_orientation(img_pil) -> "PIL.Image.Image":
    """Apply EXIF rotation tags so pixels match visual orientation.

    *sips* preserves EXIF tags but does not bake them into pixel data, so
    this step is still required after HEIC → JPEG conversion.
    """
    from PIL import ImageOps
    return ImageOps.exif_transpose(img_pil)


# ── Slide contour detection ───────────────────────────────────────────────────

def _find_slide_contour(
    work_gray, work_shape: Tuple[int, int]
) -> Optional["np.ndarray"]:
    """Locate the largest bright quadrilateral in *work_gray*.

    Targets the white slide content area, not the dark TV bezel.

    Pass 1 — brightness threshold → morphology → contour approximation.
    Pass 2 — Canny edges → contour search (dark-border / projector fallback).

    Returns a (4, 2) float32 array in working-image coordinates, or None.
    """
    import cv2
    import numpy as np

    wh, ww = work_shape
    min_area = wh * ww * MIN_SLIDE_AREA_FRACTION

    # ── Pass 1: bright region thresholding ───────────────────────────────────
    for thresh_val in BRIGHT_THRESHOLDS:
        _, bright_mask = cv2.threshold(
            work_gray, thresh_val, 255, cv2.THRESH_BINARY
        )
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
        bright_mask = cv2.morphologyEx(bright_mask, cv2.MORPH_CLOSE, kernel)
        bright_mask = cv2.morphologyEx(bright_mask, cv2.MORPH_OPEN,  kernel)

        contours, _ = cv2.findContours(
            bright_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        if not contours:
            continue

        largest = max(contours, key=cv2.contourArea)
        if cv2.contourArea(largest) < min_area:
            continue

        peri = cv2.arcLength(largest, True)
        for eps in POLY_EPS_FACTORS:
            approx = cv2.approxPolyDP(largest, eps * peri, True)
            if len(approx) == 4:
                return approx.reshape(4, 2).astype("float32")

        # Fall back to minimum-area bounding rectangle
        rect = cv2.minAreaRect(largest)
        box  = cv2.boxPoints(rect).astype("float32")
        return box

    # ── Pass 2: Canny edge detection ─────────────────────────────────────────
    blurred = cv2.GaussianBlur(work_gray, (5, 5), 0)
    dilate_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))

    for lo, hi in CANNY_PARAMS:
        edged = cv2.Canny(blurred, lo, hi)
        edged = cv2.dilate(edged, dilate_kernel, iterations=1)

        contours, _ = cv2.findContours(
            edged, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
        )
        contours = sorted(contours, key=cv2.contourArea, reverse=True)

        for c in contours:
            if cv2.contourArea(c) < min_area:
                break
            peri   = cv2.arcLength(c, True)
            approx = cv2.approxPolyDP(c, 0.02 * peri, True)
            if len(approx) == 4:
                return approx.reshape(4, 2).astype("float32")

    return None


# ── Single-rotation processing ────────────────────────────────────────────────

def _process_single_rotation(img_pil) -> "PIL.Image.Image":
    """Run the scanner pipeline on *img_pil* (already at the desired rotation).

    Returns the best cropped result for this orientation:
    - Perspective-warped crop if a quadrilateral is found.
    - Brightness-based crop otherwise.

    NOTE: this function makes NO assumption about which way is "up"; the
    caller is responsible for choosing the correct rotation afterward.
    """
    import cv2
    import numpy as np
    from PIL import Image

    cv_img = cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)
    orig   = cv_img.copy()
    oh, ow = orig.shape[:2]

    # Down-scale for fast detection
    scale = WORK_WIDTH / ow if ow > WORK_WIDTH else 1.0
    work  = cv2.resize(cv_img, (int(ow * scale), int(oh * scale)))

    work_gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
    wh, ww    = work_gray.shape

    target = _find_slide_contour(work_gray, (wh, ww))

    if target is not None:
        # Scale contour points back to original resolution and warp
        pts    = (target / scale).astype("float32")
        warped = _four_point_transform(orig, pts)
        return Image.fromarray(cv2.cvtColor(warped, cv2.COLOR_BGR2RGB))

    # Scanner detection failed → use brightness-based crop
    return _brightness_crop(img_pil)


# ── Orientation scoring ───────────────────────────────────────────────────────

def _orientation_score_ocr(candidate_pil) -> float:
    """Score a candidate using OCR high-confidence word count (higher = better).

    Uses pytesseract's detailed output (confidence per word) and counts only
    words that:
      - have a Tesseract confidence score above 60 / 100, and
      - are at least 2 characters long (filters single-char noise).

    This is more robust than raw character count because sideways images often
    produce long strings of garbled characters that fool a simple len() check.

    Falls back to average confidence when no high-confidence words are found,
    so images with small/blurry text still produce a meaningful signal.

    Returns 0.0 if OCR fails for any reason.
    """
    try:
        import pytesseract
        thumb = candidate_pil.copy()
        thumb.thumbnail(SCORE_THUMB)
        data = pytesseract.image_to_data(
            thumb, config="--psm 6", output_type=pytesseract.Output.DICT
        )
        confs  = [int(c) for c in data["conf"] if str(c) != "-1" and int(c) >= 0]
        words  = [
            w for w, c in zip(data["text"], data["conf"])
            if str(c) != "-1" and int(c) > 60 and len(str(w).strip()) >= 2
        ]
        # Primary: number of high-confidence words (scaled up so it dominates
        # when the image has readable text)
        word_score = float(len(words)) * 10.0
        # Secondary: average confidence across all detected boxes
        avg_conf   = sum(confs) / len(confs) if confs else 0.0
        return word_score + avg_conf * 0.1
    except Exception:
        return 0.0


def _orientation_score_heuristic(candidate_pil) -> float:
    """Score a candidate using image structure when OCR is unavailable.

    Combines four complementary signals:

    1. **Gradient-direction dominance** (primary, distinguishes 0°/180° from
       90°/270°): in a text image, Sobel-X energy (horizontal edges —
       tops/bottoms of character strokes) should dominate over Sobel-Y energy.
       Rotating 90° swaps the two, giving a large negative score.

    2. **Aspect-ratio preference**: lecture slides are almost always landscape
       (wider than tall).  A portrait crop is penalised.

    3. **Horizontal-line variance** (secondary): dark pixels per row show high
       variance when text is in horizontal lines (correct orientation) and low
       variance when rotated 90°/270°.

    4. **Top-bias** (tiebreaker between 0° and 180°): slide titles appear near
       the top.  Compare dark-pixel density of the top 25 % vs the bottom 25 %.
       A correctly-oriented slide has slightly more ink at the top.
    """
    import numpy as np
    try:
        import cv2
        _cv2_ok = True
    except ImportError:
        _cv2_ok = False

    thumb = candidate_pil.copy()
    thumb.thumbnail(SCORE_THUMB)
    gray8 = np.array(thumb.convert("L"), dtype=np.uint8)
    gray  = gray8.astype(float)
    h, w  = gray.shape

    # ── Signal 1: gradient-direction dominance ────────────────────────────────
    if _cv2_ok and h > 4 and w > 4:
        sx = cv2.Sobel(gray8, cv2.CV_64F, 1, 0, ksize=3)  # horizontal edges
        sy = cv2.Sobel(gray8, cv2.CV_64F, 0, 1, ksize=3)  # vertical edges
        energy_x = float(np.mean(sx ** 2))
        energy_y = float(np.mean(sy ** 2))
        total_e  = energy_x + energy_y + 1e-9
        # Positive when horizontal edges dominate (text rows)
        grad_score = (energy_x - energy_y) / total_e  # range [-1, +1]
    else:
        grad_score = 0.0

    # ── Signal 2: aspect-ratio preference ────────────────────────────────────
    # Slides are landscape; prefer w > h.  Score in [-1, +1].
    aspect_score = (w - h) / max(w + h, 1)

    # ── Signal 3: horizontal-line variance ───────────────────────────────────
    dark_mask = gray < 128
    row_sums  = dark_mask.sum(axis=1).astype(float)
    proj_var  = float(np.var(row_sums))
    norm_var  = proj_var / max(h * w, 1)          # normalise by image area

    # ── Signal 4: top-bias tiebreaker ────────────────────────────────────────
    q = max(int(h * 0.25), 1)
    top_density = float(dark_mask[:q, :].mean())  if h > 2 * q else 0.0
    bot_density = float(dark_mask[-q:, :].mean()) if h > 2 * q else 0.0
    top_bias    = top_density - bot_density        # positive → more ink at top

    # ── Combine ───────────────────────────────────────────────────────────────
    # Weights chosen so signal 1 (gradient) dominates landscape/portrait split,
    # signal 2 (aspect) reinforces geometry, signal 3 adds text-line evidence,
    # signal 4 breaks 0° vs 180° ties.
    score = (grad_score   * 2.0
             + aspect_score * 3.0
             + norm_var    * 100.0
             + top_bias    * 0.5)
    return score


def _orientation_score(candidate_pil) -> float:
    """Return an orientation quality score for *candidate_pil*.

    When OCR is available the OCR character count is used as the primary
    signal.  If all four candidates return a near-zero OCR score (sparse or
    non-text images), the heuristic is used instead so that geometry-based
    signals can still pick the right orientation.
    Without OCR only the heuristic is used.
    A higher score means more likely correctly oriented.
    """
    heuristic = _orientation_score_heuristic(candidate_pil)
    if _OCR_AVAILABLE:
        ocr_score = _orientation_score_ocr(candidate_pil)
        # Blend: OCR dominates when it has useful signal, otherwise fall back
        # to heuristic.  The caller (_select_best_orientation) calls us once
        # per candidate, so we don't know the other scores here; instead we
        # return a combined value and let the max() in the caller decide.
        # Scale OCR chars to be comparable to heuristic range (~0-10).
        return ocr_score * 0.05 + heuristic
    return heuristic


# ── Rotation pipeline ─────────────────────────────────────────────────────────

def _generate_rotations(img_pil) -> List[Tuple[int, "PIL.Image.Image"]]:
    """Return the image at all four cardinal rotations.

    Returns a list of (angle_degrees, rotated_image) tuples.
    PIL rotates counter-clockwise; expand=True keeps the full image.
    """
    from PIL import Image
    return [
        (angle, img_pil.rotate(angle, expand=True))
        for angle in ROTATION_ANGLES
    ]


def _select_best_orientation(
    candidates: List[Tuple[int, "PIL.Image.Image"]]
) -> "PIL.Image.Image":
    """Score each candidate and return the one with the highest score.

    *candidates* is a list of (angle, processed_image) pairs produced by
    running _process_single_rotation on every rotation of the input.
    """
    best_img: Optional["PIL.Image.Image"] = None
    best_score: float = -1.0

    for angle, img in candidates:
        score = _orientation_score(img)
        if score > best_score:
            best_score = score
            best_img   = img

    # Guaranteed non-None because candidates is never empty
    assert best_img is not None
    return best_img


def _auto_rotate(img_pil) -> "PIL.Image.Image":
    """Process all four rotations of *img_pil* and return the best crop.

    Pipeline per rotation:
      1. Rotate the input image.
      2. Run scanner detection → perspective warp (or brightness fallback).
      3. Score the result.
    Then return the highest-scoring candidate.
    """
    rotations  = _generate_rotations(img_pil)
    candidates: List[Tuple[int, "PIL.Image.Image"]] = [
        (angle, _process_single_rotation(rotated))
        for angle, rotated in rotations
    ]
    return _select_best_orientation(candidates)


# ── Brightness-based fallback crop ────────────────────────────────────────────

def _largest_contiguous_range(
    indices, max_gap: int = 20
) -> Tuple[Optional[int], Optional[int]]:
    """Return (start, end) of the longest run in a sorted index array."""
    import numpy as np

    if not len(indices):
        return None, None
    diffs    = np.diff(indices)
    splits   = np.where(diffs > max_gap)[0] + 1
    segments = np.split(indices, splits)
    best     = max(segments, key=len)
    return int(best[0]), int(best[-1])


def _brightness_crop(img) -> "PIL.Image.Image":
    """Crop *img* to the slide area using brightness-density analysis.

    Two modes:
      - Bright images  (median > 155): printed paper — trim dark borders.
      - Dark images    (median ≤ 155): screen/projector — find bright rectangle.
    """
    from PIL import ImageFilter
    import numpy as np

    small = img.copy()
    small.thumbnail((600, 800))
    sx = img.width  / small.width
    sy = img.height / small.height

    blurred = small.filter(ImageFilter.GaussianBlur(2))
    gray    = np.array(blurred.convert("L"), dtype=float)
    h, w    = gray.shape
    median  = float(np.median(gray))
    pad: int = 12

    if median > 155:
        # Bright image: find columns/rows that are mostly light (slide content)
        dark             = gray < 80
        col_dark_density = dark.sum(axis=0) / h
        row_dark_density = dark.sum(axis=1) / w
        slide_cols = np.where(col_dark_density < 0.40)[0]
        slide_rows = np.where(row_dark_density < 0.40)[0]
        if len(slide_cols) < 5 or len(slide_rows) < 5:
            return img
        c_min, c_max = _largest_contiguous_range(slide_cols, max_gap=30)
        r_min, r_max = _largest_contiguous_range(slide_rows, max_gap=30)
    else:
        # Dark image: find the bright slide rectangle
        bright              = gray > 195
        col_bright_density  = bright.sum(axis=0) / h
        row_bright_density  = bright.sum(axis=1) / w
        slide_cols = np.where(col_bright_density > 0.08)[0]
        slide_rows = np.where(row_bright_density > 0.08)[0]
        if len(slide_cols) < 5 or len(slide_rows) < 5:
            return img
        c_min, c_max = _largest_contiguous_range(slide_cols, max_gap=25)
        r_min, r_max = _largest_contiguous_range(slide_rows, max_gap=25)

    if c_min is None or r_min is None:
        return img

    c_min = max(0,          int(c_min * sx) - pad)
    c_max = min(img.width,  int(c_max * sx) + pad)
    r_min = max(0,          int(r_min * sy) - pad)
    r_max = min(img.height, int(r_max * sy) + pad)
    return img.crop((c_min, r_min, c_max, r_max))


# ── Main crop entry point ─────────────────────────────────────────────────────

def crop_to_slide(img) -> "PIL.Image.Image":
    """Auto-correct orientation and crop *img* to just the slide area.

    Pipeline:
      1. EXIF rotation correction — handles HEIC/iPhone orientation metadata.
      2. Four-rotation sweep — tries 0°/90°/180°/270°, each followed by
         scanner detection (perspective warp) or brightness fallback.
      3. Best-candidate selection — OCR or heuristic picks the correct output.
    """
    # Step 1: apply EXIF orientation so pixels are visually upright
    img = _fix_exif_orientation(img)

    # Steps 2 + 3: try all rotations, return the best-scored crop
    return _auto_rotate(img)


# ── PDF creation ──────────────────────────────────────────────────────────────

def build_pdf(jpg_files: List[str], output_pdf: str) -> None:
    """Crop all images and assemble them into a single multi-page PDF."""
    from PIL import Image

    total = len(jpg_files)
    print(f"  Cropping slides and building PDF ({total} pages)...")

    first_img = None
    rest_imgs: List["PIL.Image.Image"] = []

    for i, f in enumerate(jpg_files):
        raw     = Image.open(f)
        cropped = crop_to_slide(raw).convert("RGB")

        if i == 0:
            first_img = cropped
        else:
            rest_imgs.append(cropped)

        if (i + 1) % 10 == 0 or (i + 1) == total:
            print(
                f"  [{i + 1}/{total}] {os.path.basename(f)} "
                f"→ {cropped.size[0]}×{cropped.size[1]}"
            )

    print("\n Saving PDF...")
    first_img.save(
        output_pdf,
        save_all=True,
        append_images=rest_imgs,
        resolution=150,
    )

    size_mb = os.path.getsize(output_pdf) / 1024 / 1024
    print(f"\nPDF created successfully!")
    print(f"    File   : {output_pdf}")
    print(f"    Pages  : {total}")
    print(f"    Size   : {size_mb:.1f} MB")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert a folder of HEIC/JPG slide photos to a clean cropped PDF.",
        epilog="Example: python3 slides_to_pdf.py ~/Desktop/week05_slides",
    )
    parser.add_argument("folder", help="Path to the folder containing photos")
    parser.add_argument(
        "output",
        nargs="?",
        default=None,
        help="Output PDF filename (default: <folder_name>.pdf inside the folder)",
    )
    args = parser.parse_args()

    folder = os.path.expanduser(args.folder)
    if not os.path.isdir(folder):
        print(f" Folder not found: {folder}")
        sys.exit(1)

    all_files = sorted(
        glob.glob(os.path.join(folder, "*.HEIC"))
        + glob.glob(os.path.join(folder, "*.heic"))
        + glob.glob(os.path.join(folder, "*.JPG"))
        + glob.glob(os.path.join(folder, "*.jpg"))
        + glob.glob(os.path.join(folder, "*.JPEG"))
        + glob.glob(os.path.join(folder, "*.jpeg"))
    )
    if not all_files:
        print(f" No supported photos found in: {folder}")
        print("   Supported formats: HEIC, JPG, JPEG")
        sys.exit(1)

    n_heic = sum(1 for f in all_files if f.lower().endswith(".heic"))
    n_jpeg = len(all_files) - n_heic

    if args.output:
        output_pdf = os.path.expanduser(args.output)
        if not output_pdf.endswith(".pdf"):
            output_pdf += ".pdf"
    else:
        folder_name = os.path.basename(folder.rstrip("/"))
        output_pdf  = os.path.join(folder, f"{folder_name}.pdf")

    print(f"\n  Slides to PDF Converter (Scanner Edition)")
    print(f"{'─' * 46}")
    print(f"    Input  : {folder}")
    if n_heic and n_jpeg:
        print(f"     Photos : {len(all_files)} files  ({n_heic} HEIC + {n_jpeg} JPG/JPEG)")
    elif n_heic:
        print(f"     Photos : {n_heic} HEIC file(s)")
    else:
        print(f"     Photos : {n_jpeg} JPG/JPEG file(s)")
    print(f"    Output : {output_pdf}")
    print(f"{'─' * 46}\n")

    ensure_deps()

    tmp_dir = tempfile.mkdtemp(prefix="slides_pdf_")
    try:
        ready_files = prepare_images(all_files, tmp_dir)
        if not ready_files:
            print(" No images could be prepared.")
            sys.exit(1)

        build_pdf(ready_files, output_pdf)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print(f"\nDone! Open your PDF:")
    print(f"   open \"{output_pdf}\"")


if __name__ == "__main__":
    main()
