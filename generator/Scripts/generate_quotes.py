#!/usr/bin/env python3
"""
Régénère Config/Quotes.json — les cotes des cartes Pokémon que TCGdex ne cote
pas :

    python3 Scripts/generate_quotes.py

**Pourquoi ce fichier existe.** Les cotes Pokémon de l'app viennent de TCGdex,
qui relaie Cardmarket. TCGdex a deux trous, et ce sont les deux moments où on
regarde le plus une cote :

- une **extension qui vient de sortir** n'a pas encore de prix — le 30ᵉ
  Anniversaire, sorti le 16 septembre 2026, n'en avait toujours aucun huit
  jours plus tard, pas même sur ses cartes ordinaires ;
- une **Collection Classique** n'en a jamais : ses réimpressions forment chez
  TCGdex un set à part (« 30th-c ») sans donnée de prix ni visuel, et le
  numéro imprimé sur la carte (« 4/102 ») ne permet pas de l'y retrouver.

**Pourquoi pas Cardmarket directement.** Le guide des prix Cardmarket est
public, et l'app le lit déjà pour Riftbound et One Piece. Mais il est indexé
par identifiant produit, et Cardmarket ne publie **ni le numéro de collection,
ni de quoi distinguer les variantes** : dans une seule extension, quatre
produits portent le nom `Terapagos ex [Unified Beatdown | Crown Opal]` — la
normale, l'ex, l'illustration spéciale et la gold, qui ne valent pas le même
prix. Sur le 30ᵉ, 91 des 158 cartes sont ambiguës par leur nom. Une table
bâtie là-dessus poserait la cote de la normale sur la gold : mieux vaut pas de
cote qu'une fausse.

**Ce qu'on utilise donc.** TCGCSV — le même miroir libre du catalogue
TCGplayer que `generate_products.py`, sans clé — qui donne le numéro imprimé
et le prix dans la même requête, sans aucun rapprochement par nom. C'est un
prix du marché américain : il est converti en euros au taux du jour (BCE, via
frankfurter.dev).

**L'ordre que suit l'app**, lui, ne change pas : la cote Cardmarket de TCGdex
d'abord, ce fichier seulement quand elle manque.

**Reprendre la main sur une carte.** `Config/QuotesOverride.json` associe un
numéro imprimé à un **identifiant de produit Cardmarket** — pas à un prix :

    { "30C": { "4/102": 907902 } }

Le prix du jour est alors lu dans le guide Cardmarket, celui-là même que l'app
utilise pour Riftbound et One Piece. Écrit une fois, il se rafraîchit tout
seul à chaque passage ; un prix recopié à la main, lui, serait figé pour
toujours.

L'identifiant se lit dans l'adresse de la carte sur Cardmarket : le nombre qui
suit `/Products/Singles/…`. C'est le seul moyen fiable de le connaître —
Cardmarket ne publie ni le numéro de collection de ses produits, ni le nom de
ses extensions, et plusieurs tirages d'une même carte y portent des noms
identiques. Se rapprocher par le nom reviendrait à poser la cote de la carte
normale sur sa version gold.
"""

import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from generate_products import (DAY, app_code, card_key, get,  # noqa: E402
                                provisional, printed_total)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT = os.path.join(ROOT, "Config", "Quotes.json")
OVERRIDE = os.path.join(ROOT, "Config", "QuotesOverride.json")

POKEMON = 3
# Le jeu Pokémon chez Cardmarket, pour le guide des prix — le même fichier
# public que l'app lit déjà pour Riftbound (22) et One Piece (18).
CARDMARKET_GAME = 6
CARDMARKET_GUIDE = ("https://downloads.s3.cardmarket.com/productCatalog/priceGuide/"
                    f"price_guide_{CARDMARKET_GAME}.json")

# Au-delà, TCGdex a eu le temps de coter l'extension : la reprendre ici
# n'apporterait qu'un prix moins frais que le sien. Les Collections Classiques
# échappent à cette limite — TCGdex ne les cotera jamais.
RECENT_DAYS = 180

# L'ordre des tirages : la carte nue d'abord, puis ses versions brillantes.
# Une carte qui n'existe qu'en holo n'a pas de ligne « Normal ».
SUBTYPES = ["Normal", "Holofoil", "Reverse Holofoil", "1st Edition Holofoil"]


def usd_to_eur():
    """Le taux du jour, publié par la BCE. `None` s'il est indisponible : une
    cote en dollars affichée avec un « € » serait un mensonge.

    Rendre `None` plutôt que s'arrêter : ce script tourne dans la même chaîne
    que le reste du catalogue, et un service de change en panne n'a pas à
    empêcher la publication des extensions et des cartes. Les cotes de la
    veille restent alors en place.
    """
    url = "https://api.frankfurter.dev/v1/latest?base=USD&symbols=EUR"
    request = urllib.request.Request(url, headers={"User-Agent": "Ripped/1.0 (generate_quotes.py)"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except Exception as error:                          # noqa: BLE001
        print(f"  taux USD→EUR indisponible ({error}) : les cotes ne bougent pas aujourd'hui.")
        return None
    return (payload.get("rates") or {}).get("EUR")


def cardmarket_prices(ids):
    """Le prix Cardmarket des produits demandés, en euros.

    La tendance d'abord — c'est la valeur que lisent les vendeurs, et celle
    que l'app affiche partout ailleurs. Le fichier pèse une quinzaine de
    mégaoctets : il n'est téléchargé que si une carte le demande.
    """
    if not ids:
        return {}
    request = urllib.request.Request(CARDMARKET_GUIDE,
                                     headers={"User-Agent": "Ripped/1.0 (generate_quotes.py)"})
    with urllib.request.urlopen(request, timeout=120) as response:
        guide = json.load(response)
    found = {}
    for line in guide.get("priceGuides") or []:
        if line["idProduct"] not in ids:
            continue
        price = line.get("trend") or line.get("avg") or line.get("trend-foil") or line.get("avg-foil")
        if price:
            found[line["idProduct"]] = price
    return found


def number_of(product):
    """Le numéro imprimé, s'il y en a un. C'est lui qui fait d'une entrée une
    carte, plutôt qu'un produit scellé ou une carte-code.
    """
    for field in product.get("extendedData") or []:
        if field.get("name") == "Number":
            return (field.get("value") or "").strip()
    return None


def price_of(lines):
    """Le prix du marché, dans l'ordre des tirages. `marketPrice` est la valeur
    des ventes récentes ; `midPrice` prend le relais quand rien ne s'est vendu.
    """
    by_subtype = {line.get("subTypeName"): line for line in lines}
    for subtype in SUBTYPES:
        line = by_subtype.get(subtype)
        if not line:
            continue
        price = line.get("marketPrice") or line.get("midPrice")
        if price:
            return price
    # Un tirage inattendu — les « Unlimited » et autres — plutôt que rien.
    for line in lines:
        price = line.get("marketPrice") or line.get("midPrice")
        if price:
            return price
    return None


def groups_to_quote(groups):
    """Les groupes qui valent d'être cotés : les Collections Classiques, que
    TCGdex ne cotera jamais, et les extensions assez récentes pour qu'il n'ait
    pas encore eu le temps.
    """
    horizon = time.strftime("%Y-%m-%d", time.gmtime(time.time() - RECENT_DAYS * 86400))
    keep = []
    for group in groups:
        code = app_code("pokemon", group)
        if not code:
            candidate = provisional("pokemon", group)
            if not candidate:
                continue
            code = candidate["code"]
        published = (group.get("publishedOn") or "")[:10]
        if group.get("isSupplemental") or published >= horizon:
            keep.append((code, group))
    return keep


def main():
    rate = usd_to_eur()
    if not rate:
        return
    print(f"Taux du jour : 1 $ = {rate:.4f} €")

    groups = get(f"{POKEMON}/groups", f"groups_{POKEMON}.json", max_age=DAY)["results"]
    selected = groups_to_quote(groups)

    def read(gid):
        # Les prix bougent tous les jours : le cache ne les garde pas plus.
        return (get(f"{POKEMON}/{gid}/products", f"products_{POKEMON}_{gid}.json",
                    max_age=DAY)["results"],
                get(f"{POKEMON}/{gid}/prices", f"prices_{POKEMON}_{gid}.json",
                    max_age=DAY)["results"])

    # Le dénominateur de chaque extension d'accueil, lu sur ses propres
    # cartes : sans lui, une réimpression d'époque serait rangée sous « 4 » et
    # écraserait la carte 4 de l'extension.
    hosts = {}
    for code, group in selected:
        if group.get("isSupplemental"):
            continue
        products, _ = read(group["groupId"])
        hosts[code] = max(hosts.get(code, 0), printed_total(products))

    quotes = {}
    for code, group in selected:
        products, prices = read(group["groupId"])
        host = hosts.get(code) if group.get("isSupplemental") else None

        lines = {}
        for line in prices:
            lines.setdefault(line["productId"], []).append(line)

        table = quotes.setdefault(code, {})
        found = 0
        for product in products:
            number = number_of(product)
            if not number:
                continue                    # produit scellé, carte-code
            price = price_of(lines.get(product["productId"]) or [])
            if not price:
                continue
            key, _ = card_key(number, host)
            # Le premier trouvé gagne, comme partout ailleurs : deux tirages
            # d'une même carte partagent son numéro.
            if key in table:
                continue
            table[key] = {"n": product["name"], "p": round(price * rate, 2)}
            found += 1

        if not table:
            quotes.pop(code, None)
            continue
        print(f"  {code:8} {group['name'][:46]:46} {found:4} cartes cotées")

    # Une carte rattachée à la main à son produit Cardmarket prend la cote
    # européenne : c'est celle que voit un vendeur d'ici, et celle que l'app
    # affiche partout ailleurs.
    manual = {}
    if os.path.exists(OVERRIDE):
        with open(OVERRIDE, encoding="utf-8") as f:
            manual = json.load(f)
    wanted = {pid for table in manual.values() for pid in table.values()}
    prices = cardmarket_prices(wanted)
    if wanted:
        print(f"  Cardmarket : {len(prices)}/{len(wanted)} produits rattachés à la main")
    for code, table in manual.items():
        for number, pid in table.items():
            price = prices.get(pid)
            if not price:
                # Un identifiant qui ne rend rien est une faute de frappe, ou
                # un produit retiré : on le dit, et on garde la cote TCGCSV.
                print(f"  ! {code} {number} : produit Cardmarket {pid} introuvable")
                continue
            entry = quotes.setdefault(code, {}).get(number)
            quotes.setdefault(code, {})[number] = {"n": (entry or {}).get("n", ""),
                                                   "p": round(price, 2)}

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(quotes, f, separators=(",", ":"), ensure_ascii=False, sort_keys=True)

    total = sum(len(t) for t in quotes.values())
    print(f"  {len(quotes)} extensions · {total} cotes "
          f"({os.path.getsize(OUTPUT) / 1024:.0f} Ko)")


if __name__ == "__main__":
    main()
