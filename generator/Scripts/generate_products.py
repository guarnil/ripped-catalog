#!/usr/bin/env python3
"""
Régénère Ripped/Resources/Products.json — les produits scellés de chaque
extension, avec leur photo :

    python3 Scripts/generate_products.py

Source : TCGCSV (tcgcsv.com), miroir libre et quotidien du catalogue
TCGplayer, sans clé. C'est la seule source trouvée qui couvre les trois
licences avec, pour chaque extension, la liste de ses produits — sachet,
display, ETB, bundle, blisters, Vault, Double Pack — et une photo de chacun.
Son auteur demande de ne pas le solliciter à l'excès : tout est mis en cache.

Ce que l'app en fait :

- **le sachet** devient le visuel de l'extension sur l'écran d'ouverture :
  Pokémon a enfin son vrai sachet, comme Riftbound et One Piece ;
- **les items proposés** sont ceux qui existent vraiment dans l'extension —
  inutile de proposer un ETB à une extension qui n'en a pas ;
- **les photos** illustrent la feuille « Tu ouvres quoi ? ».

Les photos sont détourées par l'app : TCGplayer les publie sur fond blanc.
"""

import json
import os
import re
import sys
import time
import urllib.request

TCGCSV = "https://tcgcsv.com/tcgplayer"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "tcgcsv")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT = os.path.join(ROOT, "Ripped", "Resources", "Products.json")

# Les catégories TCGplayer des trois licences.
CATEGORIES = {"pokemon": 3, "onePiece": 68, "riftbound": 89}

# Les extensions Pokémon sans numéro dans leur nom TCGplayer : leur code de
# l'app ne se devine pas, il se lit ici. (Les « .5 » du catalogue TCGdex.)
POKEMON_NAMES = {
    "SV: Black Bolt": "SV10.5B",
    "SV: White Flare": "SV10.5W",
    "SV: Prismatic Evolutions": "SV08.5",
    "SV: Shrouded Fable": "SV06.5",
    "SV: Paldean Fates": "SV04.5",
    "SV: Scarlet & Violet 151": "SV03.5",
    "SWSH: Crown Zenith": "SWSH12.5",
    "Pokemon GO": "SWSH10.5",
    "Shining Fates": "SWSH4.5",
    "Champion's Path": "SWSH3.5",
    "ME: Ascended Heroes": "ME02.5",
}

# Nom du produit → item de l'app, dans l'ordre d'examen : la première règle qui
# s'applique gagne. Les mots écartés (`SKIP`) passent avant tout.
#
# La comparaison porte sur des mots entiers : « tin » se cache dans
# « Des-tin-ed Rivals », et écartait toute l'extension.
SKIP = ("case", "sleeved", "art bundle", "pokemon center", "exclusive",
        "deck", "decks", "kit", "tin", "tins", "promo", "pre-release", "checklane")
SKIP_PHRASES = ("[set of",)
RULES = [
    ("buildBattle", ("build & battle box", "build and battle box")),
    ("bundle",      ("booster bundle",)),
    ("etb",         ("elite trainer box",)),
    ("display",     ("booster box", "booster display")),
    ("triPack",     ("3 pack blister", "three pack blister")),
    ("blister",     ("blister",)),
    ("vault",       ("vault",)),
    ("doublePack",  ("double pack set",)),
    ("boosterSeul", ("booster pack",)),
]

# La photo de 1000 px : assez fine pour le sachet plein écran, ~100 Ko.
IMAGE = "https://tcgplayer-cdn.tcgplayer.com/product/{id}_in_1000x1000.jpg"


def get(path, cache_name):
    os.makedirs(CACHE, exist_ok=True)
    cached = os.path.join(CACHE, cache_name)
    if os.path.exists(cached):
        return json.load(open(cached))
    for attempt in range(3):
        try:
            request = urllib.request.Request(f"{TCGCSV}/{path}",
                                             headers={"User-Agent": "Ripped/1.0 (generate_products.py)"})
            with urllib.request.urlopen(request, timeout=60) as response:
                data = json.load(response)
            break
        except Exception as error:                      # noqa: BLE001
            if attempt == 2:
                print(f"  ! échec {path} : {error}", file=sys.stderr)
                return {"results": []}
            time.sleep(2 * (attempt + 1))
    json.dump(data, open(cached, "w"))
    return data


def app_code(license, group):
    """Le code d'extension de l'app pour un groupe TCGplayer, ou None."""
    name, abbreviation = group["name"], (group["abbreviation"] or "")

    if license == "pokemon":
        if name in POKEMON_NAMES:
            return POKEMON_NAMES[name]
        # « SV08: Surging Sparks » → SV08, « SWSH09: … » → SWSH9 : l'app
        # rembourre les numéros SV et ME sur deux chiffres, pas ceux de SWSH.
        match = re.match(r"([A-Z]+)(\d+):", name)
        if not match:
            return None
        series, number = match.group(1), int(match.group(2))
        return series + (f"{number:02d}" if series in ("SV", "ME") else str(number))

    if license == "onePiece":
        # Les cartes d'avant-première et de tournoi ont un suffixe : « OP02 PRE ».
        if " " in abbreviation.strip():
            return None
        match = re.match(r"(OP|EB|PRB)-?(\d+)", abbreviation)
        return match.group(1) + match.group(2) if match else None

    return abbreviation or None     # Riftbound : OGN, SFD, UNL, VEN


def item_of(name, license):
    """L'item de l'app que désigne un nom de produit, ou None."""
    lowered = name.lower()
    words = set(re.findall(r"[a-z&']+", lowered))
    if words & set(SKIP) or any(phrase in lowered for phrase in SKIP_PHRASES):
        return None
    for item, keywords in RULES:
        if any(keyword in lowered for keyword in keywords):
            return item
    # Les Extra et Premium Boosters One Piece se passent du mot « booster » :
    # « Extra Booster: Anime 25th Collection Pack » / « … Box ».
    if license == "onePiece":
        if lowered.endswith(" pack"):
            return "boosterSeul"
        if lowered.endswith(" box"):
            return "display"
    return None


def main():
    print("Produits scellés TCGCSV → Products.json")
    catalog = {}

    for license, category in CATEGORIES.items():
        groups = get(f"{category}/groups", f"groups_{category}.json")["results"]
        found = 0
        for group in groups:
            code = app_code(license, group)
            if not code:
                continue
            products = get(f"{category}/{group['groupId']}/products",
                           f"products_{category}_{group['groupId']}.json")["results"]

            items = {}
            for product in products:
                item = item_of(product["name"], license)
                if not item or item in items:
                    continue        # le premier trouvé gagne : c'est le produit simple
                items[item] = IMAGE.format(id=product["productId"])
            if not items:
                continue

            entry = catalog.setdefault(code, {"items": {}})
            entry["items"].update(items)
            if "boosterSeul" in items:
                entry["pack"] = items["boosterSeul"]
            entry["release"] = (group.get("publishedOn") or "")[:10]
            found += 1
        print(f"  {license:10} {found} extensions reliées")

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    json.dump(catalog, open(OUTPUT, "w"), separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    packs = sum(1 for e in catalog.values() if "pack" in e)
    items = sum(len(e["items"]) for e in catalog.values())
    print(f"  {len(catalog)} extensions · {packs} sachets · {items} produits "
          f"({os.path.getsize(OUTPUT) / 1024:.0f} Ko)")


if __name__ == "__main__":
    main()
