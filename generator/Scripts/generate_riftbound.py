#!/usr/bin/env python3
"""
Régénère le catalogue Riftbound de Ripped, en pendant de generate_catalog.py
pour Pokémon :

    python3 Scripts/generate_riftbound.py

Trois sorties :

- Ripped/Models/Catalog+Riftbound.swift   les extensions, pour le sélecteur ;
- Ripped/Resources/CardIndexRiftbound.json numéro → palier, même format que
  CardIndex.json : c'est ce qui résout un scan hors ligne ;
- Ripped/Resources/RiftboundCards.json     numéro → nom, visuel, produit
  Cardmarket : ce que TCGdex renvoie à la demande pour Pokémon, et qu'aucune
  API Riftbound ne sert d'un seul tenant.

Deux sources, sans clé :

- Riftcodex (api.riftcodex.com), catalogue ouvert, en anglais. Ses visuels
  sont servis par le CDN de Riot (cmsassets.rgpub.io) ;
- les fichiers publics de Cardmarket (jeu 22 = Riftbound). Le catalogue
  produits sert ici à relier chaque carte à son identifiant Cardmarket ; la
  cote, elle, est lue par l'app dans le guide des prix du jour.

Le rapprochement carte → produit Cardmarket est la seule partie délicate :
Cardmarket ne publie pas de numéro de collection, et une carte et ses
variantes (art alternatif, overnumbered, signature) portent le même nom. Dans
une extension, Cardmarket crée toujours la carte de base avant ses variantes :
à nom égal, l'ordre des identifiants suit donc l'ordre des variantes. Vérifié
sur Origins et Spiritforged, où la cote croît avec l'identifiant dans 87
groupes sur 88.
"""

import collections
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request

RIFTCODEX = "https://api.riftcodex.com"
CARDMARKET = "https://downloads.s3.cardmarket.com/productCatalog/productList/products_singles_22.json"
# Riftcodex refuse le User-Agent par défaut d'urllib (403).
HEADERS = {"User-Agent": "Ripped/1.0 (generate_riftbound.py)"}

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "riftbound")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT = os.path.join(ROOT, "Ripped", "Models", "Catalog+Riftbound.swift")
CARD_INDEX = os.path.join(ROOT, "Ripped", "Resources", "CardIndexRiftbound.json")
CARD_DETAILS = os.path.join(ROOT, "Ripped", "Resources", "RiftboundCards.json")

# Extensions qui ne s'ouvrent pas en booster : promos (PR, OPP, JDG) et la
# boîte de démarrage Proving Grounds, dont le contenu est fixe.
SKIP = {"PR", "OPP", "JDG", "OGS"}

# Les paliers de l'app, dans l'ordre d'affichage. Les trois premiers présents
# sont mis en avant, le reste passe derrière « voir plus ».
TIER_ORDER = ["epic", "altArt", "overnumbered", "signature"]

# Raretés de base : un booster en contient toujours, ce ne sont pas des hits.
# La Rare en fait partie — chaque booster en a deux.
BASE_RARITIES = {"Common", "Uncommon", "Rare", "Promo"}

# Visuel de chaque extension : le sachet, tel que photographié pour la
# boutique officielle de Riot (merch.riotgames.com, CDN Sanity du projet
# Riot). Aucune API ne le donne, et la boutique ne le publie qu'en compagnie
# du display : le sachet en est découpé par le CDN lui-même (`rect=`, en
# pixels de l'image source 2560 × 2560). Choisi et cadré à la main — une
# nouvelle extension reste sans visuel (le halo de couleur prend le relais)
# tant qu'elle n'est pas ajoutée ici.
SHOP = "https://cdn.sanity.io/images/dsfx7636/consumer_products_live"
PACK_ART = {
    "OGN": f"{SHOP}/df0a06134808a2c0e5f595cf04eb1e98ef446a78-2560x2560.png?rect=1708,744,722,1204&w=400&fm=png",
    "SFD": f"{SHOP}/84d112ee2796307fd629bf7f603914e5e86ca337-2560x2560.png?rect=624,168,1316,2224&w=400&fm=png",
    "UNL": f"{SHOP}/aa46ce39877fd9798ad45022c5ebc753f9d4e374-2560x2560.png?rect=1710,780,720,1202&w=400&fm=png",
    "VEN": f"{SHOP}/e0a08c2f8d9e931a799617aaf74cb1f89a71e148-2560x2560.png?rect=1720,950,734,1210&w=400&fm=png",
}

# Le visuel du CDN de Riot fait 744 × 1039 en PNG, 1,4 Mo. Redimensionné et
# converti par le CDN lui-même, il tombe à 120 Ko — la définition du `high.png`
# de TCGdex, qui suffit à la carte révélée.
IMAGE_PARAMS = "&w=600&fm=jpg"


def get(url, cache_name):
    os.makedirs(CACHE, exist_ok=True)
    cached = os.path.join(CACHE, cache_name)
    if os.path.exists(cached):
        return json.load(open(cached))
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=45) as response:
                data = json.load(response)
            break
        except Exception as error:                      # noqa: BLE001
            if attempt == 2:
                sys.exit(f"  ! échec {url} : {error}")
            time.sleep(1.5 * (attempt + 1))
    json.dump(data, open(cached, "w"))
    return data


def all_cards():
    """Toutes les cartes Riftcodex, page par page, dédoublonnées."""
    cards, page = {}, 1
    while True:
        data = get(f"{RIFTCODEX}/cards?size=100&page={page}", f"cards_{page}.json")
        for card in data["items"]:
            cards[card["riftbound_id"]] = card      # Vendetta liste des doublons
        if page >= data["pages"]:
            return list(cards.values())
        page += 1


def card_key(card):
    """La clé de la carte dans son extension, telle que l'app la normalise.

    `unl-116a-219` → `116A`, `ogn-007-298` → `7`, `unl-229*-219` → `229*`,
    `ven-sp4-006` → `SP4`. Mêmes règles que `CardIndex.normalize` côté Swift :
    zéros de tête retirés du nombre, suffixe en capitales.
    """
    middle = card["riftbound_id"].split("-")[1].upper()
    match = re.fullmatch(r"(\d+)(.*)", middle)
    if not match:
        return middle                     # SP4, R06, T03 : gardés tels quels
    digits, suffix = match.groups()
    return (digits.lstrip("0") or "0") + suffix


def tier_of(card):
    """Le palier de l'app, ou None pour une carte de base.

    Les indicateurs priment sur le libellé : Riftcodex classe les variantes en
    « Showcase » sur Origins et Spiritforged, mais en « Rare » ou « Epic » sur
    Unleashed et Vendetta. Le libellé seul rangerait un art alternatif
    d'Unleashed parmi les communes.
    """
    flags = card["metadata"]
    if flags["signature"]:
        return "signature"
    if flags["overnumbered"]:
        return "overnumbered"
    if flags["alternate_art"]:
        return "altArt"
    rarity = card["classification"]["rarity"]
    if rarity == "Showcase":
        return "altArt"
    if rarity == "Epic":
        return "epic"
    return None


def variant_rank(card):
    """Ordre de création des variantes chez Cardmarket : la base d'abord."""
    return TIER_ORDER.index(tier_of(card)) if tier_of(card) in TIER_ORDER[1:] else 0


def normalized_name(name):
    """« Vi - Piltover Enforcer (Signature) » et « Vi, Piltover Enforcer »
    désignent la même carte : Cardmarket sépare au virgule, Riftcodex au tiret,
    et seul Riftcodex précise la variante."""
    name = re.sub(r"\s*\((Alternate Art|Overnumbered|Signature|Showcase|Starter)[^)]*\)", "", name)
    name = name.replace(" - ", ", ")
    name = unicodedata.normalize("NFKD", name).lower()
    return re.sub(r"[^a-z0-9]", "", name)


def display_name(name):
    """Le nom affiché dans l'app, sans la variante : le palier la dit déjà."""
    return re.sub(r"\s*\((Alternate Art|Overnumbered|Signature)\)", "", name)


def cardmarket_expansion(set_info, cards, products):
    """L'extension Cardmarket d'un set : celle que donne Riftcodex, ou à défaut
    celle qui partage le plus de noms de cartes avec lui."""
    declared = set_info.get("cardmarket_id")
    if isinstance(declared, str) and declared.isdigit():
        return int(declared)
    names = {normalized_name(c["name"]) for c in cards}
    by_expansion = collections.defaultdict(set)
    for product in products:
        by_expansion[product["idExpansion"]].add(normalized_name(product["name"]))
    best = max(by_expansion, key=lambda e: len(names & by_expansion[e]))
    return best


def match_products(cards, products):
    """carte → idProduct Cardmarket, à nom égal dans l'ordre des variantes.

    Un groupe dont les effectifs diffèrent des deux côtés est ambigu : seule sa
    carte de base est reliée, au plus petit identifiant. Mieux vaut une variante
    sans cote qu'une variante affichée au prix de sa version commune.
    """
    groups = collections.defaultdict(list)
    for product in products:
        groups[normalized_name(product["name"])].append(product)

    matched, ambiguous = {}, 0
    by_name = collections.defaultdict(list)
    for card in cards:
        by_name[normalized_name(card["name"])].append(card)

    for name, group in by_name.items():
        group.sort(key=lambda c: (variant_rank(c), c["collector_number"], c["riftbound_id"]))
        candidates = sorted(groups.get(name, []), key=lambda p: p["idProduct"])
        if not candidates:
            continue
        if len(candidates) == len(group):
            for card, product in zip(group, candidates):
                matched[card["riftbound_id"]] = product["idProduct"]
        else:
            ambiguous += 1
            base = [c for c in group if variant_rank(c) == 0]
            if len(base) == 1:
                matched[base[0]["riftbound_id"]] = candidates[0]["idProduct"]
    return matched, ambiguous


def swift_string(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main():
    print("Catalogue Riftbound → Swift")
    sets = get(f"{RIFTCODEX}/sets?size=100", "sets.json")["items"]
    cards = all_cards()
    products = get(CARDMARKET, "cardmarket_products.json")["products"]
    print(f"  {len(cards)} cartes Riftcodex, {len(products)} produits Cardmarket")

    entries, index, details = [], {}, {}
    for set_info in sets:
        code = set_info["set_id"]
        if code in SKIP:
            continue
        set_cards = [c for c in cards if c["set"]["set_id"] == code]
        if not set_cards:
            continue

        expansion = cardmarket_expansion(set_info, set_cards, products)
        matched, ambiguous = match_products(
            set_cards, [p for p in products if p["idExpansion"] == expansion])

        tiers, by_key, card_details = set(), {}, {}
        for card in set_cards:
            key = card_key(card)
            tier = tier_of(card)
            if tier:
                tiers.add(tier)
            by_key[key] = tier or "base"
            entry = {"n": display_name(card["name"]), "r": card["classification"]["rarity"]}
            image = (card.get("media") or {}).get("image_url")
            if image:
                entry["i"] = image + IMAGE_PARAMS
            if card["riftbound_id"] in matched:
                entry["cm"] = matched[card["riftbound_id"]]
            card_details[key] = entry
        index[code] = by_key
        details[code] = card_details

        # Le total imprimé (« /298 ») : le plus fréquent des suffixes d'identifiant.
        totals = collections.Counter(c["riftbound_id"].rsplit("-", 1)[-1] for c in set_cards)
        printed = int(next(t for t, _ in totals.most_common() if t.isdigit()))

        ordered = [t for t in TIER_ORDER if t in tiers]
        entries.append({
            "code": code,
            "name": set_info["name"],
            "release": (set_info.get("published_on") or "")[:10],
            "cards": printed,
            "main": ordered[:3],
            "more": ordered[3:],
        })
        priced = sum(1 for c in set_cards if c["riftbound_id"] in matched)
        print(f"    {code:4} {set_info['name']:14} {len(set_cards):4} cartes · "
              f"Cardmarket {expansion} · {priced} reliées, {ambiguous} groupes ambigus")

    entries.sort(key=lambda e: e["release"], reverse=True)
    years = [e["release"][:4] for e in entries if e["release"]]
    span = f"{min(years)} → {max(years)}" if years else ""

    lines = [
        "// Généré par Scripts/generate_riftbound.py — ne pas modifier à la main.",
        "// Source : Riftcodex (api.riftcodex.com) et catalogue Cardmarket, sans clé.",
        f"// Régénéré le {time.strftime('%Y-%m-%d')} · {len(entries)} extensions.",
        "",
        "import Foundation",
        "",
        "extension Catalog {",
        "",
        "    static let riftboundSeries: [PackSeries] = [",
        f"        PackSeries(id: \"RB\", label: \"Riftbound\", years: {swift_string(span)}, license: .riftbound),",
        "    ]",
        "",
        "    static let riftboundExtensions: [PackExtension] = [",
    ]
    for e in entries:
        lines.append(
            f"        PackExtension(code: {swift_string(e['code'])}, "
            f"name: {swift_string(e['name'])}, "
            f"series: \"RB\", "
            f"releaseDate: {swift_string(e['release'])}, "
            f"cardCount: {e['cards']}, "
            f"logoURL: {swift_string(PACK_ART.get(e['code'], ''))}, "
            f"mainRarities: [{', '.join('.' + t for t in e['main'])}], "
            f"moreRarities: [{', '.join('.' + t for t in e['more'])}], "
            f"license: .riftbound),"
        )
    lines += ["    ]", "}", ""]
    with open(OUTPUT, "w") as handle:
        handle.write("\n".join(lines))

    json.dump(index, open(CARD_INDEX, "w"), separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    json.dump(details, open(CARD_DETAILS, "w"), separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    for path in (CARD_INDEX, CARD_DETAILS):
        print(f"  {os.path.relpath(path, ROOT)} ({os.path.getsize(path) / 1024:.0f} Ko)")
    print(f"  {len(entries)} extensions écrites dans {os.path.relpath(OUTPUT, ROOT)}")
    missing = [e["code"] for e in entries if e["code"] not in PACK_ART]
    if missing:
        print(f"  ! sans visuel de sachet : {', '.join(missing)} — à ajouter dans PACK_ART")


if __name__ == "__main__":
    main()
