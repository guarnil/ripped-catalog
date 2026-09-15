#!/usr/bin/env python3
"""
Régénère le catalogue d'extensions de Ripped depuis TCGdex (API française,
sans clé) : Ripped/Models/Catalog+Generated.swift

    python3 Scripts/generate_catalog.py

Les identifiants TCGdex sont déjà ceux de l'app (sv08, swsh12…), le code
affiché est simplement l'identifiant en capitales.

Ce que le script prend à l'API : identifiant, nom officiel, série, date de
sortie, nombre de cartes, et l'URL du visuel officiel. Aucune image n'est
téléchargée ici : seule l'adresse est écrite dans le catalogue, l'app va
chercher le visuel à l'usage et le garde en cache sur l'appareil.

Les paliers proposés par extension sont lus dans les cartes réelles : le
script compte les raretés de chaque extension et ne garde que celles qu'elle
contient vraiment. La table RARITY_TIERS traduit les libellés de l'API en
paliers de l'app.
"""

import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "https://api.tcgdex.net/v2/fr"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT = os.path.join(ROOT, "Ripped", "Models", "Catalog+Generated.swift")
CARD_INDEX = os.path.join(ROOT, "Ripped", "Resources", "CardIndex.json")

# Séries suivies, dans l'ordre d'affichage du sélecteur.
SERIES_ORDER = ["me", "sv", "swsh"]

# Les paliers de l'app, et les libellés que l'API leur associe.
# C'est la seule table à toucher quand une extension introduit une rareté
# inédite : tout le reste (quels paliers proposer, dans quel ordre) est lu
# extension par extension dans les cartes réelles.
RARITY_TIERS = {
    "doubleRare":          ["Double rare"],
    "v":                   ["Holo Rare V", "Holo Rare VMAX", "Holo Rare VSTAR"],
    "illustration":        ["Illustration rare"],
    "specialIllustration": ["Special illustration rare"],
    "ultra":               ["Ultra Rare"],
    "secret":              ["Secret Rare"],
    "hyper":               ["Hyper rare"],
    "megaHyper":           ["Mega Hyper Rare"],   # bloc Méga-Évolution
    "ace":                 ["ACE SPEC Rare"],
    "shiny":               ["Shiny rare", "Shiny Ultra Rare", "Radiant Rare", "Amazing Rare"],
}

# Raretés de base : présentes dans chaque booster, ce ne sont pas des « hits ».
BASE_RARITIES = {"Common", "Uncommon", "Rare", "Holo Rare", "Promo", "None", None}

# Ordre d'affichage : les trois premiers paliers présents sont mis en avant,
# le reste passe derrière « voir plus ».
TIER_ORDER = ["doubleRare", "illustration", "specialIllustration", "v",
              "ultra", "secret", "hyper", "megaHyper", "megaAttack", "ace", "shiny"]

# Une extension sans aucune rareté de chase (les promos) garde ce minimum.
FALLBACK_TIERS = ["ultra"]

# Paliers que l'API ne connaît pas encore, ajoutés à la main.
#
# « Méga attaque rare » (MA) est apparue en novembre 2025 : TCGdex classe encore
# ses sept cartes (Héros Transcendants, n° 265 à 271) en « Ultra Rare ». À
# étendre quand une extension en introduit de nouvelles.
EXTRA_TIERS = {
    "me02.5": ["megaAttack"],
}

# Cartes que l'API classe mal, corrigées au numéro. Même cause : la rareté
# « Méga attaque rare » n'existe pas encore chez TCGdex, qui range ses sept
# cartes en « Ultra Rare ».
CARD_OVERRIDES = {
    "me02.5": {tier: list(range(265, 272)) for tier in ["megaAttack"]},
}

# Sets à écarter : produits qui ne s'ouvrent pas en booster à part entière.
SKIP = {"sve", "mee", "swsh4.5sv"}          # decks d'énergies, Shiny Vault
SKIP_SUFFIXES = ("tg", "gg")                # sous-collections (galeries)


def fetch(path):
    os.makedirs(CACHE, exist_ok=True)
    cached = os.path.join(CACHE, re.sub(r"[^a-zA-Z0-9._-]", "_", path) + ".json")
    if os.path.exists(cached):
        return json.load(open(cached))
    for attempt in range(3):
        try:
            with urllib.request.urlopen(f"{API}/{path}", timeout=30) as response:
                data = json.load(response)
            break
        except Exception as error:                      # noqa: BLE001
            if attempt == 2:
                print(f"  ! échec {path} : {error}", file=sys.stderr)
                return None
            time.sleep(1.5 * (attempt + 1))
    if isinstance(data, dict):
        data.pop("cards", None)   # la liste des cartes ne sert pas ici
    json.dump(data, open(cached, "w"))
    return data


def cards_of(set_id):
    """(localId, rareté) de toutes les cartes d'une extension, mises en cache."""
    cached = os.path.join(CACHE, "cards", f"{set_id}.json")
    os.makedirs(os.path.dirname(cached), exist_ok=True)
    if os.path.exists(cached):
        return json.load(open(cached))

    query = {"query": '{ cards(filters:{id:"%s-"}, pagination:{page:1, itemsPerPage:600}) '
                      '{ id localId rarity } }' % set_id}
    request = urllib.request.Request(
        "https://api.tcgdex.net/v2/graphql",
        data=json.dumps(query).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.load(response)
        cards = {c["localId"]: c["rarity"]
                 for c in payload.get("data", {}).get("cards", [])
                 if c["id"].rsplit("-", 1)[0] == set_id}
    except Exception as error:                          # noqa: BLE001
        print(f"  ! cartes indisponibles pour {set_id} : {error}", file=sys.stderr)
        cards = {}
    json.dump(cards, open(cached, "w"))
    return cards


def tier_of(rarity_name):
    """Le palier de l'app pour un libellé de l'API, ou None si c'est une base."""
    if rarity_name in BASE_RARITIES:
        return None
    for tier, labels in RARITY_TIERS.items():
        if rarity_name in labels:
            return tier
    return "other"


UNKNOWN_SEEN = set()

# Libellés connus mais que l'app ne suit pas (encore) : pas de signalement.
# « Black White Rare » : Foudre Noire et Flamme Blanche (SV10.5), sans palier
# équivalent dans l'app.
KNOWN_UNTRACKED = {"Black White Rare"}


def rarities_of(set_id):
    """Les paliers réellement présents dans une extension, via GraphQL.

    Il n'existe pas de filtre par extension : on filtre sur le préfixe
    d'identifiant, puis on ne garde que les cartes du bon set (« sv08- »
    ramène aussi « sv08.5- »).
    """
    present = set()
    for rarity_name in cards_of(set_id).values():
        tier = tier_of(rarity_name)
        if tier == "other" and rarity_name not in UNKNOWN_SEEN | KNOWN_UNTRACKED:
            # Non bloquant : la carte est simplement ignorée. Le signaler
            # permet à la mise à jour automatique d'ouvrir un ticket.
            UNKNOWN_SEEN.add(rarity_name)
            print(f"  ! rareté inconnue « {rarity_name} » ({set_id}) — à ajouter dans RARITY_TIERS")
        if tier and tier != "other":
            present.add(tier)
    present.update(EXTRA_TIERS.get(set_id, []))
    ordered = [t for t in TIER_ORDER if t in present] or FALLBACK_TIERS
    return ordered[:3], ordered[3:]


def era_of(set_id):
    for era in SERIES_ORDER:
        if set_id.lower().startswith(era):
            return era
    return None


def is_promo(set_id):
    return set_id.lower().endswith("p")


def swift_string(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def rarity_list(names):
    return "[" + ", ".join("." + name for name in names) + "]"


def main():
    print("Catalogue TCGdex → Swift")
    index = fetch("sets")
    if index is None:
        sys.exit("impossible de lire la liste des extensions")

    candidates = [
        s["id"] for s in index
        if era_of(s["id"])
        and s["id"].lower() not in SKIP
        and not s["id"].lower().endswith(SKIP_SUFFIXES)
    ]
    print(f"  {len(candidates)} extensions candidates, récupération du détail…")

    with ThreadPoolExecutor(max_workers=6) as pool:
        details = list(pool.map(lambda sid: fetch(f"sets/{sid}"), candidates))

    print("  lecture des raretés réelles, extension par extension…")
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(rarities_of, candidates))

    entries = []
    for detail in details:
        if not detail or "name" not in detail:
            continue
        set_id = detail["id"]
        era = era_of(set_id)
        main_rarities, more_rarities = rarities_of(set_id)
        entries.append({
            "code": set_id.upper(),
            "name": detail["name"],
            "series": "HS" if is_promo(set_id) else era.upper(),
            "seriesName": detail.get("serie", {}).get("name", era.upper()),
            "release": detail.get("releaseDate") or "",
            "cards": detail.get("cardCount", {}).get("official") or 0,
            # URL de base, sans extension : TCGdex sert .webp (pas toujours) et .png
            "logo": detail.get("logo") or "",
            "main": main_rarities,
            "more": more_rarities,
        })

    # Plus récent en premier, comme dans le sélecteur.
    entries.sort(key=lambda e: e["release"], reverse=True)

    series_names = {}
    for entry in entries:
        if entry["series"] != "HS":
            series_names.setdefault(entry["series"], entry["seriesName"])

    series_rows = []
    for era in SERIES_ORDER:
        key = era.upper()
        if key not in series_names:
            continue
        years = [e["release"][:4] for e in entries if e["series"] == key and e["release"]]
        span = f"{min(years)} → {max(years)}" if years else ""
        series_rows.append((key, series_names[key], span))
    series_rows.append(("HS", "Hors série", "coffrets, promos, tri-packs"))

    lines = [
        "// Généré par Scripts/generate_catalog.py — ne pas modifier à la main.",
        "// Source : TCGdex (api.tcgdex.net), API française, sans clé.",
        f"// Régénéré le {time.strftime('%Y-%m-%d')} · {len(entries)} extensions.",
        "//",
        "// Métadonnées seulement : ni logo ni symbole officiel n'est repris ici,",
        "// la vignette de chaque extension reste un halo généré depuis son code.",
        "",
        "import Foundation",
        "",
        "extension Catalog {",
        "",
        "    static let generatedSeries: [PackSeries] = [",
    ]
    for key, label, span in series_rows:
        lines.append(
            f"        PackSeries(id: {swift_string(key)}, "
            f"label: {swift_string(label)}, years: {swift_string(span)}),"
        )
    lines += [
        "    ]",
        "",
        "    static let generatedExtensions: [PackExtension] = [",
    ]
    for entry in entries:
        lines.append(
            f"        PackExtension(code: {swift_string(entry['code'])}, "
            f"name: {swift_string(entry['name'])}, "
            f"series: {swift_string(entry['series'])}, "
            f"releaseDate: {swift_string(entry['release'])}, "
            f"cardCount: {entry['cards']}, "
            f"logoURL: {swift_string(entry['logo'])}, "
            f"mainRarities: {rarity_list(entry['main'])}, "
            f"moreRarities: {rarity_list(entry['more'])}),"
        )
    lines += ["    ]", "}", ""]

    with open(OUTPUT, "w") as handle:
        handle.write("\n".join(lines))

    # Index numéro → palier, pour résoudre une carte scannée hors ligne.
    index = {}
    for entry in entries:
        set_id = entry["code"].lower()
        by_number = {}
        for local_id, rarity_name in cards_of(set_id).items():
            # Clés normalisées : « 001 » et « 1 » désignent la même carte, et un
            # scan ne rendra jamais les zéros de tête. Les identifiants non
            # numériques (promos « SWSH001 », galeries) gardent leur forme.
            key = local_id.lstrip("0") if local_id.isdigit() else local_id
            by_number[key or "0"] = tier_of(rarity_name) or "base"

        for tier, numbers in CARD_OVERRIDES.get(set_id, {}).items():
            for number in numbers:
                by_number[str(number)] = tier
        if by_number:
            index[entry["code"]] = by_number
    os.makedirs(os.path.dirname(CARD_INDEX), exist_ok=True)
    json.dump(index, open(CARD_INDEX, "w"), separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    total_cards = sum(len(v) for v in index.values())
    size = os.path.getsize(CARD_INDEX) / 1024
    print(f"  index de {total_cards} cartes écrit ({size:.0f} Ko)")

    print(f"  {len(entries)} extensions écrites dans {os.path.relpath(OUTPUT, ROOT)}")
    for key, label, span in series_rows:
        count = len([e for e in entries if e["series"] == key])
        print(f"    {label:26} {span:26} {count} extensions")


if __name__ == "__main__":
    main()
