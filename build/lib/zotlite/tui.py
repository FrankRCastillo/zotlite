import curses
import textwrap
import time

from .config import ZotliteError
from .pdfsearch import PdfText
from .viewer import open_pdf

KEYS_ENTER     = [10, 13, curses.KEY_ENTER]
KEYS_BACKSPACE = [8, 127, curses.KEY_BACKSPACE]
KEY_TAB        = 9
KEY_ESCAPE     = 27

SCROLL_UP   = getattr(curses, 'BUTTON4_PRESSED', 524288)
SCROLL_DOWN = getattr(curses, 'BUTTON5_PRESSED', 2097152)

# Color pairs
PAIR_SELECTED = 1
PAIR_STATUS   = 2
PAIR_HEADER   = 3
PAIR_HIT      = 4


def trunc(text, width):
    text = str(text)

    if width <= 1:
        return "." * width

    return (text[:width-1] + "…") if len(text) > width else text.ljust(width)


class Screen:
    """Shared curses helpers: the status line, flash messages and the line-input prompt."""

    def __init__(self, stdscr):
        self.stdscr = stdscr

    @property
    def size(self):
        return self.stdscr.getmaxyx()

    def status(self, text):
        max_y, max_x = self.size
        self.stdscr.addstr(max_y - 1, 0, text[:max_x-1].ljust(max_x - 1), curses.color_pair(PAIR_STATUS))
        self.stdscr.refresh()

    def flash(self, text, ms):
        self.status(text)
        curses.napms(ms)

    def prompt(self, label):
        """Read a line on the status bar. Returns None if cancelled with Escape."""
        max_y, max_x = self.size

        try: curses.curs_set(1)
        except curses.error: pass

        query = ""

        while True:
            text = f"{label}{query}"
            self.stdscr.addstr(max_y - 1, 0, text[:max_x-1].ljust(max_x - 1), curses.color_pair(PAIR_STATUS))
            self.stdscr.move(max_y - 1, min(len(text), max_x - 1))
            self.stdscr.refresh()

            c = self.stdscr.getch()

            if c in KEYS_ENTER:
                break

            elif c == KEY_ESCAPE:
                query = None
                break

            elif c in KEYS_BACKSPACE:
                query = query[:-1]

            elif 32 <= c <= 126:
                query += chr(c)

        try: curses.curs_set(0)
        except curses.error: pass

        return query


class TUI(Screen):
    """The two-pane library browser."""

    SORT_COLUMNS = ['date', 'citekey', 'author', 'title']

    def __init__(self, stdscr, library):
        super().__init__(stdscr)

        self.library = library
        self.items   = list(library.items)
        self.pdfs    = {}  # item id -> PdfText, so repeated greps skip re-extraction

        # Navigation States
        self.active_pane     = 0
        self.item_idx        = 0
        self.item_scroll     = 0
        self.detail_scroll   = 0
        self.last_click_time = 0

        # Sorting States
        self.sort_col = 'date'
        self.sort_rev = True

        # Search States
        self.last_search = ""

        # Dynamic Width States
        self.details_w_pct = 0.33
        self.w_date        = 10
        self.w_key         = 18
        self.w_auth        = 20

        self.apply_sort()

    @classmethod
    def run(cls, library):
        curses.wrapper(lambda stdscr: cls(stdscr, library).loop())

    def setup_curses(self):
        try: curses.curs_set(0)
        except curses.error: pass

        self.stdscr.nodelay(0)
        curses.set_escdelay(25)
        curses.start_color()
        curses.use_default_colors()

        curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)

        curses.init_pair(PAIR_SELECTED, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(PAIR_STATUS,   curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(PAIR_HEADER,   curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(PAIR_HIT,      curses.COLOR_YELLOW, -1)

    @property
    def selected(self):
        return self.items[self.item_idx] if self.items else None

    def apply_sort(self):
        self.items.sort(key=lambda x: str(getattr(x, self.sort_col)).lower(), reverse=self.sort_rev)
        self.item_idx = 0
        self.item_scroll = 0

    def loop(self):
        self.setup_curses()

        while True:
            self.layout()
            self.draw()

            key = self.stdscr.getch()

            if key in [ord('q'), ord('Q')]:
                break

            self.handle_key(key)

    # ---- Drawing ----------------------------------------------------------

    def layout(self):
        self.max_y, self.max_x = self.size

        self.details_w = max(15, int(self.max_x * self.details_w_pct))
        self.list_w = self.max_x - self.details_w - 1
        self.list_h = self.max_y - 2

        total_fixed = self.w_date + self.w_key + self.w_auth + 13

        if total_fixed > self.list_w - 5:
            overshoot = total_fixed - (self.list_w - 5)
            self.w_auth = max(4, self.w_auth - overshoot)

        self.w_title = max(5, self.list_w - (self.w_date + self.w_key + self.w_auth + 13))

        self.detail_lines = []

        if self.items:
            for key_name, val in self.selected.fields.items():
                self.detail_lines.append((f"{key_name.capitalize()}:", True))
                wrapped = textwrap.wrap(val, width=self.details_w - 4)
                for line in wrapped:
                    self.detail_lines.append((f"  {line}", False))
                self.detail_lines.append(("", False))

        self.max_detail_scroll = max(0, len(self.detail_lines) - self.list_h)
        self.detail_scroll = max(0, min(self.detail_scroll, self.max_detail_scroll))

    def arrow(self, col):
        if self.sort_col != col:
            return ""

        return "▼" if self.sort_rev else "▲"

    def draw(self):
        stdscr = self.stdscr
        stdscr.clear()

        list_w, details_w, list_h = self.list_w, self.details_w, self.list_h
        w_date, w_key, w_auth, w_title = self.w_date, self.w_key, self.w_auth, self.w_title

        hdr_attr_left  = curses.color_pair(PAIR_HEADER) | (curses.A_BOLD if self.active_pane == 0 else curses.A_NORMAL)
        hdr_attr_right = curses.color_pair(PAIR_HEADER) | (curses.A_BOLD if self.active_pane == 1 else curses.A_NORMAL)

        header_str = ( f" {trunc('Date'+self.arrow('date'), w_date)}"
                       f" | {trunc('CiteKey'+self.arrow('citekey'), w_key)}"
                       f" | {trunc('Author'+self.arrow('author'), w_auth)}"
                       f" | {trunc('Title'+self.arrow('title'), w_title)}"
                     )

        stdscr.addstr(0, 0, header_str[:list_w].ljust(list_w), hdr_attr_left)
        stdscr.addstr(0, list_w + 1, " ATTRIBUTES ".center(details_w), hdr_attr_right)

        for i in range(1, self.max_y - 1):
            stdscr.addch(i, list_w, curses.ACS_VLINE)

        for i in range(list_h):
            idx = self.item_scroll + i

            if idx < len(self.items):
                it = self.items[idx]
                row_str = f" {trunc(it.date, w_date)} | {trunc(it.citekey, w_key)} | {trunc(it.author, w_auth)} | {trunc(it.title, w_title)}"

                disp_str = row_str[:list_w-1].ljust(list_w)

                if idx == self.item_idx:
                    stdscr.addstr(i + 1, 0, disp_str, curses.color_pair(PAIR_SELECTED) if self.active_pane == 0 else curses.A_REVERSE)
                else:
                    stdscr.addstr(i + 1, 0, disp_str)

        for i in range(list_h):
            idx = self.detail_scroll + i

            if idx < len(self.detail_lines):
                text, is_bold = self.detail_lines[idx]
                attr = curses.A_BOLD if is_bold else curses.A_NORMAL

                if self.active_pane == 1 and text.strip():
                    attr |= curses.color_pair(PAIR_SELECTED)

                stdscr.addstr(i + 1, list_w + 2, text[:details_w-2].ljust(details_w-2), attr)

        self.status(" [/] Search | [n/N] Next | [g] Grep PDF | [TAB] Switch | [S]ort | [1-8] Resize | [ENTER] Open ")

    # ---- Input ------------------------------------------------------------

    def handle_key(self, key):
        if key == curses.KEY_RESIZE:
            return

        elif key == KEY_TAB:
            self.active_pane = 1 - self.active_pane

        elif key in [ord('s'), ord('S')]:
            cols = self.SORT_COLUMNS
            self.sort_col = cols[(cols.index(self.sort_col) + 1) % len(cols)]
            self.apply_sort()

        elif key in [ord('r'), ord('R')]:
            self.sort_rev = not self.sort_rev
            self.apply_sort()

        elif key == ord('/'):
            self.search_field()

        elif key in [ord('n'), ord('N')] and self.last_search:
            self.search_next(1 if key == ord('n') else -1)

        elif key in [ord('g'), ord('G')]:
            self.grep_selected()

        elif key == ord('1'): self.w_date = max(4, self.w_date - 1)
        elif key == ord('2'): self.w_date += 1
        elif key == ord('3'): self.w_key = max(5, self.w_key - 1)
        elif key == ord('4'): self.w_key += 1
        elif key == ord('5'): self.w_auth = max(5, self.w_auth - 1)
        elif key == ord('6'): self.w_auth += 1
        elif key == ord('7'): self.details_w_pct = max(0.15, self.details_w_pct - 0.05)
        elif key == ord('8'): self.details_w_pct = min(0.70, self.details_w_pct + 0.05)

        elif key == curses.KEY_UP:
            if self.active_pane == 0:
                self.move_item(-1)

            elif self.active_pane == 1:
                self.detail_scroll = max(0, self.detail_scroll - 1)

        elif key == curses.KEY_DOWN:
            if self.active_pane == 0:
                self.move_item(1)

            elif self.active_pane == 1:
                self.detail_scroll = min(self.max_detail_scroll, self.detail_scroll + 1)

        elif key == curses.KEY_PPAGE:
            if self.active_pane == 0:
                self.item_idx = max(0, self.item_idx - self.list_h)
                self.item_scroll = max(0, self.item_scroll - self.list_h)
                self.detail_scroll = 0

            elif self.active_pane == 1:
                self.detail_scroll = max(0, self.detail_scroll - self.list_h)

        elif key == curses.KEY_NPAGE:
            if self.active_pane == 0:
                self.item_idx = min(len(self.items) - 1, self.item_idx + self.list_h)
                self.item_scroll = min(max(0, len(self.items) - self.list_h), self.item_scroll + self.list_h)
                self.detail_scroll = 0

            elif self.active_pane == 1:
                self.detail_scroll = min(self.max_detail_scroll, self.detail_scroll + self.list_h)

        elif key in KEYS_ENTER:
            self.open_selected()

        elif key == curses.KEY_MOUSE:
            self.handle_mouse()

    def move_item(self, step):
        new_idx = max(0, min(len(self.items) - 1, self.item_idx + step))

        if new_idx == self.item_idx:
            return

        self.item_idx = new_idx

        if self.item_idx < self.item_scroll:
            self.item_scroll = self.item_idx

        elif self.item_idx >= self.item_scroll + self.list_h:
            self.item_scroll = self.item_idx - self.list_h + 1

        self.detail_scroll = 0

    def handle_mouse(self):
        try:
            _, mouse_x, mouse_y, _, bstate = curses.getmouse()

        except curses.error:
            return

        if bstate & SCROLL_UP or bstate == 65536:
            if mouse_x < self.list_w:
                self.move_item(-1)

            else:
                self.detail_scroll = max(0, self.detail_scroll - 1)

        elif bstate & SCROLL_DOWN or bstate == 2097152:
            if mouse_x < self.list_w:
                self.move_item(1)

            else:
                self.detail_scroll = min(self.max_detail_scroll, self.detail_scroll + 1)

        elif bstate & curses.BUTTON1_PRESSED or bstate & curses.BUTTON1_CLICKED:
            if mouse_y == 0 and mouse_x < self.list_w:
                if mouse_x <= self.w_date + 2: new_col = 'date'
                elif mouse_x <= self.w_date + self.w_key + 5: new_col = 'citekey'
                elif mouse_x <= self.w_date + self.w_key + self.w_auth + 8: new_col = 'author'
                else: new_col = 'title'

                if self.sort_col == new_col:
                    self.sort_rev = not self.sort_rev

                else:
                    self.sort_col = new_col
                    self.sort_rev = False

                self.apply_sort()

            elif 0 < mouse_y <= self.list_h and mouse_x < self.list_w:
                self.active_pane = 0
                clicked_idx = self.item_scroll + (mouse_y - 1)

                if 0 <= clicked_idx < len(self.items):
                    if clicked_idx == self.item_idx and (time.time() - self.last_click_time < 0.4):
                        self.open_selected()

                    else:
                        self.item_idx = clicked_idx
                        self.detail_scroll = 0
                        self.last_click_time = time.time()

            elif mouse_x > self.list_w:
                self.active_pane = 1

    # ---- Field search (/, n, N) -------------------------------------------

    def search_field(self):
        search_query = self.prompt(f"/{self.sort_col.capitalize()}: ")

        if not search_query:
            return

        self.last_search = search_query.lower()

        if not self.search_next(1):
            self.flash(" [!] Pattern not found ", 1000)

    def search_next(self, step):
        for i in range(len(self.items)):
            idx = (self.item_idx + step + (i * step)) % len(self.items)

            if self.last_search in str(getattr(self.items[idx], self.sort_col)).lower():
                self.item_idx = idx

                if self.item_idx < self.item_scroll or self.item_idx >= self.item_scroll + self.list_h:
                    self.item_scroll = max(0, self.item_idx - self.list_h // 2)

                return True

        return False

    # ---- PDFs ---------------------------------------------------------------

    def fetch_pdf(self, item):
        """Local path of the item's PDF, or None after flashing why it is unavailable."""
        attachment = self.library.pdf_attachment(item)

        if attachment is None:
            self.flash(" [!] No PDF attachment found for this item. ", 2000)
            return None

        self.status(f" Fetching {attachment.filename}... ")

        try:
            return self.library.storage.pdf_path(attachment.key, attachment.filename)

        except ZotliteError as e:
            self.flash(f" [!] Error: {e} ", 2000)
            return None

    def open_selected(self):
        if not self.items:
            return

        path = self.fetch_pdf(self.selected)

        if path:
            open_pdf(path)

    def grep_selected(self):
        """Search the text of the selected item's PDF and browse the matches."""
        if not self.items:
            return

        item  = self.selected
        query = self.prompt("PDF grep: ")

        if not query or not query.strip():
            return

        if item.id not in self.pdfs:
            path = self.fetch_pdf(item)

            if path is None:
                return

            self.status(f" Extracting text from {path.name}... ")

            try:
                self.pdfs[item.id] = PdfText(path)

            except ZotliteError as e:
                self.flash(f" [!] Error: {e} ", 2000)
                return

        pdf = self.pdfs[item.id]

        if not pdf.has_text:
            self.flash(" [!] This PDF has no extractable text (scanned?). ", 2000)
            return

        # Smart case: case-sensitive only when the query has capitals
        matches = pdf.search(query, ignore_case=query.islower())

        if not matches:
            self.flash(" [!] Pattern not found in PDF ", 1000)
            return

        MatchesView(self.stdscr, item, query, matches, pdf.path).loop()


class MatchesView(Screen):
    """Full-screen list of in-PDF matches; ENTER opens the PDF at the match's page."""

    PREFIX_W = 8  # " p.123  "

    def __init__(self, stdscr, item, query, matches, pdf_path):
        super().__init__(stdscr)

        self.item     = item
        self.query    = query
        self.matches  = matches
        self.pdf_path = pdf_path
        self.idx      = 0
        self.scroll   = 0

        self.last_click_time = 0

    def loop(self):
        while True:
            self.draw()

            key = self.stdscr.getch()

            if key in [ord('q'), ord('Q'), KEY_ESCAPE, curses.KEY_LEFT] + KEYS_BACKSPACE:
                break

            self.handle_key(key)

    @property
    def list_h(self):
        return self.size[0] - 2

    def draw(self):
        stdscr = self.stdscr
        max_y, max_x = self.size
        stdscr.clear()

        pages = len({m.page for m in self.matches})
        noun = "match" if len(self.matches) == 1 else "matches"
        header = f" '{self.query}' in {self.item.citekey or self.item.title}: {len(self.matches)} {noun} on {pages} page{'s' if pages != 1 else ''} "
        stdscr.addstr(0, 0, trunc(header, max_x - 1), curses.color_pair(PAIR_HEADER) | curses.A_BOLD)

        for row in range(self.list_h):
            idx = self.scroll + row

            if idx < len(self.matches):
                self.draw_match(row + 1, self.matches[idx], idx == self.idx, max_x - 1)

        self.status(" [↑/↓] Move | [ENTER] Open at page | [q/ESC] Back ")

    def draw_match(self, y, match, selected, width):
        base = curses.color_pair(PAIR_SELECTED) if selected else curses.A_NORMAL
        hit_attr = base | curses.A_BOLD | (curses.A_UNDERLINE if selected else curses.color_pair(PAIR_HIT))

        text_w = max(1, width - self.PREFIX_W)
        before = text_w // 3
        pre, hit, post = match.context(before=before, after=max(0, text_w - before - len(match.hit)))

        x = 0
        for text, attr in [(f" p.{match.page:<4} ", base), (pre, base), (hit, hit_attr), (post, base)]:
            text = text[:width - x]

            if text:
                self.stdscr.addstr(y, x, text, attr)
                x += len(text)

        if x < width:
            self.stdscr.addstr(y, x, " " * (width - x), base)

    def select(self, idx):
        self.idx = max(0, min(len(self.matches) - 1, idx))

        if self.idx < self.scroll:
            self.scroll = self.idx

        elif self.idx >= self.scroll + self.list_h:
            self.scroll = self.idx - self.list_h + 1

    def open_selected(self):
        open_pdf(self.pdf_path, page=self.matches[self.idx].page)

    def handle_key(self, key):
        if key == curses.KEY_UP:       self.select(self.idx - 1)
        elif key == curses.KEY_DOWN:   self.select(self.idx + 1)
        elif key == curses.KEY_PPAGE:  self.select(self.idx - self.list_h)
        elif key == curses.KEY_NPAGE:  self.select(self.idx + self.list_h)
        elif key == curses.KEY_HOME:   self.select(0)
        elif key == curses.KEY_END:    self.select(len(self.matches) - 1)
        elif key in KEYS_ENTER:        self.open_selected()

        elif key == curses.KEY_MOUSE:
            try:
                _, _, mouse_y, _, bstate = curses.getmouse()

            except curses.error:
                return

            if bstate & SCROLL_UP or bstate == 65536:
                self.select(self.idx - 1)

            elif bstate & SCROLL_DOWN or bstate == 2097152:
                self.select(self.idx + 1)

            elif (bstate & curses.BUTTON1_PRESSED or bstate & curses.BUTTON1_CLICKED) and 0 < mouse_y <= self.list_h:
                clicked_idx = self.scroll + (mouse_y - 1)

                if clicked_idx < len(self.matches):
                    if clicked_idx == self.idx and (time.time() - self.last_click_time < 0.4):
                        self.open_selected()

                    else:
                        self.select(clicked_idx)
                        self.last_click_time = time.time()
