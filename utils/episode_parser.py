"""
Extract episode numbers from filenames.

Uses **anitopy** (best for anime) with **guessit** as fallback, and a
simple regex as a last resort.
"""

import logging
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

try:
    import anitopy
    _HAS_ANITOPY = True
except ImportError:
    _HAS_ANITOPY = False

try:
    from guessit import guessit
    _HAS_GUESSIT = True
except ImportError:
    _HAS_GUESSIT = False

# Common patterns: E08, EP08, Episode 08, - 08, etc.
_EP_RE = re.compile(
    r"(?:"
    r"S\d{1,2}E(\d{1,4})"         # S01E08
    r"|(?:^|[\s\-_])E(\d{1,4})\b" # E08
    r"|EP\.?(\d{1,4})\b"          # EP08 / EP.08
    r"|Episode[\s._-]?(\d{1,4})"  # Episode 08
    r"|\s-\s(\d{2,4})(?:\s|$|\[)"  # " - 08 " or " - 08["
    r")",
    re.IGNORECASE,
)


def extract_episode_number(filename: str) -> Optional[int]:
    """
    Best-effort episode extraction.

    Returns the episode number as ``int``, or ``None`` if nothing found.
    For a filename like
    ``[AniDL] The Fable - S01E08 [Web 1080p x265 10Bit][Opus][VARYG].mkv``
    this returns ``8``.
    """
    stem = Path(filename).stem

    # 1. anitopy (anime-specific, best quality)
    if _HAS_ANITOPY:
        try:
            data = anitopy.parse(stem)
            ep = data.get("episode_number")
            if ep is not None:
                try:
                    return int(ep)
                except (ValueError, TypeError):
                    pass
        except Exception:
            pass

    # 2. guessit (general media)
    if _HAS_GUESSIT:
        try:
            data = guessit(stem)
            ep = data.get("episode")
            if ep is not None:
                if isinstance(ep, list):
                    ep = ep[0]
                try:
                    return int(ep)
                except (ValueError, TypeError):
                    pass
        except Exception:
            pass

    # 3. Regex fallback
    m = _EP_RE.search(stem)
    if m:
        for g in m.groups():
            if g is not None:
                try:
                    return int(g)
                except ValueError:
                    pass

    return None
