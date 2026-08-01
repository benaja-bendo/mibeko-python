# Dossier `data/` — pourquoi il existe, et le contrat à respecter

Ce document ne décrit **pas** l'état actuel des dossiers (quels lots existent, combien de PDF, quelle profondeur d'arborescence) — cet inventaire change dès que tu réorganises, et un README qui l'affirme devient faux au premier renommage. Il décrit le **contrat** entre ce dossier et le code (`mibeko-python`) qui le lit : ce que tu peux changer librement, et ce à quoi le code tient vraiment. Pour voir l'état réel à un instant T :

```bash
find data -maxdepth 3 -type d
cat data/manifests/*.jsonl | jq -r .id | head
```

## Pourquoi ce dossier existe

C'est le corpus juridique local (Congo-Brazzaville + OHADA) qui alimente le pipeline d'ingestion : PDF source → artefacts MinerU → structuration → base PostgreSQL en staging. Trois natures de contenu bien distinctes, qui ne se traitent pas pareil :

- des **sources irremplaçables** (les PDF originaux) — les perdre, c'est perdre le corpus ;
- des **artefacts régénérables** (sorties MinerU/parsing) — les perdre, c'est perdre du temps de calcul, pas de la donnée ;
- de la **provenance et un carnet de commandes** — la seule preuve traçable de d'où vient chaque texte, versionnée en git.

## Le contrat : ce à quoi le code tient, peu importe comment tu ranges les choses

Tu es libre de renommer, réorganiser, aplatir ou complexifier n'importe quel sous-dossier ici — le code ne dépend jamais d'un nom de dossier en dur en dehors des points ci-dessous. À chaque fois que tu casses un de ces points, dis-toi que c'est *toi* qui dois répercuter le changement dans le code ou les manifestes, pas l'inverse.

### 1. Un seul point d'entrée : `MIBEKO_DATA_DIR`

Tout part de `data_dir()` dans [`src/acquisition/config.py`](../src/acquisition/config.py). Défaut : `mibeko-python/data` (ce dossier). Tu peux le faire pointer ailleurs (autre disque, dossier partagé entre plusieurs checkouts) via la variable d'env `MIBEKO_DATA_DIR` dans `.env` — rien d'autre dans le code n'a de chemin `data/` en dur.

Piège à connaître : une valeur **relative** (ex. `MIBEKO_DATA_DIR=data/`) se résout par rapport à ton dossier de travail shell au moment où tu lances la commande (`Path(...).resolve()`), pas automatiquement par rapport à `mibeko-python/`. Si tu lances `python main.py ...` depuis un autre dossier, ça ne pointera pas où tu crois. Un chemin **absolu** est plus sûr, ou alors toujours lancer les commandes depuis `mibeko-python/`.

### 2. Trois sous-dossiers ont un accesseur dédié — à modifier si tu les renommes

`sources_dir()`, `manifests_dir()`, `corpus_file()` (même fichier) pointent respectivement vers `data_dir()/sources`, `data_dir()/manifests`, `data_dir()/corpus/corpus-v1.yaml`. Renomme un de ces sous-dossiers, mets à jour la fonction correspondante — tout le reste du pipeline continue de marcher sans autre modif, puisque personne d'autre ne reconstruit ces chemins.

**Exemple** — tu veux renommer `corpus/corpus-v1.yaml` en `commandes/carnet.yaml` :

```python
def corpus_file() -> Path:
    return data_dir() / "commandes" / "carnet.yaml"
```

Rien d'autre à toucher.

### 3. Le manifeste est le seul lien entre une entrée et son PDF — jamais la structure de dossiers

Chaque ligne JSONL a un champ `fichier` (chemin du PDF, relatif à `data_dir()`) et un `sha256`. Le code ne parcourt jamais `sources/` pour deviner où est un fichier : il lit `fichier`. Tu peux donc réorganiser `sources/` comme tu veux, tant que tu répercutes le changement dans le champ `fichier` des entrées concernées.

**Exemple** — tu renommes `sources/sgg/JO/` en `sources/sgg/journaux-officiels/` : il faut réécrire `fichier` pour chaque entrée JO dans `manifests/sgg-jo.jsonl` (par script, pas à la main sur des dizaines de lignes). Oublie ça, et le pipeline cherchera le PDF au vieil emplacement et échouera au parsing (« PDF introuvable »). Le `sha256`, lui, ne change pas — le contenu du fichier est identique, seul son adresse a bougé.

### 4. L'`id` d'une entrée manifeste est une clé stable, pas un détail cosmétique

Format `<lot>/<slug>` (ex. `sgg-codes/congo-code-2005-minier`). Cet id sert à retrouver les artefacts dans `pipeline/` (point 5) ET, une fois le document structuré en base, le `document_key` Postgres dérive de son dernier segment.

- Entrée au statut `telecharge` ou `erreur` (pas encore en base) → tu peux renommer son id librement.
- Entrée au statut `structure` (déjà en base) → **ne renomme pas** son id sans plan : au prochain passage du pipeline, ça crée un **doublon en base** au lieu d'un renommage propre. Si tu dois vraiment le faire, prévois aussi de traiter la ligne existante côté base, pas juste le manifeste.

### 5. Les artefacts (`pipeline/`) : deux formes acceptées, à garder cohérentes avec le code

`pipeline/<kind>/<id>.<kind>` (forme normale, `kind` ∈ `md`/`json`/`metrics`, le `/` de l'id crée un sous-dossier par lot) ou, en repli, `pipeline/<kind>/<basename-de-l-id>.<kind>` à plat (documents traités avant l'existence de cette convention). Si tu inventes une troisième forme, mets à jour **ensemble** `artefact_paths` ([`src/parsing/batch.py`](../src/parsing/batch.py)) et `_resolve_artefact` ([`src/structuration/structurer.py`](../src/structuration/structurer.py)) — ils doivent être d'accord, sinon la structuration ne retrouvera pas un md/json pourtant présent sur le disque.

### 6. Le versionnement git est une liste blanche, pas un dossier figé

Le `.gitignore` de `mibeko-python` ignore tout `data/**` sauf des motifs explicites (aujourd'hui : les `.jsonl` de `manifests/`, `corpus/corpus-v1.yaml`, ce README, et les `.keep`). Renomme/déplace un de ces éléments → mets à jour ces motifs en même temps, sinon tu perds silencieusement le suivi de la provenance, ou à l'inverse tu commences à committer des PDF par accident.

**Après toute modif de structure ou du `.gitignore`, vérifie** :

```bash
cd mibeko-python && git add -An data/
```

→ ne doit lister que des manifestes, le carnet, ce README et des `.keep`. Rien d'autre. Si un PDF apparaît dans cette liste, corrige le `.gitignore` avant de commit quoi que ce soit.

## Historique

21 juillet 2026 : fusion de deux dossiers `data/` (un à la racine du monorepo, un résidu de test ici) en un seul. Le filet de sécurité de cette migration (`_archive-data-migration-20260721/`, 20 Mo) a été supprimé le 1er août 2026 après vérification : les 481 entrées de provenance y étaient identiques (`id` + `sha256`) à celles d'ici, les 3 PDF qu'il conservait étaient des doublons au SHA-256 près de fichiers déjà présents dans `sources/`, et l'artefact récupéré à l'époque (`sgg-codes/congo-code-1975-travail`) est bien intégré au corpus.
