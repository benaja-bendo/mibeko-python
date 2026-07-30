# `manifests/` — provenance, versionnée : la seule trace qui compte

Pourquoi : c'est LE fichier qui prouve d'où vient chaque texte (URL, date de récupération, SHA-256, n°/date de JO) — l'invariant « provenance obligatoire » de la mission usine à textes. Perdu, ce n'est pas régénérable sans retélécharger et re-dater chaque source. Voir le contrat général : [`../README.md`](../README.md).

## La règle qui compte le plus

Un fichier JSONL par « lot » (`<lot>.jsonl`), une ligne JSON par document. **Le nom du fichier EST le lot**, et il apparaît tel quel dans l'`id` de chaque entrée (`<lot>/<slug>`). Renommer `sgg-codes.jsonl` en `codes-sgg.jsonl` change donc l'id de TOUTES ses entrées — risque de doublon en base pour celles déjà structurées (voir [`../README.md`](../README.md) point 4). À faire seulement sur un lot dont aucune entrée n'est encore `structure`, ou avec un vrai plan de migration côté base.

## Champs qu'on retrouve sur chaque ligne

`id`, `fichier` (chemin relatif à `data_dir()`), `sha256`, `size_bytes`, `type_source`, `statut` (`telecharge` → `parse`/`erreur` → `structure`), `retroactif`, `evenements` (journal **append-only**, jamais réécrit) ; pour les JO en plus : `source_url`, `jo_annee`, `jo_numero`.

## Éditer une ligne à la main

À réserver au dernier recours (exemple réel : rattraper le cas `sgg-codes/congo-code-1975-travail`, dont le parse automatique avait échoué mais dont un artefact MinerU manuel existait ailleurs — voir l'événement `artefact_recupere` dans `sgg-codes.jsonl`). Dans ce cas : ajoute toujours un événement qui explique le pourquoi, ne réécris jamais l'historique existant.

## Versionnement

Ces fichiers sont, avec `corpus/corpus-v1.yaml`, les seuls suivis par git dans `data/` (voir le `.gitignore` de `mibeko-python`).
