# `pipeline/` — artefacts régénérables, jamais la source de vérité

Pourquoi : sorties de MinerU (JSON) et du parseur natif (Markdown), entièrement reproductibles en rejouant `process-batch` depuis `sources/`. Perdre ce dossier coûte du temps de calcul, pas de la donnée. Voir le contrat général : [`../README.md`](../README.md).

## Les deux formes acceptées par le code

- **Forme normale** : `<kind>/<id>.<kind>` où `id` = `<lot>/<slug>` (crée donc un sous-dossier par lot), `kind` ∈ `md`, `json`, `metrics`.
- **Repli legacy** : `<kind>/<basename-de-l-id>.<kind>` à plat (documents traités avant l'existence de cette convention).

Si tu inventes une troisième forme, mets à jour **ensemble** `artefact_paths` (`src/parsing/batch.py`) et `_resolve_artefact` (`src/structuration/structurer.py`) — ils doivent être d'accord sur la forme, sinon la structuration ne retrouvera pas un artefact pourtant présent.

## Ce qu'il ne faut pas faire

- **Ne jamais éditer un artefact à la main.** Il doit rester l'image fidèle de ce que MinerU/le parseur natif a produit, pour que « rejouable à l'identique » reste vrai. Contenu mauvais → nouveau run en amont, pas une retouche manuelle ici.
- Si un document a des artefacts aux **deux** emplacements (forme normale + legacy, ex. après une migration), c'est la forme normale qui est lue en premier — le fichier à plat devient mort. Vérifie qu'ils sont identiques avant de le supprimer.
