import re
import sqlite3
from dataclasses import dataclass, field

from .config import ZotliteError


@dataclass
class Item:
    id      : int
    key     : str
    type    : str
    title   : str
    date    : str
    author  : str
    citekey : str
    fields  : dict = field(default_factory=dict)


@dataclass
class Attachment:
    key      : str
    filename : str


class Library:
    """Read-only view of a Zotero database, with PDFs resolved through a Storage."""

    def __init__(self, storage):
        self.storage = storage
        self.conn    = sqlite3.connect(f"file:{storage.db_path}?mode=ro", uri=True)
        self._items  = None

    @property
    def items(self):
        if self._items is None:
            self._items = self._load_items()

        return self._items

    def find(self, citekey):
        """Look up an item by citation key (exact match first, then case-insensitive)."""
        for item in self.items:
            if item.citekey == citekey:
                return item

        folded = [item for item in self.items if item.citekey.lower() == citekey.lower()]

        if len(folded) == 1:
            return folded[0]

        raise ZotliteError(f"No item with citation key '{citekey}'.")

    def pdf_attachment(self, item):
        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT items.key, itemAttachments.path
            FROM itemAttachments
            JOIN items ON itemAttachments.itemID = items.itemID
            WHERE itemAttachments.parentItemID = ?
            AND (itemAttachments.contentType = 'application/pdf' OR itemAttachments.path LIKE '%.pdf')
            LIMIT 1
        """, (item.id,))

        result = cursor.fetchone()

        if not result:
            return None

        key_val, path = result

        return Attachment(key=key_val, filename=path.replace('storage:', ''))

    def pdf_path(self, item):
        """Local path to the item's PDF, downloading it first if the library is remote."""
        attachment = self.pdf_attachment(item)

        if attachment is None:
            raise ZotliteError("No PDF attachment found for this item.")

        return self.storage.pdf_path(attachment.key, attachment.filename)

    def close(self):
        self.conn.close()

    def _load_items(self):
        cursor = self.conn.cursor()

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

        return [self._make_item(data) for data in items_dict.values()]

    @staticmethod
    def _make_item(data):
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

        return Item( id      = data['id']
                   , key     = data['key']
                   , type    = data['type']
                   , title   = title
                   , date    = date_str
                   , author  = author_str
                   , citekey = citekey
                   , fields  = fields
                   )
