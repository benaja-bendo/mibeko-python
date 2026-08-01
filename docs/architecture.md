# Architecture du service Python d'ingestion

> Statut : à jour au 2 juillet 2026 · Décrit le pipeline d'ingestion (upload → MinerU/OCR → parsing → PostgreSQL), les notions STOCK/FLUX et replay/staging, ainsi que les faits d'exploitation et de sécurité réels.

## Rôle du service

`mibeko-python` est une **brique d'infrastructure interne**, et non un site web. C'est un service FastAPI qui prend en charge l'ingestion, l'extraction OCR et la structuration (parsing) des textes juridiques congolais, puis les écrit dans PostgreSQL. Il est consommé par le backend Laravel (`mibeko-tableau-de-bord`) et par le front éditeur (`mibeko-front`, espace `/editor`) via les routes `/api/v1/*` protégées par token Sanctum. En production, il est servi derrière Traefik sur le domaine `python.mibeko.fr`.

Le fichier `src/api/config.py` matérialise cette posture : documentation OpenAPI et console HTMX héritée désactivées par défaut en production, en-têtes `X-Robots-Tag: noindex, nofollow` et durcissement HTTP appliqués à toutes les réponses.

## Vue d'ensemble du pipeline

Le corpus juridique suit la chaîne : **PDF source → JSON MinerU → Markdown → ingestion en base**. Le service orchestre trois étages :

1. **Dépôt (upload)** — `POST /api/v1/documents/upload` reçoit un PDF (obligatoire) et, en option, un ou plusieurs artefacts d'extraction `.md` et/ou `.json`. Le PDF et les artefacts sont poussés dans MinIO (S3), et un `LegalDocument` plus ses `MediaFile` sont créés en base.
   - Si **seul le PDF** est fourni, une tâche de fond soumet le document à MinerU (OCR/extraction) puis télécharge et stocke les artefacts produits.
   - Si des `.md` ou `.json` sont fournis, le document **court-circuite MinerU** : les artefacts sont stockés directement. Plusieurs morceaux (gros document découpé avant MinerU, ex. Code Bleu OHADA) sont fusionnés en un artefact unique à pagination globale via `src/extractor/chunk_merger.py`.
2. **Extraction OCR (MinerU)** — `src/services/mineru_service.py` sait cibler deux backends selon la variable `MINERU_BACKEND` :
   - `cloud` (défaut) : API SaaS MinerU (`https://mineru.net/api/v4`), asynchrone (soumission puis récupération du résultat) ;
   - `local` : serveur `mineru-api` auto-hébergé (harnais de dev `mineru-local/`), synchrone, langue OCR `fr`.
   Les deux backends exposent la même interface (`submit_pdf`, `get_results`, `download_result`).
3. **Parsing structurel → PostgreSQL** — `POST /api/v1/documents/{doc_id}/parse` déclenche `LegalDocumentParser` (`src/extractor/parser.py`) qui reconstruit la hiérarchie (nœuds structurels, articles, versions) depuis le `.md` ou le `.json`, puis l'insère en base. Le parsing détecte aussi les anomalies de numérotation d'articles (trous, doublons) et génère des `CurationFlag`. Un document sans structure détectable (circulaire, discours, proclamation…) bascule sur un article « Unique » contenant le texte intégral, afin de rester citable et publiable.

Après parsing, le document passe en `curation_status = review` : un éditeur doit contrôler le résultat côté Laravel avant publication. Seuls les `CurationFlag` de sévérité `blocking` empêchent la publication.

## STOCK vs FLUX

Le champ `legal_documents.document_role` distingue deux natures de documents (défaut `FLUX`) :

- **STOCK** — texte de fond consolidé et durable (code, loi structurante). Un `stock_code` est **obligatoire** (sinon `422`), sert de clé de regroupement et est plafonné à `varchar(100)` : un slug dérivé d'un titre trop long est refusé par un `422` explicite plutôt que de faire échouer l'INSERT. La date de consolidation (`consolidation_as_of`) est renseignée à l'upload.
- **FLUX** — actes ponctuels rattachés à leur source, typiquement les actes extraits d'un Journal Officiel (`OfficialJournal`), qui sert de parent aux textes FLUX.

Le rôle conditionne la clé de document, l'arborescence de stockage MinIO (`stock/` vs `flux/`) et le `type_code` par défaut (`CODE` pour un STOCK, `LOI` pour un FLUX).

## Replay / staging non destructif (`extraction_runs.meta`)

Chaque exécution est tracée par un `ExtractionRun`. La colonne JSONB `extraction_runs.meta` porte les métadonnées d'exécution et sert de zone de **staging** pour le retraitement non destructif :

- Lorsqu'un nouveau parsing tombe sur un document déjà porteur de **curation humaine**, le service **n'écrase pas** le contenu live. Il parque la proposition dans `meta.proposed_hierarchy`, passe le run en `needs_review` (`meta.staged = true`) et le document en `curation_status = review`.
- Un éditeur arbitre ensuite via trois endpoints :
  - `GET /api/v1/documents/{doc_id}/runs/{run_id}/diff` — compare la proposition en attente au contenu live ;
  - `POST …/promote` — applique la proposition (c'est **le seul endroit** où l'écrasement du contenu a lieu, sur décision humaine explicite) ;
  - `POST …/discard` — rejette la proposition ; le live reste intact.

En cas d'échec, le run passe en `failed` et `meta.error` conserve le message d'exception.

## Faits d'exploitation et de sécurité

Ces points reflètent l'état réel du code (juillet 2026) et doivent être connus avant toute intervention.

### Base PostgreSQL partagée, pilotée par Laravel (risque de drift)

Le service se connecte à la **même base PostgreSQL que Laravel**. Le schéma est **piloté uniquement par Laravel** (migrations). Côté Python, `init_db()` (`src/db/database.py`) est **volontairement un no-op** : il n'appelle pas `Base.metadata.create_all` pour ne pas interférer avec les migrations Laravel. Conséquence : les modèles SQLAlchemy (`src/db/models.py`) doivent rester **manuellement synchronisés** avec le schéma Laravel ; toute divergence est un **risque de drift** qui n'est pas détecté automatiquement. L'authentification s'appuie d'ailleurs directement sur les tables Laravel/Spatie (`personal_access_tokens`, `users`, `model_has_roles`, `roles`).

Le fichier `schema_postgres.sql` à la racine sert de référence documentaire du schéma, mais n'est pas appliqué par le service.

### SSE `/stream` protégé (`require_editor`)

Le flux temps réel `GET /api/v1/stream` (Server-Sent Events) diffuse des métadonnées d'ingestion (titres, ids, statuts d'avancement). Il est **réservé aux éditeurs et administrateurs** via `require_editor` : token absent ou invalide → `401`, rôle insuffisant → `403`. Le front le consomme via `fetch` + `ReadableStream` afin de porter l'en-tête `Authorization: Bearer` (impossible avec `EventSource` natif). L'ensemble des routes d'écriture et de lecture d'ingestion (`upload`, `parse`, `reprocess`, `diff`/`promote`/`discard`, `stream`) exigent `require_editor`.

### Pas de file durable (runs orphelins au redémarrage)

Le traitement asynchrone repose sur les `BackgroundTasks` de FastAPI et sur une liste **en mémoire** de files d'abonnés SSE (`event_queues`). Il n'existe **pas de file de messages durable** (ni Celery, ni broker). Conséquence : si le processus redémarre pendant qu'une extraction ou un parsing est en cours, la tâche est perdue et le `ExtractionRun` correspondant reste **orphelin** (bloqué en `running`/`queued`), sans reprise automatique. La relance se fait manuellement (`/reprocess` ou `/parse`).

### Exposition durcie

- CORS restreint à une liste blanche d'origines (`localhost:5173/5174`, `mibeko.fr`, `app.mibeko.fr`…).
- Un gestionnaire d'exception global ré-attache les en-têtes CORS sur les `500` : sans lui, une exception non gérée court-circuitait le middleware CORS et le navigateur affichait une fausse « erreur CORS » masquant le vrai `500`.
- En-têtes de sécurité systématiques (`X-Robots-Tag`, `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`, HSTS derrière HTTPS).
- Documentation OpenAPI (`EXPOSE_API_DOCS`) et console HTMX héritée (`INGESTION_CONSOLE_ENABLED`) désactivées par défaut en production.

## Modèle de données (aperçu)

Les modèles SQLAlchemy pertinents (`src/db/models.py`) :

- `Institution`, `OfficialJournal` — émetteurs et Journaux Officiels (parents des actes FLUX).
- `LegalDocument` — identité métier (rôle STOCK/FLUX, `type_code`, statuts d'extraction et de curation).
- `MediaFile` — artefacts physiques dans MinIO (PDF source, extractions MD/JSON).
- `ExtractionRun` — exécutions d'extraction/parsing, avec `meta` JSONB (staging, erreurs).
- `StructureNode`, `Article`, `ArticleVersion` — hiérarchie (arbre `ltree`), articles et versions datées.
- `CurationFlag` — anomalies à relire ; `severity = blocking` bloque la publication.

## Déploiement

- Image Docker (`Dockerfile`) basée sur `python:3.11-slim`, lancée par `uvicorn src.api.main:app` sur le **port 8000** (`ENV PORT=8000`, `EXPOSE 8000`).
- En production, `.deploy/docker-compose.yml` fixe `PORT=8000`, publie le service derrière Traefik (`server.port=8000`, host `python.mibeko.fr`), attache un middleware SSE (`X-Accel-Buffering: no`) et surveille `/api/v1/health`.
- Le workflow `.github/workflows/deploy-prod.yml` construit l'image, la pousse sur GHCR et la déploie par SSH sur le VPS (`/opt/docker/mibeko-python`).
