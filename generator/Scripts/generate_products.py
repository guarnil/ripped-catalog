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

Le même fichier de produits contient les **cartes** de l'extension : toute
entrée qui porte un « Number » en est une. On s'en sert pour les extensions
Pokémon que TCGdex ne connaît pas encore (`PendingCards.json`) : c'est ce qui
rend une nouveauté scannable dès sa sortie, sans attendre que le catalogue de
cartes la saisisse. Voir `provisional()` et `Scripts/publish_catalog.py`.
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
# Les extensions vendues mais dont les cartes ne sont pas encore cataloguées.
PENDING = os.path.join(ROOT, "Ripped", "Resources", "PendingExtensions.json")
# Et leurs cartes, quand TCGplayer les publie avant le catalogue de cartes.
PENDING_CARDS = os.path.join(ROOT, "Ripped", "Resources", "PendingCards.json")

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

    # Avant Épée et Bouclier, TCGplayer nomme les groupes sans numéro
    # (« SM - Unified Minds ») : tout le bloc se lit donc ici. Les
    # correspondances sont établies par date de sortie, identique de part et
    # d'autre — sauf Duo de Choc, daté du 31/01 chez TCGdex et du 01/02 ici.
    "SM Base Set": "SM1",
    "SM - Guardians Rising": "SM2",
    "SM - Burning Shadows": "SM3",
    "Shining Legends": "SM3.5",
    "SM - Crimson Invasion": "SM4",
    "SM - Ultra Prism": "SM5",
    "SM - Forbidden Light": "SM6",
    "SM - Celestial Storm": "SM7",
    "Dragon Majesty": "SM7.5",
    "SM - Lost Thunder": "SM8",
    "SM - Team Up": "SM9",
    "SM - Unbroken Bonds": "SM10",
    "SM - Unified Minds": "SM11",
    "Hidden Fates": "SM115",        # l'identifiant TCGdex de SM11.5
    "SM - Cosmic Eclipse": "SM12",

    "Kalos Starter Set": "XY0",
    "XY Base Set": "XY1",
    "XY - Flashfire": "XY2",
    "XY - Furious Fists": "XY3",
    "XY - Phantom Forces": "XY4",
    "XY - Primal Clash": "XY5",
    "XY - Roaring Skies": "XY6",
    "XY - Ancient Origins": "XY7",
    "XY - BREAKthrough": "XY8",
    "XY - BREAKpoint": "XY9",
    "XY - Fates Collide": "XY10",
    "XY - Steam Siege": "XY11",
    "XY - Evolutions": "XY12",
}

# Nom du produit → item de l'app, dans l'ordre d'examen : la première règle qui
# s'applique gagne. Les mots écartés (`SKIP`) passent avant tout.
#
# La comparaison porte sur des mots entiers : « tin » se cache dans
# « Des-tin-ed Rivals », et écartait toute l'extension.
SKIP = ("case", "sleeved", "art bundle", "pokemon center", "exclusive",
        "deck", "decks", "kit", "tin", "tins", "promo", "pre-release", "checklane")
SKIP_PHRASES = ("[set of",)
# Groupes TCGplayer à ne jamais prendre pour une extension provisoire.
PROVISIONAL_SKIP = ("release event", "promo", "exclusives", "miscellaneous",
                    "tournament", "championship", "pre-release", "prerelease")
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


# TCGCSV est repris du catalogue TCGplayer chaque nuit. Une extension ancienne
# n'y bouge plus : son fichier reste en cache indéfiniment. Ce qui bouge — la
# liste des groupes, et les cartes d'une extension qui vient d'être annoncée,
# saisies au fil des jours — est repris au plus une fois par jour.
DAY = 86400


def get(path, cache_name, max_age=None):
    os.makedirs(CACHE, exist_ok=True)
    cached = os.path.join(CACHE, cache_name)
    if os.path.exists(cached) and (max_age is None
                                   or time.time() - os.path.getmtime(cached) < max_age):
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


# Raretés TCGplayer → paliers de l'app, jumelle de `RARITY_TIERS` dans
# generate_catalog.py. Les deux sources emploient le même vocabulaire, à la
# casse près : TCGplayer écrit « Double Rare », TCGdex « Double rare ».
#
# Contrôlé sur quatre extensions cataloguées des deux côtés — ME01, ME02, SV10
# et SV151, 769 cartes — sans une divergence, ni de numéro ni de palier.
#
# Trois libellés propres à 30th Celebration sont rattachés au palier le plus
# proche, faute d'équivalent dans l'app : « Pikachu Rare » (les trente Pikachu
# d'artistes, numérotés dans l'extension) à l'illustration, « Futuristic Rare »
# aux dorées, « RBG Rare » (les trois Mew B/G/R) aux secrètes.
#
# Le palier `v` n'y figure pas : TCGplayer range les V, VMAX et VSTAR en
# « Ultra Rare », sans les distinguer. Sans conséquence ici — ce bloc est clos
# depuis 2022, et seules les extensions à venir passent par cette table.
POKEMON_TIERS = {
    "doubleRare":          ("Double Rare",),
    "illustration":        ("Illustration Rare", "Pikachu Rare"),
    "specialIllustration": ("Special Illustration Rare",),
    "ultra":               ("Ultra Rare",),
    "secret":              ("Secret Rare", "Rainbow Rare", "RBG Rare"),
    "hyper":               ("Hyper Rare", "Futuristic Rare"),
    "megaHyper":           ("Mega Hyper Rare",),
    "megaAttack":          ("Mega Attack Rare",),
    "ace":                 ("ACE SPEC Rare",),
    "shiny":               ("Shiny Rare", "Shiny Ultra Rare", "Shiny Holo Rare",
                            "Radiant Rare", "Amazing Rare"),
}

# Raretés de base : présentes dans chaque booster, ce ne sont pas des « hits ».
POKEMON_BASE = {"Common", "Uncommon", "Rare", "Holo Rare", "Promo", "None", None}

# Libellés connus mais que l'app ne suit pas : pas de signalement. Les deux
# premiers sont ceux de generate_catalog.py ; les suivants ne concernent que
# des blocs anciens, déjà catalogués par TCGdex.
POKEMON_UNTRACKED = {"Black White Rare", "Classic Collection", "Prism Rare", "Rare BREAK"}

# L'ordre d'affichage des paliers, comme dans generate_catalog.py : les trois
# premiers présents sont mis en avant, le reste passe derrière « voir plus ».
TIER_ORDER = ["doubleRare", "illustration", "specialIllustration", "v",
              "ultra", "secret", "hyper", "megaHyper", "megaAttack", "ace", "shiny"]

UNKNOWN_SEEN = set()


def tier_of(rarity_name):
    """Le palier de l'app pour un libellé TCGplayer, ou None si c'est une base."""
    if rarity_name in POKEMON_BASE:
        return None
    for tier, labels in POKEMON_TIERS.items():
        if rarity_name in labels:
            return tier
    if rarity_name not in UNKNOWN_SEEN | POKEMON_UNTRACKED:
        # Non bloquant : la carte est simplement rangée en « autre ».
        UNKNOWN_SEEN.add(rarity_name)
        print(f"  ! rareté inconnue « {rarity_name} » — à ajouter dans POKEMON_TIERS")
    return "other"


def cards_of(products):
    """Les cartes d'une extension, lues dans son fichier de produits.

    Seules les entrées qui portent un « Number » sont des cartes : tout le
    reste, ce sont les produits scellés et les cartes-code.

    Rend l'index numéro → palier au format de `CardIndex.json`, le nombre de
    cartes officielles et les paliers réellement présents.

    Les clés suivent `CardIndex.normalize` côté app : le numérateur seul, sans
    zéros de tête — « 021/128 » → « 21 ».

    Un numérateur qui n'est pas un nombre garde le numéro imprimé en entier :
    les trois Mew de 30th Celebration sont « B/RGB », « G/RGB » et « R/RGB ».
    Leur dénominateur commun en fait une souche pour `CardIndex.stem`, donc
    des variantes les unes des autres : le scan n'a plus qu'à reconnaître
    « /RGB » pour que l'app propose les trois d'un tap — la lettre, seule
    chose qui les distingue, se lit mal.
    """
    index, official, present = {}, 0, set()
    for product in products:
        fields = {e["name"]: e["value"] for e in product.get("extendedData", [])}
        printed = fields.get("Number")
        if not printed:
            continue
        number, _, total = printed.partition("/")
        if total.isdigit():
            # Le dénominateur imprimé : le nombre de cartes de l'extension,
            # que TCGCSV ne donne nulle part ailleurs. Les secrètes le
            # dépassent (« 157/128 ») sans le changer.
            official = max(official, int(total))
        key = number.strip().upper()
        if key[:1].isdigit():
            key = key.lstrip("0") or "0"
        elif total:
            key = f"{key}/{total.strip().upper()}"
        tier = tier_of(fields.get("Rarity"))
        # Le premier trouvé gagne, comme pour les produits scellés.
        index.setdefault(key, tier or "base")
        if tier and tier != "other":
            present.add(tier)
    ordered = [t for t in TIER_ORDER if t in present]
    return {"index": index, "cardCount": official,
            "main": ordered[:3], "more": ordered[3:]}


def provisional(license, group, code=None):
    """L'extension que TCGplayer vend déjà, mais que le catalogue de cartes ne
    connaît pas encore.

    TCGplayer référence une extension dès son annonce ; TCGdex, Riftcodex et
    l'OPTCG API attendent que les cartes soient saisies, souvent quelques jours
    après la sortie. Entre les deux, on sait qu'un produit existe, on connaît
    son nom, sa date et sa photo — assez pour l'ouvrir dans l'app.

    Son code est celui de TCGplayer (« 30C »), faute de mieux : le vrai arrive
    avec les cartes, et `publish_catalog.py` s'occupe alors du renommage.

    Ne sont retenues que les extensions récentes ou à venir : les vieux groupes
    sans code (promos, produits divers) n'ont pas à ressortir.
    """
    # Une vraie date de sortie est donnée à minuit pile. Les groupes permanents
    # — « Blister Exclusives », « Miscellaneous Cards & Products » — portent
    # l'horodatage du miroir TCGCSV, remis à jour chaque nuit : sans ce test,
    # ils passeraient pour des nouveautés tous les jours.
    stamp = group.get("publishedOn") or ""
    if not stamp.endswith("T00:00:00"):
        return None
    published = stamp[:10]
    if published < time.strftime("%Y-%m-%d", time.gmtime(time.time() - 120 * 86400)):
        return None
    # Les cartes de tournoi et d'avant-première ne s'ouvrent pas en booster.
    if any(phrase in group["name"].lower() for phrase in PROVISIONAL_SKIP):
        return None
    code = code or re.sub(r"[^A-Z0-9.]", "", (group.get("abbreviation") or "").upper())
    if not code:
        return None
    # Le nom tel qu'il se lira dans l'app : « ME: 30th Celebration » → « 30th
    # Celebration », la série étant déjà dite à côté.
    name = re.sub(r"^[A-Z]+\d*:\s*", "", group["name"]).strip()
    series = {"pokemon": "HS", "onePiece": "OP", "riftbound": "RB"}[license]
    match = re.match(r"(ME|SV|SWSH)\d*\s*:", group["name"])
    if license == "pokemon" and match:
        series = match.group(1)
    if license == "onePiece":
        series = (re.match(r"(PRB|EB|OP)", code) or re.match(r"(OP)", "OP")).group(1)
    return {"code": code, "name": name or group["name"], "series": series,
            "license": license, "releaseDate": published}


def main():
    print("Produits scellés TCGCSV → Products.json")
    catalog = {}
    pending = {}
    pending_cards = {}

    for license, category in CATEGORIES.items():
        groups = get(f"{category}/groups", f"groups_{category}.json", max_age=DAY)["results"]
        found = 0
        for group in groups:
            code = app_code(license, group)
            candidate = provisional(license, group, code)
            if not code:
                if not candidate:
                    continue
                code = candidate["code"]
            # Candidates d'extension récente : `publish_catalog.py` écartera
            # celles que le catalogue de cartes connaît déjà. Deux groupes
            # peuvent partager un code — la boîte et sa collection : on garde
            # le nom le plus court, celui de l'extension elle-même.
            if candidate:
                known = pending.get(candidate["code"])
                if not known or len(candidate["name"]) < len(known["name"]):
                    pending[candidate["code"]] = candidate
            # Une extension provisoire se complète jour après jour chez
            # TCGplayer, qui saisit ses cartes au fil de l'eau : son fichier
            # n'est gardé qu'une journée.
            products = get(f"{category}/{group['groupId']}/products",
                           f"products_{category}_{group['groupId']}.json",
                           max_age=DAY if candidate else None)["results"]

            # Les cartes d'une extension que le catalogue ne connaît pas
            # encore. Les groupes supplémentaires sont écartés : la collection
            # Classic de 30th Celebration porte le code de l'extension, mais
            # ses cartes gardent le numéro de leur set d'origine (« 4/102 »),
            # qui se confondrait avec celui de l'extension elle-même.
            if (candidate and license == "pokemon" and not group.get("isSupplemental")
                    and pending.get(code) is candidate):
                read = cards_of(products)
                if read["index"]:
                    pending_cards[code] = read

            items = {}
            for product in products:
                item = item_of(product["name"], license)
                if not item or item in items:
                    continue        # le premier trouvé gagne : c'est le produit simple
                items[item] = IMAGE.format(id=product["productId"])
            if not items:
                # Une extension provisoire sans aucun produit scellé n'a rien à
                # ouvrir. Deux groupes peuvent porter le même code — la boîte et
                # sa collection : celui qui n'apporte rien ne doit pas effacer
                # l'autre.
                if code in pending and code not in catalog:
                    pending.pop(code, None)
                    pending_cards.pop(code, None)
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
    json.dump(pending, open(PENDING, "w"), separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    pending_cards = {c: v for c, v in pending_cards.items() if c in pending}
    json.dump(pending_cards, open(PENDING_CARDS, "w"), separators=(",", ":"),
              ensure_ascii=False, sort_keys=True)
    if pending:
        print("  en attente de cartes : "
              + ", ".join(f"{c} ({e['name']}, {e['releaseDate']})" for c, e in sorted(pending.items())))
    for code, read in sorted(pending_cards.items()):
        print(f"    {code} : {len(read['index'])} cartes lues chez TCGplayer "
              f"({read['cardCount']} officielles) · "
              + ", ".join(read["main"] + read["more"]))
    packs = sum(1 for e in catalog.values() if "pack" in e)
    items = sum(len(e["items"]) for e in catalog.values())
    print(f"  {len(catalog)} extensions · {packs} sachets · {items} produits "
          f"({os.path.getsize(OUTPUT) / 1024:.0f} Ko)")


if __name__ == "__main__":
    main()
