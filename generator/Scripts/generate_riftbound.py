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

Trois sources, sans clé :

- Riftcodex (api.riftcodex.com), catalogue ouvert, en anglais. Ses visuels
  sont servis par le CDN de Riot (cmsassets.rgpub.io) ;
- les fichiers publics de Cardmarket (jeu 22 = Riftbound). Le catalogue
  produits sert ici à relier chaque carte à son identifiant Cardmarket ; la
  cote, elle, est lue par l'app dans le guide des prix du jour ;
- TCGCSV (tcgcsv.com), miroir du catalogue TCGplayer, pour les **promos
  numérotés d'après une extension** — les cartes des sachets Nexus Night, des
  kits d'avant-première et des tournois. Riftcodex ne les connaît pas, et
  elles portent le dénominateur de l'extension de la saison : sans elles,
  l'app enregistre la carte de booster de même numéro à leur place. Voir
  `promo_cards()`.

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
# TCGCSV : le miroir libre du catalogue TCGplayer, déjà lu par
# generate_products.py. Seule source des promos numérotés — voir promo_cards().
TCGCSV = "https://tcgcsv.com/tcgplayer"
TCG_CATEGORY = 89
TCG_IMAGE = "https://tcgplayer-cdn.tcgplayer.com/product/{id}_in_1000x1000.jpg"
# Un groupe TCGplayer est repris chaque nuit, et une saison d'événements ajoute
# des promos à une extension déjà sortie : ces fichiers ne se gardent qu'un jour.
DAY = 86400
# Riftcodex refuse le User-Agent par défaut d'urllib (403).
HEADERS = {"User-Agent": "Ripped/1.0 (generate_riftbound.py)"}

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "riftbound")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT = os.path.join(ROOT, "Ripped", "Models", "Catalog+Riftbound.swift")
CARD_INDEX = os.path.join(ROOT, "Ripped", "Resources", "CardIndexRiftbound.json")
CARD_DETAILS = os.path.join(ROOT, "Ripped", "Resources", "RiftboundCards.json")

# Extensions qui ne s'ouvrent pas en booster : promos (PR, OPP, JDG) et la
# boîte de démarrage Proving Grounds, dont le contenu est fixe. Elles n'ont pas
# d'entrée au catalogue ; leurs cartes numérotées d'après une extension du
# catalogue, elles, rejoignent cette extension — voir promo_cards().
SKIP = {"PR", "OPP", "JDG", "OGS"}

# Les séries de promos dont les cartes portent le numéro d'une extension : les
# sachets Nexus Night, les kits d'avant-première et les prix de tournoi.
# Proving Grounds n'y est pas : sa boîte a sa propre numérotation (« /024 »).
PROMO_SETS = {"PR", "OPP", "JDG"}

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


def get(url, cache_name, max_age=None):
    os.makedirs(CACHE, exist_ok=True)
    cached = os.path.join(CACHE, cache_name)
    if os.path.exists(cached) and (max_age is None
                                   or time.time() - os.path.getmtime(cached) < max_age):
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


def promo_name(name):
    """Le nom d'un promo, réduit à ce qui l'identifie chez Cardmarket.

    Cardmarket range les promos dans des extensions à part et ne dit pas d'où
    ils viennent ; TCGplayer, lui, précise le tirage entre parenthèses
    (« (Vendetta) », « (Champion) », « (R01b) »). On enlève tout cela avant de
    comparer, puis on applique les mêmes règles que pour les cartes de booster.
    """
    name = re.sub(r"\s*\((Promo|Champion|Top 8|GG EZ|R0\d[a-c]|"
                  r"Origins|Spiritforged|Unleashed|Vendetta)\)", "", name)
    return normalized_name(name)


def promo_tier(name, rarity):
    """Le palier de l'app pour un promo, ou None pour une carte de base.

    Riftcodex ne connaît pas ces cartes : il n'y a donc ni indicateur
    `alternate_art` ni `signature` à lire, seulement la rareté TCGplayer et le
    tirage que son nom précise. « Promo » n'est pas un palier — c'est la rareté
    que TCGplayer donne à tout prix de tournoi, quelle que soit la carte.
    """
    for marker, tier in (("Signature", "signature"),
                         ("Overnumbered", "overnumbered"),
                         ("Alternate Art", "altArt")):
        if f"({marker}" in name:
            return tier
    if rarity == "Showcase":
        return "altArt"
    if rarity == "Epic":
        return "epic"
    return None


def promo_cards(denominators, index, details, cardmarket, tiers):
    """Verse dans leur extension d'accueil les promos qui portent son numéro.

    Un promo Riftbound n'est pas tiré d'un booster — sachets Nexus Night, kits
    d'avant-première, prix de tournoi — mais il porte le **dénominateur de
    l'extension de la saison** (« 069b/166 »), et c'est tout ce que l'app lit
    au scan. Sans lui dans l'index, le repli de `CardIndex.resolveKey` sur la
    souche enregistre la carte de booster de même numéro à sa place, avec sa
    cote : une Mel promo passait pour la Mel du booster.

    Ne sont versées que les cartes dont la clé **manque** à l'extension. Les
    autres promos reprennent tel quel le numéro d'une carte de booster (le
    sachet Nexus Night est fait aux trois quarts de communes réimprimées) :
    rien ne les distingue à la lecture, et les confondre est sans conséquence.

    Riftcodex ignore ces cartes — aucun de ses promos ne porte un dénominateur
    Vendetta —, d'où le détour par TCGplayer, qui donne le numéro imprimé, la
    rareté et un visuel. Le lien vers Cardmarket, lui, n'aboutit que pour un nom
    unique dans ses extensions de promos : les six runes d'une saison y portent
    le même, et mieux vaut pas de cote qu'une cote prise à une autre carte.
    """
    groups = get(f"{TCGCSV}/{TCG_CATEGORY}/groups", "tcg_groups.json", max_age=DAY)["results"]
    by_name = collections.defaultdict(list)
    for product in cardmarket:
        by_name[promo_name(product["name"])].append(product)

    added = linked = 0
    for group in groups:
        if (group["abbreviation"] or "").upper() not in PROMO_SETS:
            continue
        products = get(f"{TCGCSV}/{TCG_CATEGORY}/{group['groupId']}/products",
                       f"tcg_products_{group['groupId']}.json", max_age=DAY)["results"]
        for product in products:
            fields = {e["name"]: e["value"] for e in product.get("extendedData", [])}
            printed = re.fullmatch(r"\s*(\d+)([A-Za-z*]?)\s*/\s*(\d+)\s*", fields.get("Number") or "")
            if not printed:
                continue                # une rune (« R01b »), un jeton : pas de numéro d'extension
            digits, suffix, total = printed.groups()
            code = denominators.get(int(total))
            if not code:
                continue                # le dénominateur d'une extension hors catalogue
            key = (digits.lstrip("0") or "0") + suffix.upper()
            if key in index[code]:
                continue                # le numéro d'une carte de booster : déjà connu
            name, rarity = product["name"], fields.get("Rarity")
            tier = promo_tier(name, rarity)
            if tier and tier not in tiers[code]:
                # Le palier n'est annoncé par aucune carte du booster : l'app ne
                # l'afficherait pas pour cette extension.
                print(f"  ! {code} {key} : palier {tier} absent du booster — versé en base")
                tier = None
            index[code][key] = tier or "base"
            # Le tirage est la seule chose qui distingue ce promo de la carte de
            # booster du même nom : il se dit dans le nom, faute d'un palier.
            label = display_name(name)
            entry = {"n": label if "Promo" in label else f"{label} (Promo)",
                     "r": rarity or "Promo",
                     "i": TCG_IMAGE.format(id=product["productId"])}
            candidates = by_name.get(promo_name(name), [])
            if len(candidates) == 1:
                entry["cm"] = candidates[0]["idProduct"]
                linked += 1
            details[code][key] = entry
            added += 1
    return added, linked


def swift_string(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main():
    print("Catalogue Riftbound → Swift")
    sets = get(f"{RIFTCODEX}/sets?size=100", "sets.json")["items"]
    cards = all_cards()
    products = get(CARDMARKET, "cardmarket_products.json")["products"]
    print(f"  {len(cards)} cartes Riftcodex, {len(products)} produits Cardmarket")

    entries, index, details = [], {}, {}
    # De quoi verser ensuite les promos : le dénominateur imprimé de chaque
    # extension, ses paliers, et les extensions Cardmarket déjà prises.
    denominators, set_tiers, booster_expansions = {}, {}, set()
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
        denominators[printed] = code
        set_tiers[code] = tiers
        booster_expansions.add(expansion)

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

    promos, linked = promo_cards(denominators, index, details,
                                 [p for p in products
                                  if p["idExpansion"] not in booster_expansions],
                                 set_tiers)
    print(f"  {promos} promos numérotés versés dans leur extension d'accueil, "
          f"{linked} reliés à Cardmarket")

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
