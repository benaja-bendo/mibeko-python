# `sources/` — PDF originaux, immuables

Pourquoi : ce sont les seuls fichiers qu'on ne peut pas régénérer. Toute la vérifiabilité du corpus (SHA-256, provenance) part d'ici. Voir le contrat général : [`../README.md`](../README.md).

## Ce qui ne doit jamais changer

- **Le contenu d'un PDF déjà référencé par une entrée manifeste ne bouge jamais.** Renommer ou déplacer le fichier n'est pas un problème (le `sha256` ne change pas, seul le chemin bouge) ; le rouvrir/re-exporter/compresser en est un — le `sha256` stocké deviendrait faux et la provenance mensongère.

## Ce que tu peux faire librement

Réorganiser l'arborescence comme tu veux (renommer un sous-dossier, changer la profondeur, regrouper par année plutôt que par type, etc.) — **à condition de répercuter le changement dans le champ `fichier`** de chaque entrée manifeste concernée (voir [`../README.md`](../README.md) point 3).

**Exemple** : tu déplaces un PDF d'un sous-dossier à un autre → tu mets à jour `fichier` dans le manifeste correspondant, tu ne touches PAS à `sha256` (le contenu n'a pas changé, seule son adresse a bougé).

## Piège connu

Les extensions sont parfois mélangées (`.pdf` / `.PDF` selon la source d'origine) — le code qui lit `fichier` est sensible à la casse. Le champ manifeste doit reprendre l'extension **exacte** du fichier réel sur le disque.
