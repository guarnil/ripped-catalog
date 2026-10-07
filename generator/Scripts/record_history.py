#!/usr/bin/env python3
"""
Enregistre la cote du jour de chaque hit, un point par carte et par jour :

    python3 Scripts/record_history.py

**Pourquoi ce script existe.** Aucune source publique ne donne l'historique
d'une carte. Cardmarket publie la cote du jour, TCGdex la relaie, et c'est
tout : `avg1`, `avg7` et `avg30` sont trois moyennes glissantes, pas une
série. Les applications qui affichent des courbes ne les ont pas achetées —
elles les ont accumulées, un relevé par jour, depuis des années. L'archive
publique de TCGCSV permettait de rattraper ce retard ; elle est fermée depuis
(HTTP 403, « temporarily removed due to rising server costs »).

L'historique est donc la seule donnée de ce projet qu'on ne peut pas
reconstruire après coup. D'où ce script, lancé bien avant l'écran qui s'en
servira : **chaque jour non relevé est un trou définitif dans la courbe.**

**Ce qu'il relève.** Uniquement les paliers que l'app appelle des hits
(`CardIndex*.json`, tout ce qui n'est pas `base`), et seulement au-dessus d'un
plancher. La « variation » d'une commune à 0,05 € n'est que du bruit
d'arrondi, et garder les 17 798 cartes du catalogue ferait grossir le dépôt de
trois fois plus pour rien.

**D'où viennent les prix.** Du guide Cardmarket, le même fichier que l'app lit
déjà — pas de TCGCSV. Une courbe doit finir exactement sur la cote affichée
sur la fiche de la carte ; deux sources différentes donneraient un dernier
point qui ne colle pas.

- Riftbound (jeu 22) et One Piece (jeu 18) : leurs cartes portent déjà leur
  identifiant produit Cardmarket (`cm`) dans `RiftboundCards.json` et
  `OnePieceCards.json`. Une requête par jeu et par jour suffit.
- Pokémon : rien ne relie un numéro de carte à un produit Cardmarket. Seul
  TCGdex le sait, carte par carte. Ce rattachement ne bouge pratiquement
  jamais : il est construit une fois, gardé dans `Config/CardmarketIds.json`,
  et complété au fil des passages sous un budget de requêtes — la couverture
  Pokémon monte donc sur les premiers jours plutôt qu'en une rafale.

**Ce qu'il écrit.** Un fichier par **jour** dans `Config/History/`, rangé par
extension puis par numéro, le prix en centimes :

    Config/History/2026-09-26.json
    { "SV07": { "170": 3137 }, "OGN": { "119": 579 } }

Un fichier par jour et non un fichier par extension, parce que ces relevés
vivent dans git : un fichier par extension serait réécrit en entier chaque
jour, et git garde une copie complète par version — le dépôt grossirait au
carré du nombre de jours. Écrit une fois puis jamais retouché, chaque relevé
ne coûte que sa propre taille.

L'autre raison vaut le détour : **un jour manquant est un fichier manquant**,
donc visible d'un coup d'œil. Pour une donnée dont tout l'intérêt est la
continuité, une lacune doit sauter aux yeux, pas se cacher au milieu d'un
tableau. Le script le signale lui-même à chaque passage.

Ces fichiers sont **versionnés mais pas publiés** : l'app n'a pas encore
d'écran pour eux, et les télécharger tous les jours pour rien coûterait de la
bande passante à tout le monde. `publish_catalog.py` n'a donc pas à les
connaître. Le jour où l'écran existera, la publication transposera ces
journées en séries par carte — une transposition est un détail, un historique
perdu ne se rattrape pas.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESOURCES = os.path.join(ROOT, "Ripped", "Resources")
HISTORY = os.path.join(ROOT, "Config", "History")
IDS = os.path.join(ROOT, "Config", "CardmarketIds.json")

AGENT = {"User-Agent": "Ripped/1.0 (record_history.py)"}
GUIDE = ("https://downloads.s3.cardmarket.com/productCatalog/priceGuide/"
         "price_guide_{game}.json")
TCGDEX = "https://api.tcgdex.net/v2/fr/cards/{id}"

# Les trois jeux, avec leur numéro Cardmarket et leur index de raretés.
GAMES = {
    "pokemon":  {"game": 6,  "index": "CardIndex.json",          "cards": None},
    "riftbound": {"game": 22, "index": "CardIndexRiftbound.json", "cards": "RiftboundCards.json"},
    "onePiece": {"game": 18, "index": "CardIndexOnePiece.json",  "cards": "OnePieceCards.json"},
}

# Sous ce prix, on ne relève pas. Une courbe ne se lit pas à ce niveau-là, et
# le seuil garde le dépôt dans des proportions raisonnables.
FLOOR_CENTS = 100

# Requêtes TCGdex par passage, pour compléter le rattachement Pokémon. Le
# rattachement se fait une fois pour toutes : mieux vaut étaler sur quelques
# jours que marteler l'API pendant une demi-heure au premier lancement.
BUDGET = 600

# Une carte que TCGdex ne sait pas rattacher. Notée, pour ne pas la redemander
# à chaque passage — `--retry-missing` les repasse en revue.
UNKNOWN = 0


def fetch(url, timeout=120, quiet_404=False):
    """Une réponse JSON, ou None.

    Un 404 n'est pas un incident : c'est une réponse. La carte n'existe pas
    sous cette adresse, et réessayer deux fois avec des pauses ne la fera pas
    apparaître — c'était six secondes perdues par carte manquante sur un
    rattachement qui en parcourt des milliers.
    """
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=AGENT),
                                        timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                if not quiet_404:
                    print(f"  ! {url} : introuvable", file=sys.stderr)
                return None
            if attempt == 2:
                print(f"  ! {url} : {error}", file=sys.stderr)
                return None
            time.sleep(2 * (attempt + 1))
        except Exception as error:                      # noqa: BLE001
            if attempt == 2:
                print(f"  ! {url} : {error}", file=sys.stderr)
                return None
            time.sleep(2 * (attempt + 1))
    return None


def read_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def guide_of(game):
    """Le guide Cardmarket d'un jeu, rangé par identifiant produit."""
    payload = fetch(GUIDE.format(game=game))
    if not payload:
        return {}
    return {line["idProduct"]: line for line in payload.get("priceGuides") or []}


def price_cents(entry, foil):
    """La même cote que celle affichée par l'app, en centimes.

    L'ordre de repli reproduit `CardmarketPriceGuide.Entry.price(foil:)` :
    la tendance d'abord, puis la moyenne, puis le prix le plus bas. Les
    variantes Riftbound n'existent qu'en foil, leur cote foil prime ; chez
    One Piece chaque variante est un produit à part, la cote normale est la
    bonne. Une courbe qui ne finirait pas sur le chiffre de la fiche serait
    une courbe fausse.
    """
    normal = entry.get("trend") or entry.get("avg") or entry.get("low")
    foiled = entry.get("trend-foil") or entry.get("avg-foil")
    price = (foiled or normal) if foil else (normal or foiled)
    return round(price * 100) if price else None


def hits(index):
    """Les cartes à suivre : tout ce qui n'est pas `base`, par extension."""
    return {code: [n for n, rarity in cards.items() if rarity != "base"]
            for code, cards in index.items()}


def save_ids(table):
    with open(IDS, "w", encoding="utf-8") as f:
        json.dump(table, f, separators=(",", ":"), sort_keys=True)


def pokemon_ids(wanted, budget, retry_missing):
    """Le produit Cardmarket de chaque carte Pokémon suivie.

    Le rattachement est gardé sur disque parce qu'il ne bouge pas : la carte
    170 de SV07 sera toujours le produit 786024. Seules les cartes encore
    inconnues coûtent une requête, et jamais plus de `budget` par passage.
    """
    table = read_json(IDS, {})
    spent = 0

    for code, numbers in sorted(wanted.items()):
        known = table.setdefault(code, {})
        for number in numbers:
            if spent >= budget > 0:
                break
            seen = known.get(number)
            if seen and seen != UNKNOWN:
                continue
            if seen == UNKNOWN and not retry_missing:
                continue

            bare = number.strip()
            if "/" in bare:
                # Une réimpression d'époque : TCGdex la range dans un set à
                # part qu'un numéro imprimé ne désigne pas.
                known[number] = UNKNOWN
                continue

            # Les deux formes d'identifiant, dans le même ordre que
            # `PriceService.cardIds` : TCGdex numérote sur trois chiffres
            # (« me04-090 »), mais quelques sets anciens ne rembourrent pas
            # (« cel25-7 »). Essayer une seule forme perdait toutes leurs
            # cartes.
            padded = bare.zfill(3) if bare.isdigit() and len(bare) < 3 else bare
            candidates = [padded] + ([bare] if bare != padded else [])

            product = None
            for candidate in candidates:
                card = fetch(TCGDEX.format(id=f"{code.lower()}-{candidate}"),
                             timeout=30, quiet_404=True)
                spent += 1
                time.sleep(0.12)             # on reste poli avec une API gratuite
                market = ((card or {}).get("pricing") or {}).get("cardmarket") or {}
                product = market.get("idProduct")
                if product:
                    break
            known[number] = product or UNKNOWN

            # Sauvegarde en cours de route : la première construction parcourt
            # des milliers de cartes, et une coupure au bout d'une demi-heure
            # ne doit pas tout redemander demain.
            if spent % 250 == 0:
                save_ids(table)

    if spent:
        save_ids(table)
        print(f"  Pokémon : {spent} requête(s) de rattachement cette fois")

    remaining = sum(1 for code, numbers in wanted.items() for n in numbers
                    if not table.get(code, {}).get(n))
    if remaining:
        print(f"  Pokémon : {remaining} carte(s) encore sans rattachement "
              f"— relancer demain pour les couvrir")
    return table


def day_path(day):
    return os.path.join(HISTORY, f"{day}.json")


def write_day(day, readings):
    """Écrit le relevé d'une journée.

    Le passé n'est jamais retouché : une cote relevée est un fait daté, et la
    corriger après coup reviendrait à réécrire l'histoire que ce script existe
    justement pour garder. Une journée déjà complète n'est donc pas réouverte ;
    une journée partielle — un jeu dont le guide n'avait pas répondu — se
    complète.
    """
    os.makedirs(HISTORY, exist_ok=True)
    existing = read_json(day_path(day), {})
    for code, points in readings.items():
        kept = existing.setdefault(code, {})
        for number, cents in points.items():
            # On complète, on ne corrige pas : le premier relevé de la journée
            # fait foi. Deux passages quotidiens lisent de toute façon le même
            # guide — Cardmarket ne le régénère qu'une fois par nuit — mais la
            # règle doit tenir sans dépendre de ce calendrier.
            kept.setdefault(number, cents)
    with open(day_path(day), "w", encoding="utf-8") as f:
        json.dump(existing, f, separators=(",", ":"), sort_keys=True)
    return sum(len(p) for p in existing.values())


def report_gaps(day):
    """Dit combien de journées manquent depuis le premier relevé.

    C'est la seule alerte qui compte ici : un trou ne se rattrape pas, et il
    passerait inaperçu sans qu'on le nomme.
    """
    if not os.path.isdir(HISTORY):
        return
    days = sorted(f[:-5] for f in os.listdir(HISTORY) if f.endswith(".json"))
    if len(days) < 2:
        return
    first = date.fromisoformat(days[0])
    span = (date.fromisoformat(day) - first).days + 1
    missing = span - len(days)
    if missing > 0:
        print(f"  ! {missing} journée(s) manquante(s) depuis le {days[0]} "
              f"— un trou ne se rattrape pas")
    else:
        print(f"  {len(days)} journées d'affilée depuis le {days[0]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget", type=int, default=BUDGET,
                        help="requêtes TCGdex de rattachement par passage (0 = sans limite)")
    parser.add_argument("--retry-missing", action="store_true",
                        help="redemande les cartes Pokémon restées sans produit Cardmarket")
    parser.add_argument("--date", default=date.today().isoformat(),
                        help="la date du relevé (par défaut aujourd'hui)")
    args = parser.parse_args()

    today = args.date
    print(f"Relevé du {today}")
    total = 0
    readings = {}

    for license, config in GAMES.items():
        index = read_json(os.path.join(RESOURCES, config["index"]), {})
        wanted = hits(index)
        if not wanted:
            continue

        guide = guide_of(config["game"])
        if not guide:
            print(f"  ! {license} : guide Cardmarket indisponible, rien de relevé")
            continue

        if license == "pokemon":
            ids = pokemon_ids(wanted, args.budget, args.retry_missing)
            product_of = lambda code, n: ids.get(code, {}).get(n)          # noqa: E731
            foil_of = lambda code, n: False                                # noqa: E731
        else:
            cards = read_json(os.path.join(RESOURCES, config["cards"]), {})
            product_of = lambda code, n: (cards.get(code, {}).get(n) or {}).get("cm")  # noqa: E731
            # Les variantes Riftbound n'existent qu'en foil ; chez One Piece,
            # chaque variante est un produit Cardmarket distinct.
            foil_of = (lambda code, n: index.get(code, {}).get(n) != "epic"  # noqa: E731
                       ) if license == "riftbound" else (lambda code, n: False)

        kept = skipped = 0
        for code, numbers in sorted(wanted.items()):
            points = {}
            for number in numbers:
                product = product_of(code, number)
                entry = guide.get(product) if product else None
                if not entry:
                    continue
                cents = price_cents(entry, foil_of(code, number))
                if not cents:
                    continue
                if cents < FLOOR_CENTS:
                    skipped += 1
                    continue
                points[number] = cents
            if points:
                readings[code] = points
                kept += len(points)

        total += kept
        print(f"  {license:10} {kept:5} cotes relevées "
              f"({skipped} sous le plancher de {FLOOR_CENTS / 100:.2f} €)")

    if not total:
        print("  rien à relever : aucun guide n'a répondu, la journée reste ouverte")
        return

    on_file = write_day(today, readings)
    size = sum(os.path.getsize(os.path.join(HISTORY, f)) for f in os.listdir(HISTORY))
    print(f"  {on_file} cotes au {today} · historique {size / 1024:.0f} Ko")
    report_gaps(today)


if __name__ == "__main__":
    main()
