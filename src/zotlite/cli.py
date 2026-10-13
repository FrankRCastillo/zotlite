import sqlite3
import subprocess
import os
import sys
import tomllib
from pathlib import Path

def main():
    # Set config path to ~/.config/zotlite/settings.toml
    config_dir = Path.home() / ".config" / "zotlite"
    config_path = config_dir / "settings.toml"
    
    if not config_path.exists():
        print(f"Error: Configuration file not found at {config_path}")
        print("Please create it with the following content:")
        print('[zotero]')
        print('library_path = "/home/yourusername/Zotero"')
        
        # Auto-create the directory structure for the user
        try:
            config_dir.mkdir(parents=True, exist_ok=True)
            print(f"\n(Note: The directory {config_dir} has been created for you.)")
        except Exception:
            pass
        sys.exit(1)
        
    try:
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
    except Exception as e:
        print(f"Error reading TOML file: {e}")
        sys.exit(1)
        
    try:
        zotero_path = Path(config['zotero']['library_path']).expanduser()
    except KeyError:
        print(f"Error: Invalid format in {config_path}. Missing [zotero] library_path.")
        sys.exit(1)
        
    db_path = zotero_path / 'zotero.sqlite'

    if not db_path.exists():
        print(f"Error: Database not found at {db_path}")
        sys.exit(1)

    db_uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(db_uri, uri=True)
    cursor = conn.cursor()

    cursor.execute("SELECT collectionID, collectionName, parentCollectionID FROM collections")
    collections_raw = cursor.fetchall()
    col_dict = {row[0]: {'name': row[1], 'parent': row[2]} for row in collections_raw}
    
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
        print("\n" + "="*40)
        print(" ZOTERO COLLECTIONS")
        print("="*40)
        for i, (cid, cpath) in enumerate(col_list):
            print(f"[{i}] {cpath}")
        print("[q] Quit")
        
        choice = input("\nSelect a collection number: ").strip().lower()
        if choice == 'q':
            break
        if not choice.isdigit() or not (0 <= int(choice) < len(col_list)):
            print("Invalid choice.")
            continue
            
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
            print("\n" + "-"*40)
            print(f" ITEMS IN: {selected_name}")
            print("-"*40)
            for i, (item_id, title) in enumerate(items):
                print(f"[{i}] {title}")
            print("[b] Back to Collections")
            print("[q] Quit")
            
            item_choice = input("\nSelect an item to open its PDF: ").strip().lower()
            
            if item_choice == 'q':
                sys.exit(0)
            if item_choice == 'b':
                break
            if not item_choice.isdigit() or not (0 <= int(item_choice) < len(items)):
                print("Invalid choice.")
                continue
                
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
                if path.startswith('storage:'):
                    filename = path.replace('storage:', '')
                    pdf_path = zotero_path / 'storage' / key / filename
                    
                    if pdf_path.exists():
                        print(f"\nLaunching Chrome: {filename}")
                        subprocess.Popen(
                            ['google-chrome', str(pdf_path)], 
                            stdout=subprocess.DEVNULL, 
                            stderr=subprocess.DEVNULL
                        )
                    else:
                        print(f"\n[!] Error: PDF file not found at {pdf_path}")
            else:
                print("\n[!] No PDF attachment found for this item.")

if __name__ == "__main__":
    main()

