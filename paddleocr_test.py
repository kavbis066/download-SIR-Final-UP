#!/usr/bin/env python3
"""
paddleocr_test.py — Standalone PaddleOCR comparison test.

I could not run this comparison myself: PaddleOCR downloads its models
from HuggingFace/ModelScope/Baidu's model hub on first use, and the
sandbox this session runs in blocks all three of those hosts at the
network level. On your own machine (normal internet access) this should
just work.

This tests PaddleOCR on the exact same two reference cards I used to
validate EasyOCR against Tesseract, so the three-way comparison is
apples-to-apples:
  - card1_clean.png — an ordinary card, ground truth:
      नाम: वासत | पिता का नाम: शौकत | मकान संख्या: 00 | आयु: 68 लिंग: पुरुष
  - cardS71_watermarked.png — a "DELETED"-watermarked card, ground truth:
      S 71 | नाम: फैमिदा | पिता का नाम: कल्लू | मकान संख्या: 21 | आयु: 34 लिंग: महिला

SETUP
-----
    pip install paddlepaddle paddleocr

USAGE
-----
    python paddleocr_test.py card1_clean.png
    python paddleocr_test.py cardS71_watermarked.png

Report back what it prints for both images (text + timing) and I'll fold
the result into the accuracy/speed comparison alongside Tesseract and
EasyOCR.
"""
import sys
import time
from paddleocr import PaddleOCR

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python paddleocr_test.py <image.png>")
        sys.exit(1)

    print("Loading PaddleOCR (lang='hi', Devanagari recognition model)...")
    t0 = time.time()
    ocr = PaddleOCR(
        lang="hi",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
    )
    print(f"  loaded in {time.time()-t0:.1f}s")

    t0 = time.time()
    result = ocr.predict(sys.argv[1])
    dt = time.time() - t0

    texts = result[0]["rec_texts"] if result else []
    scores = result[0]["rec_scores"] if result else []
    print(f"\n{sys.argv[1]}  (took {dt:.2f}s)")
    for t, s in zip(texts, scores):
        print(f"  [{s:.2f}] {t}")