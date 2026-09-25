#!/usr/bin/env python3
"""
Régénère le catalogue One Piece de Ripped, en pendant de generate_catalog.py
(Pokémon) et generate_riftbound.py :

    python3 Scripts/generate_onepiece.py

Trois sorties, au même format que Riftbound :

- Ripped/Models/Catalog+OnePiece.swift     les extensions, pour le sélecteur ;
- Ripped/Resources/CardIndexOnePiece.json   code → palier, pour le scan hors ligne ;
- Ripped/Resources/OnePieceCards.json       code → nom, visuel, produit Cardmarket.

Sources, sans clé :

- OPTCG API (optcgapi.com), catalogue ouvert, en anglais. Son auteur demande
  de ne pas le solliciter à l'excès : le script met tout en cache ;
- les visuels officiels du site de Bandai (en.onepiece-cardgame.com), marqués
  « SAMPLE » comme toutes les images de la liste officielle ;
- le catalogue produits de Cardmarket (jeu 18 = One Piece).

Deux différences avec Riftbound, qui décident de la forme de l'index :

- **une carte s'identifie par son code complet** (« OP05-119 »), pas par un
  numéro : un booster contient des cartes d'autres sets — les SP d'OP-11 sont
  imprimées OP05-119 ;
- **une variante porte exactement le même code que sa carte de base** : rien
  d'imprimé ne distingue un parallèle. Les clés de variante reprennent donc
  le suffixe de l'image officielle (« OP05-119_P7 ») et l'app fait choisir la
  variante d'un tap.

Rapprochement avec Cardmarket : ses produits portent le code dans leur nom
(« Monkey.D.Luffy (OP05-119) »), mais toutes les variantes d'un code ont ce
même nom, et l'ordre de leurs identifiants n'est pas fiable (sur OP02-013, le
parallèle a été créé avant la base). Les variantes sont donc reliées **par rang
de prix** : trié par cote TCGplayer (fournie par OPTCG) d'un côté, par cote
Cardmarket de l'autre. Les écarts entre variantes se comptent en ordres de
grandeur ; quand deux variantes sont proches, une inversion ne coûte presque
rien — c'est justement qu'elles valent à peu près la même chose.
"""

import collections
import json
import math
import os
import re
import sys
import time
import urllib.request

OPTCG = "https://optcgapi.com/api"
CARDMARKET = "https://downloads.s3.cardmarket.com/productCatalog/productList/products_singles_18.json"
# TCGCSV : miroir libre du catalogue TCGplayer. On n'en lit ici que les dates
# de sortie, qu'aucune des deux autres sources ne donne (voir
# Scripts/generate_products.py, qui en tire les produits scellés).
TCGCSV_GROUPS = "https://tcgcsv.com/tcgplayer/68/groups"
IMAGES = "https://en.onepiece-cardgame.com/images/cardlist/card"
HEADERS = {"User-Agent": "Ripped/1.0 (generate_onepiece.py)"}

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "onepiece")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT = os.path.join(ROOT, "Ripped", "Models", "Catalog+OnePiece.swift")
CARD_INDEX = os.path.join(ROOT, "Ripped", "Resources", "CardIndexOnePiece.json")
CARD_DETAILS = os.path.join(ROOT, "Ripped", "Resources", "OnePieceCards.json")

# Les séries du sélecteur, dans l'ordre d'affichage.
SERIES = [
    ("OP", "Boosters", "OP"),
    ("EB", "Extra Boosters", "EB"),
    ("PRB", "Premium Boosters", "PRB"),
]

TIER_ORDER = ["superRare", "parallel", "secretRare", "spCard", "mangaRare", "treasureRare"]

# Visuel de chaque extension : le sachet détouré de la liste des produits
# officielle (en.onepiece-cardgame.com/products/?subcategory=boosters).
SITE = "https://en.onepiece-cardgame.com"
PRODUCTS = f"{SITE}/products/?subcategory=boosters&page={{page}}"

CODE = re.compile(r"((?:OP|EB|ST|PRB|P)\d*-\d{3})(?:[_-]([a-zA-Z]\d+))?")

# Clé posée sur la carte par `assign_keys`, le temps de la génération : elle ne
# part pas dans le JSON, seul son contenu devient la clé de l'entrée.
ASSIGNED = "ripped_key"

# Tirage → suffixe de clé, pour les variantes que l'OPTCG API ne distingue ni
# par le code ni par l'identifiant d'image (voir `assign_keys`). Deux lettres,
# comme les suffixes que l'API donne elle-même (« _P1 ») : c'est une clé, pas
# un libellé — le nom, lui, garde le tirage en clair.
VARIANT_SUFFIX = {
    "Parallel": "PL",
    "Alternate Art": "AA",
    "Super Alternate Art": "SA",
    "Full Art": "FA",
    "Textured Foil": "TF",
    "Jolly Roger Foil": "JR",
    "Pirate Foil": "PF",
    "Pandaman Art": "PM",
    "Dash Pack": "DP",
    "Reprint": "RP",
    "Manga": "MG",
    "Gold": "GD",
    "Wanted Poster": "WP",
    "TR": "TR",
    "SP": "SP",
}

# Signalés une fois chacun : un tirage sans suffixe est départagé par un rang,
# la carte n'est donc pas perdue — mais sa clé se lit moins bien.
UNKNOWN_VARIANTS = set()


def get(url, cache_name):
    os.makedirs(CACHE, exist_ok=True)
    cached = os.path.join(CACHE, cache_name)
    if os.path.exists(cached):
        return json.load(open(cached))
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=120) as response:
                data = json.load(response)
            break
        except Exception as error:                      # noqa: BLE001
            if attempt == 2:
                sys.exit(f"  ! échec {url} : {error}")
            time.sleep(2 * (attempt + 1))
    json.dump(data, open(cached, "w"))
    return data


def release_dates():
    """code de l'app → date de sortie officielle, d'après TCGplayer."""
    try:
        groups = get(TCGCSV_GROUPS, "tcgcsv_groups.json")["results"]
    except SystemExit:
        return {}
    dates = {}
    for group in groups:
        abbreviation = (group.get("abbreviation") or "").strip()
        # Les cartes d'avant-première et de tournoi ont un suffixe : « OP02 PRE ».
        if " " in abbreviation:
            continue
        match = re.match(r"(OP|EB|PRB)-?(\d+)", abbreviation)
        published = (group.get("publishedOn") or "")[:10]
        if match and published:
            dates.setdefault(match.group(1) + match.group(2), published)
    return dates


def pack_art():
    """code de l'app → visuel du sachet, lu dans la liste des boosters.

    Chaque produit y est un lien vers sa page (« …/boosters/op01.php »,
    « …/op16.html », « …/boosters/op14-eb04.php »), suivi de son image. Les
    images sont des WebP détourés de 670 px. Le paramètre de cache (« ?_=… »)
    est retiré : l'adresse nue répond de même, et reste stable d'une
    régénération à l'autre.

    Le nombre de pages est lu dans le pager du site (« 1 / 2 »), et pas déduit
    de la première page qui ne rend rien : au-delà de la dernière, le site rend
    une page **sans erreur et sans produit** — exactement ce que rend une page
    refusée. S'arrêter là-dessus, c'est perdre en silence les visuels des
    extensions les plus anciennes, qui sont les dernières de la liste. C'est
    arrivé le 25 septembre 2026 : la tâche planifiée n'a lu que la page 1 et a
    publié onze extensions sans logo.
    """
    art, page, pages = {}, 1, None
    while page <= (pages or 10):
        cached = os.path.join(CACHE, f"products_{page}.html")
        if os.path.exists(cached):
            html = open(cached).read()
        else:
            request = urllib.request.Request(PRODUCTS.format(page=page),
                                             headers={"User-Agent": "Mozilla/5.0 (Ripped)"})
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    html = response.read().decode("utf-8", "replace")
            except Exception as error:                  # noqa: BLE001
                print(f"  ! liste des produits, page {page} : {error}", file=sys.stderr)
                break
            open(cached, "w").write(html)
        # L'image suit le lien de près ; elle est chargée en différé : la
        # vraie adresse est dans `data-src`, `src` n'est qu'une vignette
        # d'attente commune à tous les produits.
        links = re.findall(r'<a [^>]*href="https://en\.onepiece-cardgame\.com/products/([^"]+)"[^>]*>(.{0,600})',
                           html, re.S)
        found = False
        for slug, block in links:
            code = re.search(r"(op|eb|prb)-?(\d+)", slug)
            image = re.search(r'data-src="([^"?]+\.(?:webp|png|jpg))', block)
            if not code or not image or "noimage" in image.group(1):
                continue
            found = True
            url = image.group(1)
            url = url if url.startswith("http") else SITE + url
            art.setdefault(code.group(1).upper() + code.group(2).zfill(2), url)
        if pages is None:
            # « <span class="pageMax">2</span> », dans le pager.
            announced = re.search(r'class="pageMax"[^>]*>\s*(\d+)', html)
            pages = int(announced.group(1)) if announced else None
        if not found:
            if pages:
                # Le site annonce cette page : elle devrait porter des produits.
                print(f"  ! liste des produits, page {page} : aucun visuel alors "
                      f"que le site annonce {pages} pages", file=sys.stderr)
            break
        page += 1
    return art


def app_code(set_id):
    """« OP-01 » → OP01, « PRB-01 » → PRB01, « OP14-EB04 » → OP14."""
    match = re.match(r"([A-Z]+)-?(\d+)", set_id)
    return match.group(1) + match.group(2)


def card_key(card):
    """« OP05-119_p7 » → « OP05-119_P7 », « OP01-077 » → « OP01-077 ».

    Cherché dans l'identifiant d'image plutôt que lu tel quel : quelques
    identifiants OPTCG sont irréguliers (« EB03_OP09-034_p1 », « …_p2.jpg »).

    La clé posée par `assign_keys` prime, quand il y en a une : c'est celle
    d'une variante que l'identifiant d'image ne distingue pas de sa jumelle.
    """
    if card.get(ASSIGNED):
        return card[ASSIGNED]
    match = CODE.search(card["card_image_id"]) or CODE.search(card["card_set_id"])
    code, suffix = match.groups()
    return code + ("_" + suffix.upper() if suffix else "")


def variant_marker(name, warn=False):
    """Le tirage que dit le nom, ou None : « Kingdew (Pandaman Art) » → « PM ».

    Une parenthèse qui porte un numéro (« Kaido (062) ») ou un code
    (« Marshall.D.Teach - ST17-005 ») ne dit pas un tirage. Beaucoup n'en disent
    pas non plus : un nom de personnage (« Mr.2 Bon Clay (Bentham) »), une
    précision de set. D'où `warn`, réservé à `assign_keys` : ailleurs, une
    parenthèse inconnue est le cas ordinaire et n'a rien à signaler.
    """
    for label in reversed(re.findall(r"\(([^)]+)\)", name)):
        if re.fullmatch(r"\d+", label) or CODE.fullmatch(label):
            continue
        if label in VARIANT_SUFFIX:
            return VARIANT_SUFFIX[label]
        if warn and label not in UNKNOWN_VARIANTS:
            UNKNOWN_VARIANTS.add(label)
            print(f"  ! tirage inconnu « {label} » — à ajouter dans VARIANT_SUFFIX")
        return None
    return None


def dedupe(set_cards):
    """Écarte les doublons de l'OPTCG API : deux lignes mot pour mot identiques
    pour une seule carte (le Gecko Moria d'EB-04).

    Avant tout le reste, parce qu'un doublon fausse un compte : deux lignes face
    aux deux produits Cardmarket du code — la carte et son parallèle, que l'API
    ne liste pas — faisaient un groupe apparié, donc une cote tirée au sort
    entre les deux, qui permutait au fil des cours. Une carte face à deux
    produits est un groupe ambigu, et c'est le bon verdict : mieux vaut pas de
    cote qu'une cote prise au parallèle.
    """
    seen, unique = set(), []
    for card in set_cards:
        signature = (card["card_set_id"], card["card_image_id"],
                     card["card_name"], card["rarity"])
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(card)
    return unique


def assign_keys(set_cards):
    """Départage les cartes d'un set qui tombent sur la même clé.

    L'OPTCG API sert le même code **et le même identifiant d'image** pour
    certaines variantes : « Kingdew » et « Kingdew (Pandaman Art) » d'OP-17
    sont deux cartes, deux produits Cardmarket, un seul `OP17-006`. Seule la
    parenthèse du nom les sépare, et c'est donc elle qui donne le suffixe.

    Sans cela, la seconde était perdue — écartée comme un doublon de la
    première, donc absente de l'index : un Pandaman Art scanné s'affichait au
    nom et à la cote de la carte ordinaire, sans variante à proposer. Et
    l'appariement Cardmarket ne pouvait pas tenir d'un passage à l'autre, deux
    clés identiques rendant forcément le même identifiant : ces 35 cartes
    étaient réappariées chaque nuit, et republiées dès qu'un palier de prix
    changeait.

    La carte dont le nom ne dit aucun tirage garde la clé nue — c'est la carte
    ordinaire, celle que l'app connaît déjà et que l'historique a enregistrée.
    Les autres reçoivent le suffixe de leur tirage ; deux tirages de même
    suffixe, ou aucun nom nu, sont départagés par un rang, sur le nom trié,
    pour que la clé ne doive rien à l'ordre de la source.
    """
    groups = collections.defaultdict(list)
    for card in set_cards:
        groups[card_key(card)].append(card)

    assigned = 0
    for key, group in groups.items():
        if len(group) < 2 or len({c["card_name"] for c in group}) < 2:
            continue           # `dedupe` a déjà retiré les lignes identiques
        ordered = sorted(group, key=lambda c: c["card_name"])
        markers = {id(c): variant_marker(c["card_name"], warn=True) for c in ordered}
        # La clé nue reste occupée, toujours : c'est celle qu'un scan rend et
        # celle que l'historique a enregistrée. La carte sans tirage la prend ;
        # à défaut — un groupe dont chaque carte est un tirage, comme les deux
        # Slow-Slow Beam Sword de PRB-02 — la première du tri la garde.
        bare = next((c for c in ordered if markers[id(c)] is None), ordered[0])
        bare[ASSIGNED] = key
        taken, pending = {key}, []
        for card in ordered:
            if card is bare:
                continue
            marker = markers[id(card)]
            candidate = f"{key}_{marker}" if marker else None
            if candidate is None or candidate in taken:
                pending.append(card)
                continue
            taken.add(candidate)
            card[ASSIGNED] = candidate
        for card in pending:
            rank = 2
            while f"{key}_V{rank}" in taken:
                rank += 1
            card[ASSIGNED] = f"{key}_V{rank}"
            taken.add(card[ASSIGNED])
        assigned += len(group) - 1
    return assigned


def tier_of(card):
    """Le palier de l'app, ou None pour une carte de base.

    Le nom dit la variante (« (Manga) », « (SP) », « (Alternate Art) ») ; la
    rareté imprimée ne distingue que SR et SEC. Une réimpression (suffixe
    « r ») garde le palier de sa rareté.
    """
    name, rarity = card["card_name"], card["rarity"]
    suffix = (CODE.search(card["card_image_id"]) or CODE.search(card["card_set_id"])).group(2) or ""
    if any(tag in name for tag in ("(Manga)", "(Gold)", "(Wanted Poster)")):
        return "mangaRare"
    if rarity == "TR":
        return "treasureRare"
    if "(SP)" in name:
        return "spCard"
    if suffix.lower().startswith("p") and "(Reprint)" not in name:
        return "parallel"
    if "(Alternate Art)" in name or "(Parallel)" in name:
        return "parallel"
    if rarity == "SEC":
        return "secretRare"
    if rarity == "SR":
        return "superRare"
    return None


def display_name(name, departed=False):
    """« Roronoa Zoro (001) (Parallel) » → « Roronoa Zoro » : le palier dit la variante.

    Sauf pour une variante départagée par `assign_keys` : son tirage n'est dit
    ni par le palier — elle a celui de sa jumelle — ni par son visuel, que
    l'OPTCG API sert identique. Le nom est alors la seule chose qui la
    distingue de la carte ordinaire, et il le garde.
    """
    labels = [label for label in re.findall(r"\(([^)]+)\)", name)
              if not re.fullmatch(r"\d+", label) and not CODE.fullmatch(label)]
    plain = re.sub(r"\s*\([^)]*\)", "", name).strip()
    return f"{plain} ({labels[-1]})" if departed and labels else plain


def price_rank(price):
    """Le prix ramené à un palier, en tiers de décade (facteur 2,15).

    Trier sur le prix brut faisait dépendre le rang de quelques centimes :
    deux parallèles à 0,04 € et 0,05 € s'inversaient d'un jour à l'autre, et
    leurs identifiants Cardmarket permutaient. Or ce qui distingue vraiment
    deux variantes se compte en ordres de grandeur : le palier garde ces
    écarts-là et ignore le bruit. À palier égal, une clé fixe départage, pour
    que l'ordre ne doive plus rien au cours du jour.
    """
    return round(math.log10(max(price, 0.01)) * 3)


def previous_pairing():
    """L'appariement de la dernière génération : il fait foi tant que
    Cardmarket n'a pas bougé.

    Deux endroits possibles, parce que le script tourne dans deux contextes.
    Dans le dépôt de l'app, il écrit et relit `Ripped/Resources/`. Dans la
    tâche planifiée du dépôt publié, ce dossier est recréé vide à chaque
    passage : la seule trace du passage précédent est le fichier déjà publié,
    posé à côté de `generator/`. Sans ce second chemin, la tâche repartirait
    chaque nuit sans ancrage — et l'appariement se remettrait à permuter, ce
    qui est exactement ce qu'on cherchait à arrêter.
    """
    beside_generator = os.path.join(ROOT, os.pardir, os.path.basename(CARD_DETAILS))
    for path in (CARD_DETAILS, beside_generator):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
    return {}


def match_products(cards, products, price_of, previous=None):
    """clé de carte → idProduct, par rang de prix au sein d'un même code.

    L'appariement déjà publié fait foi tant qu'il tient : mêmes produits, un
    par variante, aucun doublon. Le recalculer à chaque passage sur les cours
    du jour faisait permuter une poignée d'identifiants à chaque publication —
    sans rien changer aux cotes, puisque ces variantes-là valent à peu près la
    même chose, mais en réécrivant le fichier, donc en le faisant retélécharger
    à toutes les apps pour rien.

    On ne rejoue l'appariement que si Cardmarket a bougé : un tirage de plus,
    un produit retiré, un identifiant inconnu.
    """
    previous = previous or {}
    groups = collections.defaultdict(list)
    for product in products:
        found = CODE.search(product["name"])
        if found:
            groups[found.group(1)].append(product)

    by_code = collections.defaultdict(list)
    for card in cards:
        by_code[card_key(card).split("_")[0]].append(card)

    matched, ambiguous, kept = {}, 0, 0
    for code, group in by_code.items():
        candidates = groups.get(code, [])
        if not candidates:
            continue
        if len(candidates) != len(group):
            ambiguous += 1
            continue

        keys = [card_key(card) for card in group]
        already = {key: previous.get(key, {}).get("cm") for key in keys}
        available = {product["idProduct"] for product in candidates}
        if (all(already[key] in available for key in keys)
                and len(set(already.values())) == len(keys)):
            matched.update(already)
            kept += len(keys)
            continue

        group = sorted(group, key=lambda c: (price_rank(c.get("market_price") or 0),
                                             card_key(c)))
        candidates = sorted(candidates, key=lambda p: (price_rank(price_of(p["idProduct"])),
                                                       p["idProduct"]))
        for card, product in zip(group, candidates):
            matched[card_key(card)] = product["idProduct"]
    return matched, ambiguous, kept


def swift_string(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main():
    print("Catalogue One Piece → Swift")
    sets = get(f"{OPTCG}/allSets/", "sets.json")
    cards = get(f"{OPTCG}/allSetCards/", "cards.json")
    products = get(CARDMARKET, "cardmarket_products.json")["products"]
    guide = get(CARDMARKET.replace("productList/products_singles", "priceGuide/price_guide"),
                "cardmarket_prices.json")["priceGuides"]
    prices = {line["idProduct"]: line for line in guide}

    def price_of(product):
        line = prices.get(product, {})
        return line.get("trend") or line.get("avg") or line.get("low") or 0

    print(f"  {len(cards)} cartes OPTCG, {len(products)} produits Cardmarket")

    published = previous_pairing()

    art = pack_art()
    print(f"  {len(art)} visuels de sachet sur le site officiel")
    dates = release_dates()
    print(f"  {len(dates)} dates de sortie chez TCGplayer")

    codes_by_expansion = collections.defaultdict(set)
    for product in products:
        found = CODE.search(product["name"])
        if found:
            codes_by_expansion[product["idExpansion"]].add(found.group(1))

    entries, index, details = [], {}, {}
    reused = rematched = departed = 0
    for set_info in sets:
        code = app_code(set_info["set_id"])
        set_cards = dedupe([c for c in cards if c["set_id"] == set_info["set_id"]])
        # Puis : deux cartes que l'API ne distingue pas doivent
        # avoir deux clés, sans quoi la seconde est perdue et l'appariement
        # Cardmarket ne peut pas tenir d'un passage à l'autre.
        departed += assign_keys(set_cards)
        if not set_cards:
            continue

        # L'extension Cardmarket : celle qui partage le plus de codes avec le set.
        codes = {c["card_set_id"] for c in set_cards}
        expansion = max(codes_by_expansion, key=lambda e: len(codes & codes_by_expansion[e]))
        expansion_products = [p for p in products if p["idExpansion"] == expansion]
        matched, ambiguous, kept = match_products(set_cards, expansion_products,
                                                  price_of, published.get(code))
        reused += kept
        rematched += len(matched) - kept

        # Date de sortie : celle que publie TCGplayer, qui est la bonne.
        # À défaut, la première carte mise en vente sur Cardmarket —
        # précommandes comprises, elle précède parfois la sortie de trois mois
        # (OP-05 : septembre pour décembre 2023).
        release = dates.get(code) or min(p["dateAdded"] for p in expansion_products)[:10]

        tiers, by_key, card_details = set(), {}, {}
        for card in set_cards:
            key = card_key(card)
            if key in by_key:
                continue                            # doublons OPTCG
            tier = tier_of(card)
            if tier:
                tiers.add(tier)
            by_key[key] = tier or "base"
            image_id = card["card_image_id"].replace(".jpg", "")
            entry = {"n": display_name(card["card_name"], bool(card.get(ASSIGNED))),
                     "r": card["rarity"],
                     "i": f"{IMAGES}/{image_id}.png"}
            if key in matched:
                entry["cm"] = matched[key]
            card_details[key] = entry
        index[code] = by_key
        details[code] = card_details

        ordered = [t for t in TIER_ORDER if t in tiers]
        series = next(s for s, _, prefix in SERIES if re.match(prefix + r"\d", code))
        entries.append({
            "code": code,
            "name": set_info["set_name"],
            "series": series,
            "release": release,
            "cards": len({k.split("_")[0] for k in by_key}),
            "main": ordered[:3],
            "more": ordered[3:],
        })
        hits = [c for c in set_cards if tier_of(c)]
        priced = sum(1 for c in hits if card_key(c) in matched)
        print(f"    {code:6} {set_info['set_name'][:36]:36} {release}  {len(set_cards):4} cartes · "
              f"Cardmarket {expansion} · hits reliés {priced}/{len(hits)}, {ambiguous} codes ambigus")

    entries.sort(key=lambda e: e["release"], reverse=True)
    if departed:
        print(f"  {departed} variantes départagées par leur tirage : "
              f"l'API leur donne le code et le visuel de leur jumelle")
    print(f"  appariement Cardmarket : {reused} cartes reprises telles quelles, "
          f"{rematched} refaites")

    lines = [
        "// Généré par Scripts/generate_onepiece.py — ne pas modifier à la main.",
        "// Source : OPTCG API (optcgapi.com) et catalogue Cardmarket, sans clé.",
        f"// Régénéré le {time.strftime('%Y-%m-%d')} · {len(entries)} extensions.",
        "",
        "import Foundation",
        "",
        "extension Catalog {",
        "",
        "    static let onePieceSeries: [PackSeries] = [",
    ]
    for key, label, _ in SERIES:
        years = [e["release"][:4] for e in entries if e["series"] == key]
        span = f"{min(years)} → {max(years)}" if years else ""
        lines.append(f"        PackSeries(id: {swift_string(key)}, label: {swift_string(label)}, "
                     f"years: {swift_string(span)}, license: .onePiece),")
    lines += ["    ]", "", "    static let onePieceExtensions: [PackExtension] = ["]
    for e in entries:
        lines.append(
            f"        PackExtension(code: {swift_string(e['code'])}, "
            f"name: {swift_string(e['name'])}, "
            f"series: {swift_string(e['series'])}, "
            f"releaseDate: {swift_string(e['release'])}, "
            f"cardCount: {e['cards']}, "
            f"logoURL: {swift_string(art.get(e['code'], ''))}, "
            f"mainRarities: [{', '.join('.' + t for t in e['main'])}], "
            f"moreRarities: [{', '.join('.' + t for t in e['more'])}], "
            f"license: .onePiece),"
        )
    lines += ["    ]", "}", ""]
    with open(OUTPUT, "w") as handle:
        handle.write("\n".join(lines))

    json.dump(index, open(CARD_INDEX, "w"), separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    json.dump(details, open(CARD_DETAILS, "w"), separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    for path in (CARD_INDEX, CARD_DETAILS):
        print(f"  {os.path.relpath(path, ROOT)} ({os.path.getsize(path) / 1024:.0f} Ko)")
    print(f"  {len(entries)} extensions écrites dans {os.path.relpath(OUTPUT, ROOT)}")
    missing = [e["code"] for e in entries if e["code"] not in art]
    if missing:
        print(f"  ! sans visuel de sachet : {', '.join(missing)}")


if __name__ == "__main__":
    main()
