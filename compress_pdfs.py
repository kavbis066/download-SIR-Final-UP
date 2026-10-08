#!/usr/bin/env python3
"""Lossless PDF shrinker for the roll PDFs.

The PDFs hold one RGB scan per page but the OCR only ever uses the grayscale of it. This rewrites every
page as a grayscale PNG-compressed image: the grayscale pixels the OCR sees are IDENTICAL (checked for every
page of every file before the result is kept), and the files come out roughly 2-5x smaller.

    python compress_pdfs.py downloads --out downloads_small            # whole tree, same sub-folder layout
    python compress_pdfs.py downloads --out downloads_small --workers 4
    python compress_pdfs.py downloads --in-place                       # replace originals (only after the check passes)
"""
import argparse, concurrent.futures as cf, os, pathlib, sys, tempfile
import cv2, fitz, numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ocr_extract as base


def shrink(src: str, dst: str):
    doc = fitz.open(src)
    out = fitz.open()
    for pg in doc:
        imgs = pg.get_images(full=True)
        newp = out.new_page(width=pg.rect.width, height=pg.rect.height)
        if len(imgs) != 1:                                   # unusual page: keep it exactly as it is
            out.delete_page(-1)
            out.insert_pdf(doc, from_page=pg.number, to_page=pg.number)
            continue
        pix = fitz.Pixmap(doc, imgs[0][0])
        if pix.n - pix.alpha >= 4:
            pix = fitz.Pixmap(fitz.csRGB, pix)
        a = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)
        g = a[:, :, 0] if pix.n - pix.alpha == 1 else cv2.cvtColor(np.ascontiguousarray(a[:, :, :3]), cv2.COLOR_RGB2GRAY)
        ok, png = cv2.imencode(".png", np.ascontiguousarray(g), [cv2.IMWRITE_PNG_COMPRESSION, 9])
        newp.insert_image(newp.rect, stream=png.tobytes())
    out.save(dst, deflate=True, garbage=4, clean=True)
    out.close(); doc.close()


def identical(a: str, b: str) -> bool:
    ga, gb = list(base.page_images(a)), list(base.page_images(b))
    return len(ga) == len(gb) and all(x[1].shape == y[1].shape and np.array_equal(x[1], y[1]) for x, y in zip(ga, gb))


def job(args):
    src, dst, in_place = args
    tmp = dst + ".tmp.pdf"
    try:
        shrink(src, tmp)
        if not identical(src, tmp):
            os.remove(tmp)
            return src, os.path.getsize(src), None, "pixels differ -- original kept"
        before, after = os.path.getsize(src), os.path.getsize(tmp)
        if after >= before:
            os.remove(tmp)
            return src, before, before, "not smaller -- original kept"
        os.replace(tmp, dst)
        return src, before, after, "ok"
    except Exception as exc:
        if os.path.exists(tmp):
            os.remove(tmp)
        return src, os.path.getsize(src), None, f"failed: {exc}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root")
    ap.add_argument("--out", help="output root (same sub-folder layout)")
    ap.add_argument("--in-place", action="store_true")
    ap.add_argument("--workers", type=int, default=2)
    a = ap.parse_args()
    if not a.out and not a.in_place:
        ap.error("give --out DIR or --in-place")
    root = pathlib.Path(a.root)
    jobs = []
    for p in sorted(root.rglob("*.pdf")):
        dst = p if a.in_place else pathlib.Path(a.out) / p.relative_to(root)
        dst.parent.mkdir(parents=True, exist_ok=True)
        jobs.append((str(p), str(dst), a.in_place))
    tb = ta = 0
    with cf.ProcessPoolExecutor(a.workers) as ex:
        for n, (src, b, af, msg) in enumerate(ex.map(job, jobs), 1):
            tb += b; ta += af or b
            print(f"[{n}/{len(jobs)}] {pathlib.Path(src).name}: {b/1e6:.1f} MB -> {(af or b)/1e6:.1f} MB  {msg}", flush=True)
            if msg != "ok" and not a.in_place:           # keep the output tree complete
                d = pathlib.Path(a.out) / pathlib.Path(src).relative_to(root)
                if not d.exists():
                    import shutil; shutil.copy2(src, d)
    print(f"Total {tb/1e9:.2f} GB -> {ta/1e9:.2f} GB ({100*(1-ta/max(tb,1)):.0f}% saved)")


if __name__ == "__main__":
    main()