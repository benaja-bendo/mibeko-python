# Documentation de l'API Mibeko Python (v1)

> Statut : à jour au 2 juillet 2026 · Référence des endpoints `/api/v1/*` du service interne d'ingestion (FastAPI).

L'API Mibeko Python est développée avec FastAPI et versionnée en `v1`. Elle prend en charge l'ingestion, l'extraction OCR (MinerU) et la structuration (parsing) des documents juridiques, avec écriture en base PostgreSQL partagée avec Laravel. C'est une brique interne : elle est consommée par le backend Laravel et le front éditeur, jamais exposée comme site public.

## Port et hôte

Le service écoute sur le **port 8000**. Cette valeur est cohérente partout dans le code et l'infrastructure : `Dockerfile` (`ENV PORT=8000`, `EXPOSE 8000`), `.deploy/docker-compose.yml` (`PORT=8000`, Traefik `server.port=8000`, healthcheck sur `:8000`) et la commande CLI `python main.py serve` (défaut `--port 8000`).

- En local : `http://localhost:8000`
- En production : `https://python.mibeko.fr` (derrière Traefik)

## Authentification

Toutes les routes d'ingestion (`/api/v1/documents/*`, `/api/v1/stream`, arbitrage de runs) exigent un **token Sanctum** porté dans l'en-tête `Authorization: Bearer <token>`. Le token est validé directement en base (table Laravel `personal_access_tokens`), et le rôle de l'utilisateur est vérifié via `require_editor` :

- token absent ou invalide → `401` ;
- utilisateur sans rôle `admin` ou `editor` → `403`.

Le flux SSE `/api/v1/stream` est lui aussi protégé par `require_editor` : il se consomme via `fetch` + `ReadableStream` pour pouvoir porter l'en-tête `Authorization` (impossible avec `EventSource` natif). Seul `GET /api/v1/health` est public.

## Documentations interactives (OpenAPI)

FastAPI génère automatiquement une documentation OpenAPI, mais elle est **désactivée par défaut en production** (`EXPOSE_API_DOCS=false`) car elle décrit des opérations d'écriture internes. Hors production (ou avec `EXPOSE_API_DOCS=true`), elle est disponible sur :

- **Swagger UI** : `http://localhost:8000/api/v1/docs`
- **ReDoc** : `http://localhost:8000/api/v1/redoc`
- **OpenAPI JSON** : `http://localhost:8000/api/v1/openapi.json`

## Endpoints principaux

Sauf `/` et `/console`, tous les endpoints sont préfixés par `/api/v1`.

### 1. Système

- `GET /api/v1/health`
  - **Description** : vérifie l'état de l'API et de la connexion à la base.
  - **Auth** : aucune (endpoint public, utilisé par le healthcheck Docker).
  - **Réponse** : JSON `status`, `service`, `version`, `db` (`ok`/`error`), `timestamp`.

### 2. Documents juridiques

- `GET /api/v1/documents`
  - **Description** : liste paginée des documents. **Paramètres** : `limit`, `offset`, `status`.
- `GET /api/v1/documents/stats`
  - **Description** : indicateurs globaux (total, en cours, terminés…).
- `GET /api/v1/documents/{doc_id}`
  - **Description** : détails complets d'un document.
- `DELETE /api/v1/documents/{doc_id}`
  - **Description** : soft-delete réversible du document et de ses articles ; aucun fichier ni artefact n'est purgé.
- `POST /api/v1/documents/{doc_id}/restore`
  - **Description** : restaure le document et les articles retirés lors de la même opération.
- `POST /api/v1/documents/upload`
  - **Description** : dépose un PDF (et, en option, des extractions `.md`/`.json`) dans MinIO et en base.
  - **Type de contenu** : `multipart/form-data`.
  - **Paramètres** : `titre_officiel`, `document_role` (`STOCK`/`FLUX`, défaut `STOCK`), `stock_code` (obligatoire si `STOCK`), `document_key`, `pdf_file`, `md_file` (plusieurs fichiers acceptés, fusionnés), `json_file` (idem), plus métadonnées optionnelles (`type_code`, `institution_sigle`, dates, `legal_scope`, `curation_status`).
  - **Comportement** : PDF seul → extraction MinerU en tâche de fond ; avec `.md`/`.json` → court-circuit MinerU. `409` si la clé de document existe déjà, `422` si le rôle/`stock_code` sont invalides.
- `POST /api/v1/documents/{doc_id}/parse`
  - **Description** : déclenche le parsing structurel depuis l'artefact `.md` ou `.json`. Détecte les anomalies de numérotation (trous, doublons) et génère des `CurationFlag`.
  - **Paramètre** : `source_format` (`md` ou `json`).
- `POST /api/v1/documents/{doc_id}/reprocess`
  - **Description** : relance l'extraction MinerU depuis le PDF source stocké dans MinIO.

### 3. Fichiers et exécutions (runs)

- `GET /api/v1/documents/{doc_id}/runs` — liste les exécutions d'extraction/parsing du document.
- `GET /api/v1/documents/{doc_id}/files` — liste les artefacts stockés (PDF, MD, JSON).
- `GET /api/v1/documents/{doc_id}/stats` — statistiques du document (articles, nœuds, runs, fichiers).
- `GET /api/v1/documents/{doc_id}/articles` — liste paginée des articles et de leurs versions.

### 4. Retraitement non destructif (replay / staging)

Ces endpoints arbitrent une proposition de re-parsing parquée dans `extraction_runs.meta` sans écraser le contenu curé (voir [architecture.md](architecture.md)).

- `GET /api/v1/documents/{doc_id}/runs/{run_id}/diff` — compare la proposition en attente au contenu live.
- `POST /api/v1/documents/{doc_id}/runs/{run_id}/promote` — applique la proposition (seul point d'écrasement, décision humaine).
- `POST /api/v1/documents/{doc_id}/runs/{run_id}/discard` — rejette la proposition, le live reste intact.

### 5. Temps réel (Server-Sent Events)

- `GET /api/v1/stream`
  - **Description** : flux SSE informant les clients de l'avancement en temps réel (extraction MinerU, parsing…), avec heartbeat.
  - **Auth** : `require_editor` (éditeurs/admins). À consommer via `fetch` + `ReadableStream` pour porter l'en-tête `Authorization: Bearer`.

## À propos de la persistance et de la fiabilité

- Le schéma PostgreSQL est **piloté par Laravel** ; le service Python ne crée aucune table (`init_db` no-op). Voir [architecture.md](architecture.md) pour le risque de drift.
- Il n'existe **pas de file durable** : une extraction ou un parsing en cours est perdu si le service redémarre, laissant un run orphelin. Relancer manuellement via `/parse` ou `/reprocess`.
