import sqlite3
import subprocess
import os
import sys
import tomllib
import getpass
import curses
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

def tui_loop(stdscr, db_conn, col_list, raw_path, zotero_path, remote_zotero_path, sftp, cache_dir):
    # Setup Curses environment
    curses.curs_set(0) # Hide cursor
    stdscr.nodelay(0)  # Blocking input
    curses.start_color()
    curses.use_default_colors()
    
    # Color pairs
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)  # Highlighted item
    curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_WHITE) # Status bar
    curses.init_pair(3, curses.COLOR_WHITE, curses.COLOR_BLUE)  # Headers

    active_pane = 0 # 0 = Collections (left), 1 = Items (right)
    col_idx = 0
    item_idx = 0
    col_scroll = 0
    item_scroll = 0
    items = []

    def load_items():
        if not col_list: return []
        cid = col_list[col_idx][0]
        cursor = db_conn.cursor()
        cursor.execute("""
            SELECT items.itemID, itemDataValues.value
            FROM collectionItems
            JOIN items ON collectionItems.itemID = items.itemID
            JOIN itemData ON items.itemID = itemData.itemID
            JOIN fields ON itemData.fieldID = fields.fieldID
            JOIN itemDataValues ON itemData.valueID = itemDataValues.valueID
            WHERE collectionItems.collectionID = ? AND fields.fieldName = 'title'
            ORDER BY itemDataValues.value ASC
        """, (cid,))
        return cursor.fetchall()

    items = load_items()

    while True:
        max_y, max_x = stdscr.getmaxyx()
        stdscr.clear()

        # Layout dimensions
        col_width = max_x // 3
        item_width = max_x - col_width - 1
        list_height = max_y - 2 # Leave room for header and status bar

        # Draw Headers
        stdscr.addstr(0, 0, " COLLECTIONS ".center(col_width), curses.color_pair(3))
        stdscr.addstr(0, col_width + 1, " ITEMS ".center(item_width), curses.color_pair(3))

        # Draw vertical separator
        for i in range(1, max_y - 1):
            stdscr.addch(i, col_width, curses.ACS_VLINE)

        # Draw Collections (Left Pane)
        for i in range(list_height):
            idx = col_scroll + i
            if idx < len(col_list):
                text = f" {col_list[idx][1]}"[:col_width-1]
                if active_pane == 0 and idx == col_idx:
                    stdscr.addstr(i + 1, 0, text.ljust(col_width), curses.color_pair(1))
                else:
                    stdscr.addstr(i + 1, 0, text.ljust(col_width))

        # Draw Items (Right Pane)
        for i in range(list_height):
            idx = item_scroll + i
            if idx < len(items):
                text = f" {items[idx][1]}"[:item_width-1]
                if active_pane == 1 and idx == item_idx:
                    stdscr.addstr(i + 1, col_width + 1, text.ljust(item_width), curses.color_pair(1))
                else:
                    stdscr.addstr(i + 1, col_width + 1, text.ljust(item_width))

        # Draw Status Bar
        status_text = " [TAB/LEFT/RIGHT] Switch Pane | [UP/DOWN] Navigate | [ENTER] Open PDF | [Q] Quit "
        stdscr.addstr(max_y - 1, 0, status_text.ljust(max_x - 1), curses.color_pair(2))

        stdscr.refresh()

        # Handle Keyboard Input
        key = stdscr.getch()

        if key in [ord('q'), ord('Q')]:
            break
        elif key == curses.KEY_RESIZE:
            continue
        elif key in [9, curses.KEY_LEFT, curses.KEY_RIGHT]: # 9 is TAB
            active_pane = 1 - active_pane
        elif key == curses.KEY_UP:
            if active_pane == 0 and col_idx > 0:
                col_idx -= 1
                if col_idx < col_scroll: col_scroll = col_idx
                items = load_items()
                item_idx, item_scroll = 0, 0
            elif active_pane == 1 and item_idx > 0:
                item_idx -= 1
                if item_idx < item_scroll: item_scroll = item_idx
        elif key == curses.KEY_DOWN:
            if active_pane == 0 and col_idx < len(col_list) - 1:
                col_idx += 1
                if col_idx >= col_scroll + list_height: col_scroll = col_idx - list_height + 1
                items = load_items()
                item_idx, item_scroll = 0, 0
            elif active_pane == 1 and item_idx < len(items) - 1:
                item_idx += 1
                if item_idx >= item_scroll + list_height: item_scroll = item_idx - list_height + 1
        elif key in [10, 13, curses.KEY_ENTER]:
            if active_pane == 0:
                active_pane = 1 # Switch to right pane on Enter
            elif active_pane == 1 and items:
                # User hit Enter on an Item -> Open PDF
                selected_item_id = items[item_idx][0]
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
    cursor = conn.cursor()

    cursor.execute("SELECT collectionID, collectionName, parentCollectionID FROM collections")
    col_dict = {row[0]: {'name': row[1], 'parent': row[2]} for row in cursor.fetchall()}
    
    def get_col_path(cid):
        path = col_dict[cid]['name']
        pid = col_dict[cid]['parent']
        while pid and pid in col_dict:
            path = col_dict[pid]['name'] + " / " + path
            pid = col_dict[pid]['parent']
        return path

    col_list = [(cid, get_col_path(cid)) for cid in col_dict.keys()]
    col_list.sort(key=lambda x: x[1].lower())

    # Launch the Curses TUI
    curses.wrapper(tui_loop, conn, col_list, raw_path, zotero_path, remote_zotero_path, sftp, cache_dir)

if __name__ == "__main__":
    main()
