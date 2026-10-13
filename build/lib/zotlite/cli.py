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
    connect_kwargs = {
        'hostname': host_conf.get('hostname', hostname),
        'port': int(host_conf.get('port', 22))
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
    
    # 1. Get base items (excluding attachments and notes)
    cursor.execute("""
        SELECT i.itemID, i.key, t.typeName
        FROM items i
        JOIN itemTypes t ON i.itemTypeID = t.itemTypeID
        WHERE t.typeName NOT IN ('attachment', 'note', 'annotation')
    """)
    items_dict = {row[0]: {'id': row[0], 'key': row[1], 'type': row[2], 'fields': {}, 'creators': []} for row in cursor.fetchall()}
    
    # 2. Get metadata fields
    cursor.execute("""
        SELECT id.itemID, f.fieldName, v.value
        FROM itemData id
        JOIN fields f ON id.fieldID = f.fieldID
        JOIN itemDataValues v ON id.valueID = v.valueID
    """)
    for row in cursor.fetchall():
        if row[0] in items_dict:
            items_dict[row[0]]['fields'][row[1]] = str(row[2])
            
    # 3. Get authors
    cursor.execute("""
        SELECT ic.itemID, c.lastName
        FROM itemCreators ic
        JOIN creators c ON ic.creatorID = c.creatorID
        ORDER BY ic.orderIndex
    """)
    for row in cursor.fetchall():
        if row[0] in items_dict:
            items_dict[row[0]]['creators'].append(row[1])

    # 4. Format data for the UI Table
    display_list = []
    for i_id, data in items_dict.items():
        fields = data['fields']
        title = fields.get('title', '[No Title]')
        
        # Extract just the year from the date
        raw_date = fields.get('date', '')
        date_match = re.search(r'\d{4}', raw_date)
        date_str = date_match.group(0) if date_match else raw_date[:4]
        
        # Format Authors (e.g. Smith et al.)
        creators = data['creators']
        if len(creators) > 2: author_str = f"{creators[0]} et al."
        elif len(creators) == 2: author_str = f"{creators[0]} & {creators[1]}"
        elif len(creators) == 1: author_str = creators[0]
        else: author_str = ""
            
        # Extract Citation Key (usually jammed in the 'extra' field by Better BibTeX)
        citekey = fields.get('citationKey', '')
        if not citekey:
            ck_match = re.search(r'Citation Key:\s*([^\s\n]+)', fields.get('extra', ''))
            if ck_match: citekey = ck_match.group(1)
        
        display_list.append({
            'id': data['id'],
            'title': title,
            'date': date_str,
            'author': author_str,
            'citekey': citekey,
            'fields': fields
        })
        
    # Sort descending by date, then alphabetically by title
    display_list.sort(key=lambda x: (x['date'], x['title']), reverse=True)
    return display_list

def tui_loop(stdscr, db_conn, raw_path, zotero_path, remote_zotero_path, sftp, cache_dir):
    curses.curs_set(0)
    stdscr.nodelay(0)
    curses.start_color()
    curses.use_default_colors()
    
    # Enable Mouse Reporting
    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)  # Highlight
    curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_WHITE) # Status Bar
    curses.init_pair(3, curses.COLOR_WHITE, curses.COLOR_BLUE)  # Headers

    items = load_all_items(db_conn)
    
    item_idx = 0
    item_scroll = 0
    last_click_time = 0

    while True:
        max_y, max_x = stdscr.getmaxyx()
        stdscr.clear()

        # Layout Math
        details_w = max_x // 3
        list_w = max_x - details_w - 1
        list_h = max_y - 3 # Leave room for header and status
        
        # Table Columns Widths
        w_date, w_key, w_auth = 6, 18, 20
        w_title = list_w - (w_date + w_key + w_auth + 13) # Remainder
        
        header_str = f" {'Date':<{w_date}} | {'Citation Key':<{w_key}} | {'Author':<{w_auth}} | {'Title':<{w_title}}"

        # Draw Headers
        stdscr.addstr(0, 0, header_str[:list_w].ljust(list_w), curses.color_pair(3))
        stdscr.addstr(0, list_w + 1, " ATTRIBUTES ".center(details_w), curses.color_pair(3))

        # Draw Vertical Separator
        for i in range(1, max_y - 1):
            stdscr.addch(i, list_w, curses.ACS_VLINE)

        # Draw Library Table
        for i in range(list_h):
            idx = item_scroll + i
            if idx < len(items):
                it = items[idx]
                row_str = f" {it['date']:<{w_date}} | {it['citekey']:<{w_key}} | {it['author']:<{w_auth}} | {it['title']:<{w_title}}"
                
                # Truncate and highlight
                disp_str = row_str[:list_w-1].ljust(list_w)
                if idx == item_idx:
                    stdscr.addstr(i + 1, 0, disp_str, curses.color_pair(1))
                else:
                    stdscr.addstr(i + 1, 0, disp_str)

        # Draw Details Pane (Right Side)
        if items:
            sel_fields = items[item_idx]['fields']
            y_det = 2
            for key, val in sel_fields.items():
                if y_det >= max_y - 2: break
                
                # Print Field Name in Bold
                label = f"{key.capitalize()}:"
                stdscr.addstr(y_det, list_w + 2, label[:details_w-4], curses.A_BOLD)
                y_det += 1
                
                # Wrap long text values so they don't crash the terminal edge
                wrapped_lines = textwrap.wrap(val, width=details_w - 4)
                for line in wrapped_lines:
                    if y_det >= max_y - 2: break
                    stdscr.addstr(y_det, list_w + 4, line)
                    y_det += 1
                y_det += 1 # Empty line between fields

        # Draw Status Bar
        status = " [CLICK] Select | [DOUBLE CLICK / ENTER] Open PDF | [UP/DOWN] Scroll | [Q] Quit "
        stdscr.addstr(max_y - 1, 0, status.ljust(max_x - 1), curses.color_pair(2))
        stdscr.refresh()

        # Handle Inputs
        key = stdscr.getch()
        trigger_open = False

        if key in [ord('q'), ord('Q')]:
            break
        elif key == curses.KEY_RESIZE:
            continue
        elif key == curses.KEY_UP and item_idx > 0:
            item_idx -= 1
            if item_idx < item_scroll: item_scroll = item_idx
        elif key == curses.KEY_DOWN and item_idx < len(items) - 1:
            item_idx += 1
            if item_idx >= item_scroll + list_h: item_scroll = item_idx - list_h + 1
        elif key in [10, 13, curses.KEY_ENTER]:
            trigger_open = True
        elif key == curses.KEY_MOUSE:
            try:
                _, mouse_x, mouse_y, _, bstate = curses.getmouse()
            except curses.error:
                continue
                
            # If mouse clicked inside the left List Pane
            if 0 < mouse_y <= list_h and mouse_x < list_w:
                clicked_idx = item_scroll + (mouse_y - 1)
                
                if 0 <= clicked_idx < len(items):
                    # Manual Double-Click Detection
                    if clicked_idx == item_idx and (time.time() - last_click_time < 0.4):
                        trigger_open = True
                    else:
                        item_idx = clicked_idx
                        last_click_time = time.time()
        
        # Handle Opening PDF
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
                    subprocess.Popen(['google-chrome', str(target_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

        client = setup_ssh_client(hostname=hostname, username=username)
        sftp = client.open_sftp()
        
        cache_dir.mkdir(exist_ok=True)
        db_path = cache_dir / "zotero.sqlite"
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

    # Launch the Curses TUI
    curses.wrapper(tui_loop, conn, raw_path, zotero_path, remote_zotero_path, sftp, cache_dir)

if __name__ == "__main__":
    main()
