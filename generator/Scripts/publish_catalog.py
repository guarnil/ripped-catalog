#!/usr/bin/env python3
"""Prépare le catalogue que l'app télécharge sans mise à jour App Store.

À lancer après les scripts de génération (generate_catalog.py,
generate_riftbound.py, generate_onepiece.py, generate_products.py) :

    python3 Scripts/publish_catalog.py            # prépare CatalogSite/
    python3 Scripts/publish_catalog.py --push     # et le publie

Le dossier `CatalogSite/` est un clone du dépôt GitHub `ripped-catalog`, servi
par GitHub Pages à https://guarnil.github.io/ripped-catalog/ — l'adresse lue
par `RemoteCatalog.baseURL` dans l'app.

On y écrit :
- `extensions.json` : les séries et extensions des trois jeux, relues dans les
  fichiers Swift générés — ce sont eux qui font foi, l'app compilée et la copie
  publiée ne peuvent donc pas diverger ;
- les index de rareté, les empreintes de cartes, les fiches et les produits,
  copiés tels quels
  depuis `Ripped/Resources/` — l'index Pokémon reçoit en plus les cartes des
  extensions que TCGdex ne connaît pas encore, lues chez TCGplayer par
  `generate_products.py` ;
- `manifest.json` : l'empreinte SHA-256 de chaque fichier. L'app ne retélécharge
  que ce qui a changé, et rejette un fichier dont l'empreinte ne correspond pas.

Garde-fou : une extension déjà publiée ne doit jamais disparaître. Le script
refuse de publier une liste qui en perdrait une.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS = os.path.join(ROOT, "Ripped", "Models")
RESOURCES = os.path.join(ROOT, "Ripped", "Resources")
SITE = os.path.join(ROOT, "CatalogSite")

# Doit suivre `RemoteCatalog.schema` dans l'app.
SCHEMA = 1

# Dans l'ordre de `Catalog.series` et `Catalog.extensions` côté app.
SWIFT_SOURCES = [
    ("Catalog+Generated.swift", "pokemon"),
    ("Catalog+Riftbound.swift", "riftbound"),
    ("Catalog+OnePiece.swift", "onePiece"),
]

PENDING = "PendingExtensions.json"
PENDING_CARDS = "PendingCards.json"
CLASSIC_CARDS = "ClassicCards.json"

# Le logo des extensions annoncées, à la main.
#
# Aucune source ne le donne avant que les cartes soient cataloguées : TCGCSV
# n'a pas d'image de série, et l'adresse TCGdex a besoin du vrai code, qui
# n'existe pas encore. En attendant, la vignette montre la photo du sachet ;
# une adresse posée ici la remplace par le logo.
#
# Deux façons de faire, au choix :
#
# 1. Déposer l'image dans `CatalogSite/logos/<CODE>.png` — elle est publiée
#    avec le catalogue, et reprise automatiquement, sans toucher à ce fichier.
# 2. Pointer une adresse existante dans la table ci-dessous.
#
# Clé : le code provisoire (l'abréviation TCGplayer). Dans les deux cas,
# vérifie les droits de l'image : l'app la télécharge telle quelle, et
# l'héberger toi-même, c'est la rediffuser.
PENDING_LOGOS = {
    # "30C": "https://exemple.invalid/30e-anniversaire.png",
}

# La racine servie par GitHub Pages, pour les logos déposés dans le dépôt.
SITE_URL = "https://guarnil.github.io/ripped-catalog/"

COPIED = [
    "CardIndex.json",
    "CardIndexRiftbound.json",
    "CardIndexOnePiece.json",
    "RiftboundCards.json",
    "OnePieceCards.json",
    "Products.json",
    # L'empreinte de chaque carte — PV, dégâts, coût de retraite — que le scan
    # recoupe avec le numéro lu. Voir `facts_of` dans generate_products.py.
    "CardFacts.json",
    # Le nom français de chaque carte : le plus gros texte qu'elle porte, et
    # donc le plus sûr à lire quand le numéro se dérobe.
    "CardNames.json",
]

# Publiés sans être embarqués. Une cote vieillit trop vite pour tenir dans un
# binaire qui ne bouge qu'à chaque version de l'app : elle ne fait que passer
# par la publication. Absent, le fichier ne bloque rien — l'app se contente
# alors de TCGdex, comme avant.
PUBLISHED_ONLY = ["Quotes.json"]
CONFIG = os.path.join(ROOT, "Config")


def string_field(line, name):
    match = re.search(name + r': "((?:[^"\\]|\\.)*)"', line)
    return match.group(1).replace('\\"', '"') if match else ""


def rarities_field(line, name):
    match = re.search(name + r": \[([^\]]*)\]", line)
    if not match:
        return []
    return [r.strip().lstrip(".") for r in match.group(1).split(",") if r.strip()]


def license_field(line, default):
    match = re.search(r"license: \.(\w+)", line)
    return match.group(1) if match else default


def read_swift_catalog():
    series, extensions = [], []
    for filename, default_license in SWIFT_SOURCES:
        with open(os.path.join(MODELS, filename), encoding="utf-8") as f:
            text = f.read()

        announced = re.search(r"· (\d+) extensions", text)
        found = 0
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("PackSeries("):
                series.append({
                    "id": string_field(line, "id"),
                    "label": string_field(line, "label"),
                    "years": string_field(line, "years"),
                    "license": license_field(line, default_license),
                })
            elif line.startswith("PackExtension("):
                found += 1
                card_count = re.search(r"cardCount: (\d+)", line)
                extensions.append({
                    "code": string_field(line, "code"),
                    "name": string_field(line, "name"),
                    "series": string_field(line, "series"),
                    "releaseDate": string_field(line, "releaseDate"),
                    "cardCount": int(card_count.group(1)) if card_count else 0,
                    "logoURL": string_field(line, "logoURL"),
                    "main": rarities_field(line, "mainRarities"),
                    "more": rarities_field(line, "moreRarities"),
                    "license": license_field(line, default_license),
                })

        # Le format des fichiers générés a changé : mieux vaut s'arrêter que
        # publier une liste incomplète.
        if announced and int(announced.group(1)) != found:
            sys.exit(f"{filename} annonce {announced.group(1)} extensions, {found} relues.")

    for ext in extensions:
        if not ext["code"] or not ext["name"] or not ext["series"]:
            sys.exit(f"Extension illisible : {ext}")
    return series, extensions


def days_between(a, b):
    from datetime import date
    try:
        x = date(*map(int, a.split("-")))
        y = date(*map(int, b.split("-")))
    except (TypeError, ValueError):
        return 10_000
    return abs((x - y).days)


def logo_for(code):
    """L'image déposée dans `logos/` du dépôt, sinon l'adresse de la table,
    sinon rien — et la vignette montre alors la photo du sachet.

    L'adresse porte l'empreinte du fichier. Sans elle, remplacer une image
    sans changer son nom ne changeait pas son adresse, et les apps qui
    l'avaient déjà en cache gardaient l'ancienne indéfiniment : le logo du
    ME06, détouré dans ce dépôt, restait blanc sur les téléphones. L'empreinte
    fait de chaque version une adresse distincte, donc une entrée de cache
    distincte, et la correction arrive toute seule.
    """
    for extension in ("png", "webp", "jpg"):
        path = os.path.join(SITE, "logos", f"{code}.{extension}")
        if os.path.exists(path):
            with open(path, "rb") as f:
                stamp = hashlib.sha256(f.read()).hexdigest()[:8]
            return f"{SITE_URL}logos/{code}.{extension}?v={stamp}"
    return PENDING_LOGOS.get(code, "")


def pending_cards():
    """Les cartes des extensions vendues avant d'être cataloguées, relevées
    chez TCGplayer par `generate_products.py` : code provisoire → index des
    numéros, nombre de cartes et paliers présents. Vide quand aucune extension
    n'est en avance sur son catalogue.
    """
    path = os.path.join(RESOURCES, PENDING_CARDS)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def classic_cards():
    """Les réimpressions d'époque vendues dans une extension — la
    Collection Classique du 30ᵉ Anniversaire — relevées chez TCGplayer par
    `generate_products.py`.

    Elles ne forment pas une extension à part : elles sortent du même
    booster, et rejoignent donc l'index de leur extension d'accueil. Leur
    clé porte le numéro imprimé en entier (« 4/102 »), dénominateur
    compris, parce que c'est lui — et lui seul — qui les distingue de la
    carte de même numérateur de l'extension.

    Le code est celui de TCGplayer ; `apply_renames` le fait suivre si
    l'extension a pris son vrai code entre-temps.
    """
    path = os.path.join(RESOURCES, CLASSIC_CARDS)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def resolve_pending(extensions, previous_renames):
    """Les extensions vendues avant que leurs cartes soient cataloguées.

    `generate_products.py` les repère chez TCGplayer. Trois cas :
    - son code est déjà celui du catalogue : les cartes sont arrivées, il n'y a
      plus rien à faire ;
    - une extension du catalogue, même licence, sortie à moins de dix jours et
      qui n'est réclamée par personne d'autre : c'est la même, sous son vrai
      code. On publie le renommage, et l'app renomme l'historique une fois ;
    - sinon, elle est publiée telle quelle, marquée provisoire : on peut
      l'ouvrir, et, si TCGplayer a déjà saisi ses cartes, la scanner —
      `PendingCards.json` donne alors son nombre de cartes et ses vrais
      paliers, là où on en était réduit à ceux de l'extension voisine.

    Rend (extensions provisoires, renommages, points à signaler).
    """
    path = os.path.join(RESOURCES, PENDING)
    if not os.path.exists(path):
        return [], previous_renames, []
    with open(path, encoding="utf-8") as f:
        pending = json.load(f)

    known = {e["code"]: e for e in extensions}
    renames = dict(previous_renames)
    cards = pending_cards()
    provisional, notes = [], []

    for code, candidate in sorted(pending.items()):
        if code in known or code in renames:
            continue

        claimed = set(renames.values()) | set(pending)
        matches = [e for e in extensions
                   if e["license"] == candidate["license"]
                   and e["code"] not in claimed
                   and days_between(e["releaseDate"], candidate["releaseDate"]) <= 10]
        if len(matches) == 1:
            renames[code] = matches[0]["code"]
            continue
        if len(matches) > 1:
            notes.append(f"  ! extension provisoire {code} ({candidate['name']}) : "
                         f"plusieurs correspondances possibles — "
                         + ", ".join(m["code"] for m in matches))
        if not logo_for(code):
            notes.append(f"  ! extension provisoire {code} ({candidate['name']}) sans logo — "
                         f"dépose `logos/{code}.png` dans ce dépôt, ou donne son adresse "
                         f"dans PENDING_LOGOS. Sans logo, la vignette garde la photo du sachet.")

        # Les paliers de la dernière extension de la même série : une
        # nouveauté propose presque toujours les mêmes que sa voisine.
        sibling = next((e for e in extensions
                        if e["series"] == candidate["series"] and e["license"] == candidate["license"]), None)
        # Les paliers lus dans les cartes valent mieux que ceux devinés ; une
        # extension dont TCGplayer n'a encore saisi aucune carte garde ceux de
        # sa voisine.
        read = cards.get(code)
        fallback_main = sibling["main"] if sibling else ["ultra"]
        provisional.append({
            "code": code,
            "name": candidate["name"],
            "series": candidate["series"] if sibling else "HS",
            "releaseDate": candidate["releaseDate"],
            "cardCount": read["cardCount"] if read else 0,
            "logoURL": logo_for(code),
            "main": (read["main"] if read and read["main"] else fallback_main),
            "more": (read["more"] if read and read["main"] else
                     (sibling["more"] if sibling else [])),
            "license": candidate["license"],
            "provisional": True,
        })
        if read:
            notes.append(f"  · extension provisoire {code} ({candidate['name']}) : "
                         f"{len(read['index'])} cartes lues chez TCGplayer, scannable.")
    return provisional, renames, notes


def order_by_release(extensions):
    """La plus récente d'abord, licence par licence.

    L'app lit ce tableau dans l'ordre : c'est lui qui range le sélecteur, et
    qui désigne l'extension proposée par défaut. Ajoutée en fin de liste, une
    extension provisoire se retrouverait sous les plus vieilles — invisible là
    où on l'attend.
    """
    licenses = list(dict.fromkeys(e["license"] for e in extensions))
    ordered = []
    for license in licenses:
        block = [e for e in extensions if e["license"] == license]
        # Sans date, on garde la place que le catalogue lui donne, en fin.
        block.sort(key=lambda e: e["releaseDate"] or "", reverse=True)
        ordered += block
    return ordered


def apply_renames(table, renames):
    """Range les produits d'une extension provisoire sous son vrai code."""
    for old, new in renames.items():
        if old in table:
            table.setdefault(new, {}).update(table.pop(old))
    return table


def check_not_shrinking(name, table, renames=None):
    """Refuse une table qui perdrait une extension, ou une bonne part de ses
    cartes, par rapport à la version publiée.

    C'est le cas typique d'une API indisponible pendant la génération : le
    script écrit alors une extension vide, et la publier écraserait des
    données justes chez tous les utilisateurs.
    """
    previous_path = os.path.join(SITE, name)
    if not os.path.exists(previous_path):
        return
    with open(previous_path, encoding="utf-8") as f:
        previous = json.load(f)
    for code, entries in previous.items():
        code = (renames or {}).get(code, code)
        if code not in table:
            sys.exit(f"{name} : l'extension {code} disparaîtrait.")
        before = len(entries) if isinstance(entries, dict) else 0
        after = len(table[code]) if isinstance(table[code], dict) else 0
        if before >= 10 and after < before * 0.9:
            sys.exit(f"{name} : {code} passerait de {before} à {after} entrées.")


def write(name, data):
    path = os.path.join(SITE, name)
    with open(path, "wb") as f:
        f.write(data)
    return hashlib.sha256(data).hexdigest()


def sync_generator():
    """Copie les scripts qui viennent de produire ce catalogue à côté de lui.

    Le dépôt publié porte sa propre copie de `Scripts/`, dont se sert la tâche
    planifiée. Rien ne la tenait à jour : un correctif apporté ici et non
    recopié là-bas, et la tâche régénérait chaque nuit un catalogue d'avant —
    assez pour que le garde-fou « une extension ne doit jamais disparaître »
    arrête tout, en boucle, jusqu'à ce qu'on remarque les échecs.

    Sans effet quand le script tourne déjà depuis cette copie : dans la tâche
    planifiée, source et destination sont le même dossier.
    """
    source = os.path.dirname(os.path.abspath(__file__))
    destination = os.path.join(SITE, "generator", "Scripts")
    if os.path.abspath(source) == os.path.abspath(destination):
        return 0

    os.makedirs(destination, exist_ok=True)
    copied = 0
    for name in sorted(os.listdir(source)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(source, name), "rb") as f:
            data = f.read()
        target = os.path.join(destination, name)
        if os.path.exists(target):
            with open(target, "rb") as f:
                if f.read() == data:
                    continue
        with open(target, "wb") as f:
            f.write(data)
        copied += 1
    return copied


def git(*args):
    subprocess.run(["git", "-C", SITE, *args], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--push", action="store_true", help="committer et pousser CatalogSite/")
    parser.add_argument("--site", help="dossier à remplir, à la place de CatalogSite/ (GitHub Actions)")
    args = parser.parse_args()

    global SITE
    if args.site:
        SITE = os.path.abspath(args.site)

    if not os.path.isdir(SITE):
        sys.exit(f"{SITE} est absent : clone d'abord le dépôt ripped-catalog à cet endroit.")

    series, extensions = read_swift_catalog()

    previous_path = os.path.join(SITE, "extensions.json")
    previous, previous_renames, previous_logos = set(), {}, {}
    if os.path.exists(previous_path):
        with open(previous_path, encoding="utf-8") as f:
            published = json.load(f)
        previous = {e["code"] for e in published.get("extensions", [])}
        previous_renames = published.get("renames", {})
        previous_logos = {e["code"]: e.get("logoURL") or ""
                          for e in published.get("extensions", [])}

    provisional, renames, notes = resolve_pending(extensions, previous_renames)
    for note in notes:
        print(note)
    listed = order_by_release(extensions + provisional)

    # Jamais une extension de moins que la version déjà publiée — sauf celle
    # qui a simplement pris son vrai code, et dont l'app renomme l'historique.
    lost = sorted(previous - {e["code"] for e in listed} - set(renames))
    if lost:
        sys.exit("Ces extensions disparaîtraient du catalogue publié : " + ", ".join(lost))

    # Jamais un logo de moins, non plus. Un visuel de sachet est lu sur le site
    # officiel du jeu, page par page : une page refusée ne se distingue pas
    # d'une page vide, et la régénération rend alors une extension sans logo
    # au lieu de s'arrêter. Le garde-fou est ici plutôt que dans chaque script
    # de génération : la version publiée fait foi, et un logo vide ne veut
    # jamais dire « celui-ci n'a plus de logo », seulement « pas trouvé cette
    # fois-ci ».
    for entry in listed:
        if entry.get("logoURL"):
            continue
        source = next((old for old, new in renames.items() if new == entry["code"]),
                      entry["code"])
        kept = previous_logos.get(entry["code"]) or previous_logos.get(source)
        if kept:
            entry["logoURL"] = kept
            print(f"  logo de {entry['code']} repris de la version publiée — "
                  f"la régénération ne l'a pas trouvé")

    # Une extension qui reçoit une Collection Classique doit proposer le
    # palier à la saisie : le catalogue de cartes, lui, ne connaît pas ces
    # réimpressions — elles forment un set à part chez TCGdex, écarté exprès.
    classic = classic_cards()
    for entry in listed:
        source = next((old for old, new in renames.items() if new == entry["code"]),
                      entry["code"])
        if source in classic and "classic" not in entry["more"]:
            entry["more"] = entry["more"] + ["classic"]

    payload = json.dumps({"series": series, "extensions": listed, "renames": renames},
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    hashes = {"extensions.json": write("extensions.json", payload)}

    # Les cartes que TCGplayer a saisies avant TCGdex : elles ne sont pas dans
    # l'index embarqué — celui-ci ne connaît que le catalogue de cartes — mais
    # la copie téléchargée les porte, et c'est elle que l'app lit d'abord.
    cards = pending_cards()
    extra = {e["code"]: cards[e["code"]]["index"]
             for e in provisional if e["code"] in cards}

    # Les Collections Classiques rejoignent l'index de leur extension
    # d'accueil, provisoire ou non.
    for code, index in classic_cards().items():
        extra.setdefault(code, {}).update(index)

    for name in COPIED + PUBLISHED_ONLY:
        source = os.path.join(CONFIG if name in PUBLISHED_ONLY else RESOURCES, name)
        if name in PUBLISHED_ONLY and not os.path.exists(source):
            print(f"  {name} absent : rien à publier (voir Scripts/generate_quotes.py)")
            continue
        with open(source, "rb") as f:
            data = f.read()
        table = json.loads(data)  # un fichier illisible ne part pas
        if name == "CardIndex.json" and extra:
            for code, index in extra.items():
                table.setdefault(code, {}).update(index)
        # Les produits d'une extension provisoire suivent son renommage.
        if renames:
            table = apply_renames(table, renames)
        if renames or (name == "CardIndex.json" and extra):
            data = json.dumps(table, ensure_ascii=False, separators=(",", ":"),
                              sort_keys=True).encode("utf-8")
        check_not_shrinking(name, table, renames)
        hashes[name] = write(name, data)

    manifest = json.dumps({"schema": SCHEMA, "files": hashes}, indent=2, sort_keys=True).encode("utf-8")
    write("manifest.json", manifest)
    # Sans ce fichier, GitHub Pages passe les fichiers dans Jekyll.
    write(".nojekyll", b"")

    synced = sync_generator()
    if synced:
        print(f"{synced} script(s) de génération recopiés dans le dépôt publié.")

    print(f"{len(series)} séries, {len(listed)} extensions "
          f"(dont {len(provisional)} en attente de cartes, "
          f"{len(extra)} déjà scannables), "
          f"{len(renames)} renommages, {len(hashes)} fichiers prêts dans {os.path.basename(SITE) or SITE}.")

    if args.push:
        git("add", "-A")
        if subprocess.run(["git", "-C", SITE, "diff", "--cached", "--quiet"]).returncode == 0:
            print("Rien de nouveau à publier.")
            return
        git("commit", "-m", f"Catalogue : {len(extensions)} extensions")
        git("push")
        print("Publié. Les apps le récupèrent à leur prochaine ouverture (au plus une vérification toutes les 6 heures), puis l'affichent au lancement suivant.")


if __name__ == "__main__":
    main()
