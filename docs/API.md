# Documentation de l'API Mibeko (v1)

L'API Mibeko Python est développée avec FastAPI et est versionnée en `v1`.
Elle permet l'ingestion, le traitement et la consultation de documents juridiques.

## 🔗 Accès aux documentations interactives

FastAPI génère automatiquement la documentation interactive basée sur OpenAPI :
- **Swagger UI** : `http://localhost:8004/api/v1/docs`
- **ReDoc** : `http://localhost:8004/api/v1/redoc`
- **OpenAPI JSON** : `http://localhost:8004/api/v1/openapi.json`

## 📋 Endpoints Principaux

Tous les endpoints commencent par le préfixe `/api/v1`.

### 1. Système

*   `GET /api/v1/health`
    *   **Description** : Vérifie l'état de santé de l'API et de la base de données.
    *   **Réponse** : JSON contenant le statut (`ok` ou `error`), la version et le timestamp.

### 2. Documents Juridiques

*   `GET /api/v1/documents`
    *   **Description** : Liste les documents juridiques enregistrés (paginée).
    *   **Paramètres** : `limit`, `offset`, `status` (filtrage par statut d'extraction).

*   `GET /api/v1/documents/stats`
    *   **Description** : Retourne les indicateurs de performance (KPIs) globaux (nombre total de documents, en cours, terminés, etc.).

*   `GET /api/v1/documents/{doc_id}`
    *   **Description** : Récupère les détails complets d'un document spécifique.

*   `DELETE /api/v1/documents/{doc_id}`
    *   **Description** : Supprime un document et toutes ses données associées (articles, versions, fichiers médias, etc.).

*   `POST /api/v1/documents/upload`
    *   **Description** : Téléverse un nouveau document PDF (ainsi que les extractions optionnelles Markdown ou JSON) dans MinIO et la base de données.
    *   **Type de contenu** : `multipart/form-data`
    *   **Paramètres** : `titre_officiel`, `document_role`, `stock_code`, `document_key`, `pdf_file`, `md_file`, `json_file`.

*   `POST /api/v1/documents/{doc_id}/parse`
    *   **Description** : Déclenche le processus de parsing structurel d'un document à partir de son artefact extrait (MD ou JSON).
    *   **Paramètres** : `source_format` (`md` ou `json`).

### 3. Fichiers et Extractions (Runs)

*   `GET /api/v1/documents/{doc_id}/runs`
    *   **Description** : Liste les exécutions (runs) d'extraction liées à un document.

*   `GET /api/v1/documents/{doc_id}/files`
    *   **Description** : Liste les fichiers multimédias/artefacts (PDF, JSON, MD) stockés pour un document.

*   `GET /api/v1/documents/{doc_id}/stats`
    *   **Description** : Retourne les statistiques spécifiques à un document (nombre d'articles, nœuds, runs, fichiers).

*   `GET /api/v1/documents/{doc_id}/articles`
    *   **Description** : Liste paginée des articles extraits d'un document, avec leurs versions.

### 4. Temps Réel (Server-Sent Events)

*   `GET /api/v1/stream`
    *   **Description** : Expose un flux SSE (Server-Sent Events) pour informer les clients web des mises à jour en temps réel (avancement des tâches MinerU, état du parsing, etc.).
