#!/usr/bin/env python3
"""
lec2pdf — Convert lecture slide photos (HEIC/JPG/JPEG) into a clean, cropped PDF.

Usage:  python3 slides_to_pdf.py <photo_folder> [output.pdf]
Docs:   See README.md for full instructions and examples.
"""

import sys
import os
import glob
import subprocess
import tempfile
import shutil
import argparse


# ── Dependency check ──────────────────────────────────────────────────────────

def ensure_pillow():
    """Install Pillow and numpy if not already present."""
    try:
        from PIL import Image
        import numpy
        return True
    except ImportError:
        pass

    print("📦 Installing required libraries (Pillow + numpy)...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet",
         "--user", "--break-system-packages", "pillow", "numpy"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        # Try without break-system-packages flag
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--user", "pillow", "numpy"],
            capture_output=True, text=True
        )
    # Refresh sys.path so newly installed packages are found
    import site
    sys.path.insert(0, site.getusersitepackages())
    try:
        from PIL import Image
        import numpy
        print("✅ Libraries ready.\n")
        return True
    except ImportError:
        print("❌ Could not install Pillow/numpy. Please run:")
        print("   pip3 install pillow numpy")
        sys.exit(1)


# ── Image preparation (HEIC → JPEG conversion, JPG/JPEG passed through) ──────

def prepare_images(all_files, tmp_dir):
    """
    Prepare images for PDF creation.

    - HEIC files  → converted to JPEG via macOS sips
    - JPG / JPEG files → used directly (no conversion needed)

    Returns a sorted list of JPEG file paths ready for processing.
    """
    JPEG_EXTS = {".jpg", ".jpeg"}
    HEIC_EXTS = {".heic"}

    heic_files  = [f for f in all_files if os.path.splitext(f)[1].lower() in HEIC_EXTS]
    jpeg_files  = [f for f in all_files if os.path.splitext(f)[1].lower() in JPEG_EXTS]
    ready_files = list(jpeg_files)  # JPEGs are used as-is

    if heic_files:
        print(f"🔄 Converting {len(heic_files)} HEIC photo(s) to JPEG...")
        for i, heic in enumerate(heic_files):
            name = os.path.splitext(os.path.basename(heic))[0] + ".jpg"
            out  = os.path.join(tmp_dir, name)
            result = subprocess.run(
                ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "85",
                 heic, "--out", out],
                capture_output=True, text=True
            )
            if os.path.exists(out):
                ready_files.append(out)
            else:
                print(f"  ⚠️  Failed to convert: {os.path.basename(heic)}")

            if (i + 1) % 10 == 0 or (i + 1) == len(heic_files):
                print(f"  [{i+1}/{len(heic_files)}] HEIC converted")
        print(f"✅ HEIC conversion done.\n")

    if jpeg_files:
        print(f"✅ {len(jpeg_files)} JPG/JPEG file(s) will be used directly.\n")

    # Sort combined list by filename so slides stay in order
    ready_files.sort(key=lambda p: os.path.basename(p).lower())
    return ready_files


# ── Slide crop algorithm ──────────────────────────────────────────────────────

def largest_contiguous_range(indices, max_gap=20):
    """Find the start and end of the longest contiguous block in a sorted index array."""
    if not len(indices):
        return None, None
    import numpy as np
    diffs = np.diff(indices)
    splits = np.where(diffs > max_gap)[0] + 1
    segments = np.split(indices, splits)
    best = max(segments, key=len)
    return best[0], best[-1]


def crop_to_slide(img):
    """
    Auto-rotate and crop an image to just the slide area.

    Two modes:
      - Bright images  (median > 155): printed paper slides — find dark borders and remove them
      - Dark images    (median ≤ 155): screen/projector photos — find bright slide rectangle
    """
    from PIL import ImageOps, ImageFilter
    import numpy as np

    # Step 1: apply EXIF rotation
    img = ImageOps.exif_transpose(img)

    # Step 2: work on a small thumbnail for fast analysis
    small = img.copy()
    small.thumbnail((600, 800))
    sx = img.width / small.width
    sy = img.height / small.height

    blurred = small.filter(ImageFilter.GaussianBlur(2))
    gray = np.array(blurred.convert("L"), dtype=float)
    h, w = gray.shape
    median = float(np.median(gray))
    pad = 12

    if median > 155:
        # ── PRINTED PAPER MODE ──
        # The slide fills most of the frame. Find the dark border edges.
        dark = gray < 80
        col_dark_density = dark.sum(axis=0) / h
        row_dark_density = dark.sum(axis=1) / w

        slide_cols = np.where(col_dark_density < 0.40)[0]
        slide_rows = np.where(row_dark_density < 0.40)[0]

        if len(slide_cols) < 5 or len(slide_rows) < 5:
            return img  # can't detect — return full image

        c_min, c_max = largest_contiguous_range(slide_cols, max_gap=30)
        r_min, r_max = largest_contiguous_range(slide_rows, max_gap=30)

    else:
        # ── SCREEN / PROJECTOR MODE ──
        # The slide is a bright white rectangle in a darker classroom photo.
        bright = gray > 195
        col_bright_density = bright.sum(axis=0) / h
        row_bright_density = bright.sum(axis=1) / w

        slide_cols = np.where(col_bright_density > 0.08)[0]
        slide_rows = np.where(row_bright_density > 0.08)[0]

        if len(slide_cols) < 5 or len(slide_rows) < 5:
            return img

        c_min, c_max = largest_contiguous_range(slide_cols, max_gap=25)
        r_min, r_max = largest_contiguous_range(slide_rows, max_gap=25)

    if c_min is None or r_min is None:
        return img

    # Scale crop box back to original image coordinates
    c_min = max(0, int(c_min * sx) - pad)
    c_max = min(img.width, int(c_max * sx) + pad)
    r_min = max(0, int(r_min * sy) - pad)
    r_max = min(img.height, int(r_max * sy) + pad)

    return img.crop((c_min, r_min, c_max, r_max))


# ── PDF creation ──────────────────────────────────────────────────────────────

def build_pdf(jpg_files, output_pdf):
    """Crop all images and save as a single multi-page PDF."""
    from PIL import Image

    total = len(jpg_files)
    print(f"✂️  Cropping slides and building PDF ({total} pages)...")

    first_img = None
    rest_imgs = []

    for i, f in enumerate(jpg_files):
        raw = Image.open(f)
        cropped = crop_to_slide(raw).convert("RGB")

        if i == 0:
            first_img = cropped
        else:
            rest_imgs.append(cropped)

        if (i + 1) % 10 == 0 or (i + 1) == total:
            print(f"  [{i+1}/{total}] {os.path.basename(f)} → {cropped.size[0]}×{cropped.size[1]}")

    print(f"\n💾 Saving PDF...")
    first_img.save(
        output_pdf,
        save_all=True,
        append_images=rest_imgs,
        resolution=150
    )

    size_mb = os.path.getsize(output_pdf) / 1024 / 1024
    print(f"\n✅ PDF created successfully!")
    print(f"   📄 File   : {output_pdf}")
    print(f"   📊 Pages  : {total}")
    print(f"   💾 Size   : {size_mb:.1f} MB")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Convert a folder of HEIC slide photos to a clean cropped PDF.",
        epilog="Example: python3 slides_to_pdf.py ~/Desktop/week05_slides"
    )
    parser.add_argument(
        "folder",
        help="Path to the folder containing HEIC photos"
    )
    parser.add_argument(
        "output",
        nargs="?",
        default=None,
        help="Output PDF filename (default: <folder_name>.pdf saved inside the folder)"
    )
    args = parser.parse_args()

    folder = os.path.expanduser(args.folder)
    if not os.path.isdir(folder):
        print(f"❌ Folder not found: {folder}")
        sys.exit(1)

    # Find all supported photo files (HEIC, JPG, JPEG) — case-insensitive
    all_files = sorted(
        glob.glob(os.path.join(folder, "*.HEIC"))  +
        glob.glob(os.path.join(folder, "*.heic"))  +
        glob.glob(os.path.join(folder, "*.JPG"))   +
        glob.glob(os.path.join(folder, "*.jpg"))   +
        glob.glob(os.path.join(folder, "*.JPEG"))  +
        glob.glob(os.path.join(folder, "*.jpeg"))
    )
    if not all_files:
        print(f"❌ No supported photos found in: {folder}")
        print(f"   Supported formats: HEIC, JPG, JPEG")
        sys.exit(1)

    # Count by type for the summary
    n_heic = sum(1 for f in all_files if f.lower().endswith(".heic"))
    n_jpeg = len(all_files) - n_heic

    # Determine output path
    if args.output:
        output_pdf = os.path.expanduser(args.output)
        if not output_pdf.endswith(".pdf"):
            output_pdf += ".pdf"
    else:
        folder_name = os.path.basename(folder.rstrip("/"))
        output_pdf = os.path.join(folder, f"{folder_name}.pdf")

    print(f"\n🖼️  Slides to PDF Converter")
    print(f"{'─'*40}")
    print(f"   📁 Input  : {folder}")
    if n_heic and n_jpeg:
        print(f"   🖼️  Photos : {len(all_files)} files  ({n_heic} HEIC + {n_jpeg} JPG/JPEG)")
    elif n_heic:
        print(f"   🖼️  Photos : {n_heic} HEIC file(s)")
    else:
        print(f"   🖼️  Photos : {n_jpeg} JPG/JPEG file(s)")
    print(f"   📄 Output : {output_pdf}")
    print(f"{'─'*40}\n")

    # Ensure dependencies
    ensure_pillow()

    # Prepare images: convert HEIC → JPEG, pass JPG/JPEG through as-is
    tmp_dir = tempfile.mkdtemp(prefix="slides_pdf_")
    try:
        ready_files = prepare_images(all_files, tmp_dir)
        if not ready_files:
            print("❌ No images could be prepared.")
            sys.exit(1)

        build_pdf(ready_files, output_pdf)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print(f"\n🎉 Done! Open your PDF:")
    print(f"   open \"{output_pdf}\"")


if __name__ == "__main__":
    main()
