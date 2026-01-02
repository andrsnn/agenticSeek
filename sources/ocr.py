from __future__ import annotations

from typing import Optional


def ocr_image(image_path: str) -> tuple[Optional[str], Optional[str]]:
    """
    Best-effort OCR for a screenshot file.

    Returns: (text, error)
    - If OCR is not available, returns (None, "<reason>")
    - Never raises
    """
    try:
        from PIL import Image  # type: ignore
    except Exception as e:
        return None, f"OCR unavailable (missing Pillow): {e}"
    try:
        import pytesseract  # type: ignore
    except Exception as e:
        return None, f"OCR unavailable (missing pytesseract): {e}"

    try:
        img = Image.open(image_path)
        # Keep it simple: default language; users can extend later.
        text = pytesseract.image_to_string(img)
        text = (text or "").strip()
        return (text if text else ""), None
    except Exception as e:
        return None, f"OCR failed: {e}"
