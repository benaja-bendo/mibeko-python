"""Étage 1 de l'usine à textes : acquisition des PDF officiels avec provenance.

Sous-modules :
- config      : résolution du dossier data/ et identité du bot.
- manifest    : manifeste de provenance (JSONL, un fichier par source).
- politeness  : client HTTP poli (User-Agent identifiable, délais, backoff).
- sgg         : grammaire des Journaux officiels sgg.cg + collecte.
- ohada       : reconnaissance des Actes uniformes sur ohada.org (pas de collecte
                de masse tant que les URLs n'ont pas été validées humainement).
- backfill    : rétro-remplissage du manifeste depuis data/sources/ existant.

Règles héritées de la mission (docs/missions/mission-usine-a-textes.md) :
sources officielles uniquement, data/sources/ immuable, jamais de
re-téléchargement d'un checksum connu, chaque étape idempotente et rejouable.
"""
