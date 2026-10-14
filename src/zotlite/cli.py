import sqlite3
import subprocess
import os
import sys
import tomllib
import getpass
import curses
import textwrap
import time
import re
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("Error: paramiko is required for SSH support. Reinstall with: pipx install .")
    sys.exit(1)

def setup_ssh_client(hostname, username=None):
    ssh_config_file = Path.home() / ".ssh" / "config"
    ssh_config = paramiko.SSHConfig()
    
    if ssh_config_file.exists():
        with open(ssh_config_file) as f:
            ssh_config.parse(f)
            
    host_conf = ssh_config.lookup(hostname)
    host_name = host_conf.get('hostname', hostname)
    host_port = int(host_conf.get('port', 22)) 

    connect_kwargs = { 'hostname' : host_name
                     , 'port'     : host_port
                     }
    
    if username:
        connect_kwargs['username'] = username
    elif 'user' in host_conf:
        connect_kwargs['username'] = host_conf['user']
    else:
        connect_kwargs['username'] = getpass.getuser()

    if 'identityfile' in host_conf:
        connect_kwargs['key_filename'] = host_conf['identityfile']

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    print(f"Connecting to SSH -> {connect_kwargs['username']}@{connect_kwargs['hostname']}...")

    try:
        client.connect(**connect_kwargs)

    except paramiko.ssh_exception.AuthenticationException:
        password = getpass.getpass(f"Password for {connect_kwargs['username']}@{connect_kwargs['hostname']}: ")
        connect_kwargs['password'] = password
        client.connect(**connect_kwargs)
        
    return client

def load_all_items(db_conn):
    cursor = db_conn.cursor()
    
    cursor.execute("""
        SELECT i.itemID, i.key, t.typeName
        FROM items i
        JOIN itemTypes t ON i.itemTypeID = t.itemTypeID
        JOIN libraries l ON i.libraryID = l.libraryID
        LEFT JOIN deletedItems d ON i.itemID = d.itemID
        WHERE t.typeName NOT IN ('attachment', 'note', 'annotation')
          AND d.itemID IS NULL
          AND l.type = 'user'
    """)
    items_dict = { row[0]:  { 'id'      : row[0]
                            , 'key'     : row[1]
                            , 'type'    : row[2]
                            , 'fields'  : {}
                            , 'creators': []
                            } for row in cursor.fetchall()
                 }
    
    cursor.execute("""
        SELECT id.itemID, f.fieldName, v.value
        FROM itemData id
        JOIN fields f ON id.fieldID = f.fieldID
        JOIN itemDataValues v ON id.valueID = v.valueID
    """)

    for row in cursor.fetchall():
        if row[0] in items_dict:
            items_dict[row[0]]['fields'][row[1]] = str(row[2])
            
    cursor.execute("""
        SELECT ic.itemID, c.lastName
        FROM itemCreators ic
        JOIN creators c ON ic.creatorID = c.creatorID
        ORDER BY ic.orderIndex
    """)

    for row in cursor.fetchall():
        if row[0] in items_dict:
            items_dict[row[0]]['creators'].append(row[1])

    display_list = []

    for i_id, data in items_dict.items():
        fields = data['fields']
        title = fields.get('title', '[No Title]')
        raw_date = fields.get('date', '')
        date_match = re.search(r'\d{4}(?:[-/]\d{2}(?:[-/]\d{2})?)?', raw_date)
        date_str = date_match.group(0).replace('-00', '') if date_match else raw_date.split()[0] if raw_date else ""
        creators = data['creators']

        if len(creators) > 2: author_str = f"{creators[0]} et al."
        elif len(creators) == 2: author_str = f"{creators[0]} & {creators[1]}"
        elif len(creators) == 1: author_str = creators[0]
        else: author_str = ""
            
        citekey = fields.get('citationKey', '')

        if not citekey:
            ck_match = re.search(r'Citation Key:\s*([^\s\n]+)', fields.get('extra', ''))

            if ck_match:
                citekey = ck_match.group(1)
        
        display_list.append(
            { 'id': data['id']
            , 'title': title
            , 'date': date_str
            , 'author': author_str
            , 'citekey': citekey
            , 'fields': fields
            })
        
    return display_list

def trunc(text, width):
    text = str(text)

    if width <= 1:
        return "." * width

    return (text[:width-1] + "…") if len(text) > width else text.ljust(width)

def tui_loop(stdscr, db_conn, raw_path, zotero_path, remote_zotero_path, sftp, cache_dir):
    try: curses.curs_set(0)
    except curses.error: pass

    stdscr.nodelay(0)
    curses.start_color()
    curses.use_default_colors()
    
    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)

    SCROLL_UP = getattr(curses, 'BUTTON4_PRESSED', 524288)
    SCROLL_DOWN = getattr(curses, 'BUTTON5_PRESSED', 2097152)
    
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN) 
    curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_WHITE)
    curses.init_pair(3, curses.COLOR_WHITE, curses.COLOR_BLUE)

    items = load_all_items(db_conn)
    
    # Navigation States
    active_pane = 0 
    item_idx = 0
    item_scroll = 0
    detail_scroll = 0
    last_click_time = 0
    
    # Sorting States
    sort_col = 'date'
    sort_rev = True
    
    def apply_sort():
        nonlocal item_idx, item_scroll
        items.sort(key=lambda x: str(x[sort_col]).lower(), reverse=sort_rev)
        item_idx = 0
        item_scroll = 0

    apply_sort()
    
    # Search States
    last_search = ""
    
    # Dynamic Width States
    details_w_pct = 0.33
    w_date = 10
    w_key = 18
    w_auth = 20

    while True:
        max_y, max_x = stdscr.getmaxyx()
        stdscr.clear()

        details_w = max(15, int(max_x * details_w_pct))
        list_w = max_x - details_w - 1
        list_h = max_y - 1 
        
        total_fixed = w_date + w_key + w_auth + 13
        if total_fixed > list_w - 5:
            overshoot = total_fixed - (list_w - 5)
            w_auth = max(4, w_auth - overshoot)
        w_title = max(5, list_w - (w_date + w_key + w_auth + 13))

        detail_lines = []
        if items:
            sel_fields = items[item_idx]['fields']
            for key_name, val in sel_fields.items():
                detail_lines.append((f"{key_name.capitalize()}:", True)) 
                wrapped = textwrap.wrap(val, width=details_w - 4)
                for line in wrapped:
                    detail_lines.append((f"  {line}", False))
                detail_lines.append(("", False))

        max_detail_scroll = max(0, len(detail_lines) - list_h)
        detail_scroll = max(0, min(detail_scroll, max_detail_scroll))

        arr_d = "▼" if sort_col == 'date'    and sort_rev else ("▲" if sort_col == 'date'    else "")
        arr_k = "▼" if sort_col == 'citekey' and sort_rev else ("▲" if sort_col == 'citekey' else "")
        arr_a = "▼" if sort_col == 'author'  and sort_rev else ("▲" if sort_col == 'author'  else "")
        arr_t = "▼" if sort_col == 'title'   and sort_rev else ("▲" if sort_col == 'title'   else "")

        hdr_attr_left  = curses.color_pair(3) | (curses.A_BOLD if active_pane == 0 else curses.A_NORMAL)
        hdr_attr_right = curses.color_pair(3) | (curses.A_BOLD if active_pane == 1 else curses.A_NORMAL)
        
        header_str = f" {trunc('Date'+arr_d, w_date)} | {trunc('CiteKey'+arr_k, w_key)} | {trunc('Author'+arr_a, w_auth)} | {trunc('Title'+arr_t, w_title)}"

        stdscr.addstr(0, 0, header_str[:list_w].ljust(list_w), hdr_attr_left)
        stdscr.addstr(0, list_w + 1, " ATTRIBUTES ".center(details_w), hdr_attr_right)

        for i in range(1, max_y - 1):
            stdscr.addch(i, list_w, curses.ACS_VLINE)

        for i in range(list_h):
            idx = item_scroll + i

            if idx < len(items):
                it = items[idx]
                row_str = f" {trunc(it['date'], w_date)} | {trunc(it['citekey'], w_key)} | {trunc(it['author'], w_auth)} | {trunc(it['title'], w_title)}"
                
                disp_str = row_str[:list_w-1].ljust(list_w)

                if idx == item_idx:
                    stdscr.addstr(i + 1, 0, disp_str, curses.color_pair(1) if active_pane == 0 else curses.A_REVERSE)
                else:
                    stdscr.addstr(i + 1, 0, disp_str)

        for i in range(list_h):
            idx = detail_scroll + i

            if idx < len(detail_lines):
                text, is_bold = detail_lines[idx]
                attr = curses.A_BOLD if is_bold else curses.A_NORMAL

                if active_pane == 1 and text.strip():
                    attr |= curses.color_pair(1)
                
                stdscr.addstr(i + 1, list_w + 2, text[:details_w-2].ljust(details_w-2), attr)

        status = " [/] Search | [n/N] Next | [TAB] Switch | [S]ort | [1-8] Resize | [ENTER] Open "
        stdscr.addstr(max_y - 1, 0, status[:max_x-1].ljust(max_x - 1), curses.color_pair(2))
        stdscr.refresh()

        key = stdscr.getch()
        trigger_open = False

        if key in [ord('q'), ord('Q')]:
            break

        elif key == curses.KEY_RESIZE:
            continue

        elif key == 9: 
            active_pane = 1 - active_pane
            
        elif key in [ord('s'), ord('S')]:
            cols = ['date', 'citekey', 'author', 'title']
            sort_col = cols[(cols.index(sort_col) + 1) % 4]
            apply_sort()

        elif key in [ord('r'), ord('R')]:
            sort_rev = not sort_rev
            apply_sort()

        elif key == ord('/'):
            try: curses.curs_set(1)
            except curses.error: pass
            
            search_query = ""

            while True:
                prompt = f"/{sort_col.capitalize()}: {search_query}"
                stdscr.addstr(max_y - 1, 0, prompt[:max_x-1].ljust(max_x - 1), curses.color_pair(2))
                stdscr.move(max_y - 1, min(len(prompt), max_x - 1))
                stdscr.refresh()
                
                c = stdscr.getch()

                if c in [10, 13, curses.KEY_ENTER]:
                    break

                elif c == 27: # Escape key
                    search_query = None
                    break

                elif c in [8, 127, curses.KEY_BACKSPACE]:
                    search_query = search_query[:-1]

                elif 32 <= c <= 126:
                    search_query += chr(c)
                    
            try: curses.curs_set(0)
            except curses.error: pass
            
            if search_query:
                last_search = search_query.lower()
                found = False

                for i in range(len(items)):
                    idx = (item_idx + 1 + i) % len(items)

                    if last_search in str(items[idx][sort_col]).lower():
                        item_idx = idx

                        if item_idx < item_scroll or item_idx >= item_scroll + list_h:
                            item_scroll = max(0, item_idx - list_h // 2)

                        found = True

                        break

                if not found:
                    stdscr.addstr(max_y - 1, 0, " [!] Pattern not found ".ljust(max_x - 1), curses.color_pair(2))
                    stdscr.refresh()
                    curses.napms(1000)

        # Handle Search Next/Prev
        elif key in [ord('n'), ord('N')] and last_search:
            step = 1 if key == ord('n') else -1

            for i in range(len(items)):
                idx = (item_idx + step + (i * step)) % len(items)

                if last_search in str(items[idx][sort_col]).lower():
                    item_idx = idx

                    if item_idx < item_scroll or item_idx >= item_scroll + list_h:
                        item_scroll = max(0, item_idx - list_h // 2)

                    break

        elif key == ord('1'): w_date = max(4, w_date - 1)
        elif key == ord('2'): w_date += 1
        elif key == ord('3'): w_key = max(5, w_key - 1)
        elif key == ord('4'): w_key += 1
        elif key == ord('5'): w_auth = max(5, w_auth - 1)
        elif key == ord('6'): w_auth += 1
        elif key == ord('7'): details_w_pct = max(0.15, details_w_pct - 0.05)
        elif key == ord('8'): details_w_pct = min(0.70, details_w_pct + 0.05)
        
        elif key == curses.KEY_UP:
            if active_pane == 0 and item_idx > 0:
                item_idx -= 1

                if item_idx < item_scroll: item_scroll = item_idx

                detail_scroll = 0

            elif active_pane == 1:
                detail_scroll = max(0, detail_scroll - 1)
                
        elif key == curses.KEY_DOWN:
            if active_pane == 0 and item_idx < len(items) - 1:
                item_idx += 1

                if item_idx >= item_scroll + list_h:
                    item_scroll = item_idx - list_h + 1

                detail_scroll = 0

            elif active_pane == 1:
                detail_scroll = min(max_detail_scroll, detail_scroll + 1)
                
        elif key == curses.KEY_PPAGE:
            if active_pane == 0:
                item_idx = max(0, item_idx - list_h)
                item_scroll = max(0, item_scroll - list_h)
                detail_scroll = 0

            elif active_pane == 1:
                detail_scroll = max(0, detail_scroll - list_h)
                
        elif key == curses.KEY_NPAGE:
            if active_pane == 0:
                item_idx = min(len(items) - 1, item_idx + list_h)
                item_scroll = min(max(0, len(items) - list_h), item_scroll + list_h)
                detail_scroll = 0

            elif active_pane == 1:
                detail_scroll = min(max_detail_scroll, detail_scroll + list_h)
                
        elif key in [10, 13, curses.KEY_ENTER]:
            trigger_open = True
            
        elif key == curses.KEY_MOUSE:
            try:
                _, mouse_x, mouse_y, _, bstate = curses.getmouse()

            except curses.error:
                continue
                
            if bstate & SCROLL_UP or bstate == 65536:
                if mouse_x < list_w:
                    item_idx = max(0, item_idx - 1)

                    if item_idx < item_scroll:
                        item_scroll = item_idx

                    detail_scroll = 0

                else:
                    detail_scroll = max(0, detail_scroll - 1)
                    
            elif bstate & SCROLL_DOWN or bstate == 2097152:
                if mouse_x < list_w:
                    item_idx = min(len(items)-1, item_idx + 1)

                    if item_idx >= item_scroll + list_h:
                        item_scroll = item_idx - list_h + 1

                    detail_scroll = 0

                else:
                    detail_scroll = min(max_detail_scroll, detail_scroll + 1)
                    
            elif bstate & curses.BUTTON1_PRESSED or bstate & curses.BUTTON1_CLICKED:
                if mouse_y == 0 and mouse_x < list_w:
                    if mouse_x <= w_date + 2: new_col = 'date'
                    elif mouse_x <= w_date + w_key + 5: new_col = 'citekey'
                    elif mouse_x <= w_date + w_key + w_auth + 8: new_col = 'author'
                    else: new_col = 'title'
                    
                    if sort_col == new_col:
                        sort_rev = not sort_rev

                    else:
                        sort_col = new_col
                        sort_rev = False

                    apply_sort()
                    
                elif 0 < mouse_y <= list_h and mouse_x < list_w:
                    active_pane = 0
                    clicked_idx = item_scroll + (mouse_y - 1)

                    if 0 <= clicked_idx < len(items):
                        if clicked_idx == item_idx and (time.time() - last_click_time < 0.4):
                            trigger_open = True

                        else:
                            item_idx = clicked_idx
                            detail_scroll = 0
                            last_click_time = time.time()

                elif mouse_x > list_w:
                    active_pane = 1
        
        if trigger_open and items:
            selected_item_id = items[item_idx]['id']
            cursor = db_conn.cursor()

            cursor.execute("""
                SELECT items.key, itemAttachments.path
                FROM itemAttachments
                JOIN items ON itemAttachments.itemID = items.itemID
                WHERE itemAttachments.parentItemID = ? 
                AND (itemAttachments.contentType = 'application/pdf' OR itemAttachments.path LIKE '%.pdf')
                LIMIT 1
            """, (selected_item_id,))

            result = cursor.fetchone()
            
            if result:
                key_val, path = result
                filename = path.replace('storage:', '')
                
                stdscr.addstr(max_y - 1, 0, f" Downloading/Opening {filename}... ".ljust(max_x - 1), curses.color_pair(2))
                stdscr.refresh()

                if raw_path.startswith("ssh://"):
                    remote_pdf = f"{remote_zotero_path}/storage/{key_val}/{filename}".replace("//", "/")
                    local_pdf = cache_dir / filename

                    try:
                        sftp.get(remote_pdf, str(local_pdf))
                        target_path = local_pdf

                    except IOError:
                        stdscr.addstr(max_y - 1, 0, " [!] Error: PDF not found on remote server. ".ljust(max_x - 1), curses.color_pair(2))
                        stdscr.refresh()
                        curses.napms(2000)
                        continue

                else:
                    target_path = zotero_path / 'storage' / key_val / filename
                
                if target_path.exists():
                    subprocess.Popen( ['google-chrome', str(target_path)]
                                    , stdout=subprocess.DEVNULL
                                    , stderr=subprocess.DEVNULL
                                    )

                else:
                    stdscr.addstr(max_y - 1, 0, " [!] Error: PDF file not found locally. ".ljust(max_x - 1), curses.color_pair(2))
                    stdscr.refresh()
                    curses.napms(2000)

            else:
                stdscr.addstr(max_y - 1, 0, " [!] No PDF attachment found for this item. ".ljust(max_x - 1), curses.color_pair(2))
                stdscr.refresh()
                curses.napms(2000)

def main():
    config_path = Path.home() / ".config" / "zotlite" / "settings.toml"
    
    if not config_path.exists():
        print(f"Error: Configuration file not found at {config_path}")
        sys.exit(1)
        
    with open(config_path, "rb") as f:
        config = tomllib.load(f)

    raw_path = config.get('zotero', {}).get('library_path', '').strip()

    if not raw_path:
        print("Error: Missing library_path in settings.")
        sys.exit(1)
    
    sftp = None
    remote_zotero_path = None
    cache_dir = Path("/tmp/zotlite_cache")
    zotero_path = None

    if raw_path.startswith("ssh://"):
        path_without_scheme = raw_path[6:] 
        user_host, remote_zotero_path = path_without_scheme.split('/', 1)
        remote_zotero_path = '/' + remote_zotero_path
        
        if '@' in user_host:
            username, hostname = user_host.split('@', 1)

        else:
            username = None
            hostname = user_host

        hostname = hostname.replace(":", "")
        client   = setup_ssh_client(hostname=hostname, username=username)
        sftp     = client.open_sftp()
        
        cache_dir.mkdir(exist_ok=True)

        db_path   = cache_dir / "zotero.sqlite"
        remote_db = f"{remote_zotero_path}/zotero.sqlite".replace("//", "/")
        
        print("Syncing database...")

        try:
            sftp.get(remote_db, str(db_path))

        except IOError:
            print(f"\nError: Database not found on remote server at {remote_db}")
            sys.exit(1)
    else:
        zotero_path = Path(raw_path).expanduser()
        db_path = zotero_path / 'zotero.sqlite'

    if not db_path.exists():
        print(f"Error: Database not found at {db_path}")
        sys.exit(1)

    db_uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(db_uri, uri=True)

    curses.wrapper(tui_loop, conn, raw_path, zotero_path, remote_zotero_path, sftp, cache_dir)

if __name__ == "__main__":
    main()
