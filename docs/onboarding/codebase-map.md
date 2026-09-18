# Où trouver quoi

> Statut : à jour au 18 septembre 2026 · Repères durables dans le dépôt, en complément de l’explorateur de fichiers.

Cette page décrit les responsabilités des dossiers plutôt qu’une arborescence figée. Pour l’architecture du pipeline et ses choix métier, voir [architecture.md](../architecture.md). Pour le parcours de découverte, voir [README.md](README.md).

## Entrées principales

- `main.py` : CLI Click pour l’acquisition, le parsing, la structuration et les diagnostics.
- `src/api/main.py` : application FastAPI, cycle de vie, routes principales et traitements HTTP.
- `src/api/routers/documents.py` : consultation, upload, parsing, retraitement, suppression et restauration des documents.
- `src/api/auth.py` : authentification Sanctum et contrôle des rôles Laravel.

## Pipeline

- `src/acquisition/` : téléchargement, provenance, manifestes et règles de politesse.
- `src/parsing/` : triage PyMuPDF/MinerU et orchestration des lots.
- `src/services/` : intégrations MinerU, MinIO, Mistral et utilitaires PDF.
- `src/extractor/` : parseur juridique, qualité OCR, pagination, tableaux et découpage/fusion.
- `src/structuration/` : validation des métadonnées, insertion de la hiérarchie et traitement des Journaux Officiels.
- `src/promotion/` : opérations de promotion vers le corpus cible, avec garde-fous de production.

## Données et persistance

- `src/db/` : modèles SQLAlchemy, sessions, contrôle de schéma et accès de production en lecture seule.
- `data/sources/` : PDF originaux immuables.
- `data/manifests/` : traces de provenance JSONL.
- `data/pipeline/` : artefacts régénérables, notamment Markdown, JSON et métriques.
- `schema_postgres.sql` : référence documentaire ; les migrations Laravel restent la source d’autorité.

## Vérification

- `tests/` : comportements verrouillés par les tests unitaires et d’intégration.
- `scripts/` : diagnostics et opérations ponctuelles, à lire avant toute exécution.
- `docs/` : architecture, API et onboarding ; l’index est [docs/README.md](../README.md).
