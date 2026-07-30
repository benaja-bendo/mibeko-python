# `corpus/` — le carnet de commandes, versionné

Pourquoi : `corpus-v1.yaml` liste les textes que l'usine doit traiter — seuls les textes présents ici sont dans le périmètre. C'est la commande, pas le résultat. Voir le contrat général : [`../README.md`](../README.md).

## La règle qui compte

Le contenu est validé par Benji, pas généré automatiquement — l'usine **lit** ce fichier, elle ne l'écrit jamais.

Si tu renommes ou déplaces ce fichier, mets à jour `corpus_file()` dans `src/acquisition/config.py` (voir [`../README.md`](../README.md) point 2) — c'est le seul endroit du code qui connaît son emplacement.
