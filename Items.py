from typing import Dict, NamedTuple, Optional
from BaseClasses import Item, ItemClassification


class SMB1ItemData(NamedTuple):
    code: Optional[int]
    classification: ItemClassification


BASE_ID = 0xADD000

item_table: Dict[str, SMB1ItemData] = {
    # --- Abilities (progression, used for "ability gating") ---
    "Progressive Mushroom":  SMB1ItemData(BASE_ID + 0, ItemClassification.progression),
    "Progressive Fire Flower": SMB1ItemData(BASE_ID + 1, ItemClassification.progression),
    "Progressive Star": SMB1ItemData(BASE_ID + 6, ItemClassification.progression),

    # --- Filler / useful ---
    "1-Up Mushroom":   SMB1ItemData(BASE_ID + 2, ItemClassification.filler),
    "Star Power":      SMB1ItemData(BASE_ID + 3, ItemClassification.filler),
    "10 Coins":        SMB1ItemData(BASE_ID + 4, ItemClassification.filler),

    # --- Traps ---
    "Shrink Trap":     SMB1ItemData(BASE_ID + 5, ItemClassification.trap),
    "Ice Trap":        SMB1ItemData(BASE_ID + 8, ItemClassification.trap),
    "Death Trap":      SMB1ItemData(BASE_ID + 9, ItemClassification.trap),
}


def create_item(name: str, player: int) -> Item:
    data = item_table[name]
    return Item(name, data.classification, data.code, player)
