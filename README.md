# ZotLite

Zotero manager for the command line.

## Setup

```sh
pipx install .            # or: pipx install --force . to upgrade
```

`~/.config/zotlite/settings.toml`:

```toml
[zotero]
library_path = "~/Zotero"                        # local library
# library_path = "ssh://user@host/home/user/Zotero"  # or a library on another machine
```

## Interactive mode

Run `zotlite` with no arguments.

| Key | Action |
| --- | --- |
| `/`, `n`/`N` | Search the column you are sorted by; next/previous match |
| `g` | Grep the selected item's PDF (case-insensitive unless the query has capitals) |
| `S`, `R` | Cycle sort column, reverse sort |
| `TAB` | Switch between the list and the attributes pane |
| `1`-`8` | Resize columns/panes |
| `ENTER` | Open the PDF |
| `q` | Quit |

In the grep results, `ENTER` opens the PDF at the page of the selected match and `q`/`ESC` goes back.

## One-off commands

```sh
zotlite list                              # date, citekey, author, title (tab-separated)
zotlite info vaswani2017                  # an item's fields
zotlite open vaswani2017 --page 4         # open the PDF, optionally at a page
zotlite grep vaswani2017 attention        # search the PDF's text
zotlite grep vaswani2017 -i multi-head attention
zotlite grep vaswani2017 -E 'p\s*<\s*0\.0\d'
```

`grep` matches across line breaks and hyphenation, prints `p.N:` and surrounding context for each hit, and exits
like grep: 0 when something matched, 1 when nothing did, 2 on error. Options: `-i` ignore case, `-E` regex,
`-c` count only, `-C N` characters of context, `--color auto|always|never`.

PDFs from an `ssh://` library are cached in `/tmp/zotlite_cache` and only re-downloaded when they change.
Scanned PDFs without a text layer can't be searched.
