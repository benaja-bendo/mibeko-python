# Documentation du service Python d'ingestion

> Statut : à jour au 2 juillet 2026 · Index de la documentation technique de `mibeko-python`.

Cette documentation décrit le service FastAPI d'ingestion et de structuration des textes juridiques congolais (`mibeko-python`, domaine `python.mibeko.fr`), brique interne consommée par le backend Laravel et le front éditeur.

## Documents

| Document | Description |
| --- | --- |
| [architecture.md](architecture.md) | Pipeline d'ingestion (upload → MinerU/OCR → parsing → PostgreSQL), notions STOCK/FLUX, replay/staging via `extraction_runs.meta`, et faits d'exploitation/sécurité (base partagée sans migration côté Python, SSE protégé, absence de file durable). |
| [API.md](API.md) | Référence des endpoints `/api/v1/*`, port réel du service (8000), authentification par token Sanctum (`require_editor`) et documentations OpenAPI. |

Voir aussi le [README](../README.md) à la racine du dépôt pour l'installation et le lancement local.

## Conventions

Chaque document est daté et évolutif : il commence par une ligne « Statut : à jour au … » indiquant la date de dernière vérification et sa portée. La documentation reflète l'état réel du code à cette date ; en cas de doute entre la doc et le code, le code fait foi et la doc doit être corrigée.
