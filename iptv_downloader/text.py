"""Text helpers: Arabic shaping for Tk on X11 and filesystem-safe names."""

# Optional Arabic shaping and BiDi support for X11/Tkinter
try:
    import arabic_reshaper
    from bidi.algorithm import get_display

    HAS_ARABIC_SUPPORT = True
except ImportError:
    HAS_ARABIC_SUPPORT = False


def fix_text(text):
    """Reshapes Arabic letters and applies Right-to-Left BiDi ordering for Tkinter on X11."""
    if not text:
        return ""
    text_str = str(text)
    if not HAS_ARABIC_SUPPORT:
        return text_str
    if any("؀" <= ch <= "ۿ" or "ݐ" <= ch <= "ݿ" for ch in text_str):
        try:
            reshaped = arabic_reshaper.reshape(text_str)
            return get_display(reshaped)
        except Exception:
            return text_str
    return text_str


def sanitize_filename(name):
    """Sanitizes strings for safe filesystem directory and file naming while preserving Arabic/English."""
    cleaned = "".join(c if c.isalnum() or c in " ._-[]" else "_" for c in str(name)).strip()
    return cleaned or "Untitled"
