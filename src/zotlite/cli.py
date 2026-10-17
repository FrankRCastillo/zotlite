import argparse
import os
import sys

from .config import Config, ZotliteError
from .library import Library
from .storage import Storage

# Exit codes follow grep: 0 = matches/success, 1 = no matches, 2 = error
EXIT_OK, EXIT_NO_MATCH, EXIT_ERROR = 0, 1, 2


class Colors:
    def __init__(self, enabled):
        self.enabled = enabled

    def _wrap(self, code, text):
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def hit(self, text):  return self._wrap("1;31", text)
    def page(self, text): return self._wrap("32", text)

    @classmethod
    def for_stream(cls, mode, stream):
        if mode == 'auto':
            return cls(stream.isatty() and 'NO_COLOR' not in os.environ)

        return cls(mode == 'always')


class Commands:
    """One-off command-line commands. Each `cmd_<name>` returns an exit code."""

    def __init__(self, library, args):
        self.library = library
        self.args    = args

    def run(self):
        return getattr(self, f"cmd_{self.args.command}")()

    def cmd_tui(self):
        from .tui import TUI

        TUI.run(self.library)

        return EXIT_OK

    def cmd_list(self):
        items = sorted(self.library.items, key=lambda x: x.date, reverse=True)

        for item in items:
            print("\t".join([item.date, item.citekey, item.author, item.title]))

        return EXIT_OK

    def cmd_info(self):
        item = self.library.find(self.args.citekey)

        for key_name, val in item.fields.items():
            print(f"{key_name}: {val}")

        attachment = self.library.pdf_attachment(item)
        print(f"pdf: {attachment.filename if attachment else '(none)'}")

        return EXIT_OK

    def cmd_open(self):
        from .viewer import open_pdf

        item = self.library.find(self.args.citekey)
        open_pdf(self.library.pdf_path(item), page=self.args.page)

        return EXIT_OK

    def cmd_grep(self):
        from .pdfsearch import PdfText

        args    = self.args
        pattern = " ".join(args.pattern)

        if not pattern.strip():
            raise ZotliteError("Empty search pattern.")

        item = self.library.find(args.citekey)
        pdf  = PdfText(self.library.pdf_path(item))

        if not pdf.has_text:
            raise ZotliteError(f"{pdf.path.name} has no extractable text (scanned?).")

        matches = pdf.search(pattern, ignore_case=args.ignore_case, regex=args.regex)
        colors  = Colors.for_stream(args.color, sys.stdout)

        if args.count:
            print(len(matches))

        else:
            for m in matches:
                pre, hit, post = m.context(before=args.context)
                print(f"{colors.page(f'p.{m.page}')}: {pre}{colors.hit(hit)}{post}")

        return EXIT_OK if matches else EXIT_NO_MATCH


def build_parser():
    parser = argparse.ArgumentParser( prog="zotlite"
                                    , description="Browse a Zotero library in the terminal, or run one-off commands against it. "
                                                  "With no command, starts the interactive interface."
                                    )
    parser.add_argument("--config", metavar="PATH", help="settings file (default: ~/.config/zotlite/settings.toml)")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    sub.add_parser("tui", help="start the interactive interface (the default)")

    sub.add_parser("list", help="print every item as tab-separated date, citekey, author, title")

    p = sub.add_parser("info", help="print an item's fields")
    p.add_argument("citekey")

    p = sub.add_parser("open", help="open an item's PDF in the viewer")
    p.add_argument("citekey")
    p.add_argument("-p", "--page", type=int, help="jump to this page")

    p = sub.add_parser("grep", help="search the text of an item's PDF")
    p.add_argument("citekey")
    p.add_argument("pattern", nargs="+", help="text to search for (several words are joined with spaces)")
    p.add_argument("-i", "--ignore-case", action="store_true", help="case-insensitive search")
    p.add_argument("-E", "--regex", action="store_true", help="treat the pattern as a Python regular expression")
    p.add_argument("-c", "--count", action="store_true", help="only print the number of matches")
    p.add_argument("-C", "--context", type=int, default=60, metavar="N", help="characters of context on each side (default: 60)")
    p.add_argument("--color", choices=["auto", "always", "never"], default="auto")

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.command = args.command or "tui"

    try:
        config = Config.load(args.config)

        with Storage.open(config.library_path, config.cache_dir) as storage:
            library = Library(storage)

            try:
                return Commands(library, args).run()

            finally:
                library.close()

    except ZotliteError as e:
        print(f"zotlite: {e}", file=sys.stderr)
        return EXIT_ERROR

    except BrokenPipeError:
        # Output piped into e.g. `head`; exit quietly like grep does. Redirect stdout so the
        # interpreter's final flush doesn't raise again.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
