import re
from dataclasses import dataclass

from .config import ZotliteError

try:
    import pymupdf
except ImportError:
    pymupdf = None

# Join words hyphenated across line breaks and expand ligatures (ﬁ -> fi) so they match plain text
TEXT_FLAGS = (pymupdf.TEXTFLAGS_TEXT | pymupdf.TEXT_DEHYPHENATE) & ~pymupdf.TEXT_PRESERVE_LIGATURES if pymupdf else 0


@dataclass(frozen=True)
class Match:
    page  : int   # 1-based page number
    text  : str   # whitespace-normalized text of the whole page
    start : int
    end   : int

    @property
    def hit(self):
        return self.text[self.start:self.end]

    def context(self, before=40, after=None):
        """(pre, hit, post) around the match, trimmed to whole words and marked with … when cut."""
        after = before if after is None else after

        lo  = max(0, self.start - before)
        hi  = min(len(self.text), self.end + after)
        pre = self.text[lo:self.start]
        post = self.text[self.end:hi]

        if lo > 0:
            pre = "…" + (pre.split(" ", 1)[1] if " " in pre else pre)

        if hi < len(self.text):
            post = (post.rsplit(" ", 1)[0] if " " in post else post) + "…"

        return pre, self.hit, post


class PdfText:
    """The extracted, searchable text of a PDF, one string per page."""

    def __init__(self, path):
        if pymupdf is None:
            raise ZotliteError("PyMuPDF is required for PDF search. Reinstall with: pipx install --force .")

        pymupdf.TOOLS.mupdf_display_errors(False)
        pymupdf.TOOLS.mupdf_display_warnings(False)

        self.path = path

        try:
            with pymupdf.open(path) as doc:
                if doc.needs_pass and not doc.authenticate(""):
                    raise ZotliteError(f"{path.name} is password protected.")

                self.pages = [self._normalize(page.get_text(flags=TEXT_FLAGS)) for page in doc]

        except RuntimeError as e:
            raise ZotliteError(f"Could not read {path.name}: {e}")

    @property
    def has_text(self):
        return any(self.pages)

    def search(self, pattern, ignore_case=False, regex=False):
        if not regex:
            # Any run of whitespace in the query matches any run of whitespace in the PDF
            pattern = r"\s+".join(re.escape(word) for word in pattern.split())

        try:
            rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)

        except re.error as e:
            raise ZotliteError(f"Invalid regular expression: {e}")

        matches = []

        for page_no, text in enumerate(self.pages, start=1):
            for m in rx.finditer(text):
                if m.end() > m.start():
                    matches.append(Match(page=page_no, text=text, start=m.start(), end=m.end()))

        return matches

    @staticmethod
    def _normalize(text):
        # MuPDF only dehyphenates some layouts; also join "hyphen-\nated" between lowercase letters
        text = re.sub(r"(?<=[a-z])-[ \t]*\n[ \t]*(?=[a-z])", "", text)

        return re.sub(r"\s+", " ", text).strip()
