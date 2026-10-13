import sqlite3
import subprocess
import os
import sys
import tomllib
import getpass
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
    if raw_path.startswith("ssh://"):
        # Format expected: ssh://username@hostname/path/to/Zotero
        path_without_scheme = raw_path[6:] # Strip ssh://
        
        # Split out username, hostname, and path
        user_host, remote_zotero_path = path_without_scheme.split('/', 1)
        remote_zotero_path = '/' + remote_zotero_path # add the slash back
        
        if '@' in user_host:
            username, hostname = user_host.split('@', 1)
        else:
            username = None
            hostname = user_host

        # Clean up any accidental SCP colons
        hostname = hostname.replace(":", "")

        client = setup_ssh_client(hostname=hostname, username=username)
        sftp = client.open_sftp()
        
        cache_dir = Path("/tmp/zotlite_cache")
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
        # Local connection
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

    while True:
        print("\n" + "="*40 + "\n ZOTERO COLLECTIONS\n" + "="*40)
        for i, (cid, cpath) in enumerate(col_list):
            print(f"[{i}] {cpath}")
        print("[q] Quit")
        
        choice = input("\nSelect a collection number: ").strip().lower()
        if choice == 'q': break
        if not choice.isdigit() or not (0 <= int(choice) < len(col_list)): continue
            
        selected_cid = col_list[int(choice)][0]
        selected_name = col_dict[selected_cid]['name']
        
        cursor.execute("""
            SELECT items.itemID, itemDataValues.value
            FROM collectionItems
            JOIN items ON collectionItems.itemID = items.itemID
            JOIN itemData ON items.itemID = itemData.itemID
            JOIN fields ON itemData.fieldID = fields.fieldID
            JOIN itemDataValues ON itemData.valueID = itemDataValues.valueID
            WHERE collectionItems.collectionID = ? AND fields.fieldName = 'title'
            ORDER BY itemDataValues.value ASC
        """, (selected_cid,))
        items = cursor.fetchall()
        
        if not items:
            print(f"\nNo items found in '{selected_name}'.")
            continue

        while True:
            print("\n" + "-"*40 + f"\n ITEMS IN: {selected_name}\n" + "-"*40)
            for i, (item_id, title) in enumerate(items):
                print(f"[{i}] {title}")
            print("[b] Back to Collections\n[q] Quit")
            
            item_choice = input("\nSelect an item to open its PDF: ").strip().lower()
            if item_choice == 'q': sys.exit(0)
            if item_choice == 'b': break
            if not item_choice.isdigit() or not (0 <= int(item_choice) < len(items)): continue
                
            selected_item_id = items[int(item_choice)][0]
            
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
                key, path = result
                filename = path.replace('storage:', '')
                
                if raw_path.startswith("ssh://"):
                    remote_pdf = f"{remote_zotero_path}/storage/{key}/{filename}".replace("//", "/")
                    local_pdf = cache_dir / filename
                    print(f"\nDownloading {filename}...")
                    try:
                        sftp.get(remote_pdf, str(local_pdf))
                        target_path = local_pdf
                    except IOError:
                        print(f"\n[!] Error: PDF not found on remote server at {remote_pdf}")
                        continue
                else:
                    target_path = zotero_path / 'storage' / key / filename
                
                if target_path.exists():
                    print(f"Launching Chrome: {filename}")
                    subprocess.Popen(['google-chrome', str(target_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    print(f"\n[!] Error: PDF file not found at {target_path}")
            else:
                print("\n[!] No PDF attachment found for this item.")

if __name__ == "__main__":
    main()
