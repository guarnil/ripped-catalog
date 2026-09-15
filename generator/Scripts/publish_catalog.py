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
- les index de rareté, les fiches de cartes et les produits, copiés tels quels
  depuis `Ripped/Resources/` ;
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

COPIED = [
    "CardIndex.json",
    "CardIndexRiftbound.json",
    "CardIndexOnePiece.json",
    "RiftboundCards.json",
    "OnePieceCards.json",
    "Products.json",
]


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


def check_not_shrinking(name, table):
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

    # Jamais une extension de moins que la version déjà publiée.
    previous_path = os.path.join(SITE, "extensions.json")
    if os.path.exists(previous_path):
        with open(previous_path, encoding="utf-8") as f:
            previous = {e["code"] for e in json.load(f).get("extensions", [])}
        lost = sorted(previous - {e["code"] for e in extensions})
        if lost:
            sys.exit("Ces extensions disparaîtraient du catalogue publié : " + ", ".join(lost))

    payload = json.dumps({"series": series, "extensions": extensions},
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    hashes = {"extensions.json": write("extensions.json", payload)}

    for name in COPIED:
        with open(os.path.join(RESOURCES, name), "rb") as f:
            data = f.read()
        table = json.loads(data)  # un fichier illisible ne part pas
        check_not_shrinking(name, table)
        hashes[name] = write(name, data)

    manifest = json.dumps({"schema": SCHEMA, "files": hashes}, indent=2, sort_keys=True).encode("utf-8")
    write("manifest.json", manifest)
    # Sans ce fichier, GitHub Pages passe les fichiers dans Jekyll.
    write(".nojekyll", b"")

    print(f"{len(series)} séries, {len(extensions)} extensions, {len(hashes)} fichiers prêts dans CatalogSite/.")

    if args.push:
        git("add", "-A")
        if subprocess.run(["git", "-C", SITE, "diff", "--cached", "--quiet"]).returncode == 0:
            print("Rien de nouveau à publier.")
            return
        git("commit", "-m", f"Catalogue : {len(extensions)} extensions")
        git("push")
        print("Publié. Les apps le récupèrent au plus tard 12 heures après, puis l'affichent au lancement suivant.")


if __name__ == "__main__":
    main()
