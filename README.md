# Mibeko Python — Service d'ingestion et de parsing juridique

> Statut : à jour au 2 juillet 2026 · Service FastAPI interne d'ingestion, d'extraction OCR (MinerU) et de structuration des textes juridiques congolais.

Ce dépôt héberge le service Python qui centralise l'ingestion, l'extraction OCR et la structuration (parsing) des textes juridiques du Congo-Brazzaville. C'est une **brique d'infrastructure interne** (domaine `python.mibeko.fr`) consommée par le backend Laravel (`mibeko-tableau-de-bord`) et le front éditeur (`mibeko-front`, espace `/editor`) via les routes `/api/v1/*` protégées par token Sanctum. Il n'est pas exposé comme site public. Il orchestre le flux de documents entre le stockage S3 (MinIO), l'OCR (MinerU) et la base PostgreSQL partagée avec Laravel.

## Fonctionnalités principales

1. **Ingestion de documents** : upload de PDF (STOCK ou FLUX) et, en option, d'artefacts d'extraction `.md`/`.json` (fusionnés si découpés en morceaux), stockés dans MinIO.
2. **OCR avec MinerU** : soumission des PDF à MinerU (backend `cloud` SaaS par défaut, ou `local` auto-hébergé) et récupération des artefacts d'extraction.
3. **Parsing et structuration** : reconstruction de la hiérarchie (nœuds, articles, versions), insertion en PostgreSQL, détection d'anomalies de numérotation (`CurationFlag`).
4. **Temps réel (SSE)** et **retraitement non destructif** (replay/staging) pour l'outil d'ingestion du front éditeur.
5. **CLI** (`main.py`) pour la fusion de chunks et le découpage de compilations (Journaux Officiels, Actes uniformes).

Pour le détail, voir la [documentation technique](docs/README.md) : [architecture](docs/architecture.md) et [API](docs/API.md).

## Prérequis

- Python 3.10+ (image Docker de production : `python:3.11-slim`).
- **PostgreSQL** accessible (dev local : port 5433 ; le schéma est piloté par Laravel — le service ne crée aucune table).
- **MinIO** pour le stockage S3.
- Une clé MinerU (backend `cloud`) ou un serveur MinerU local (harnais `../minerU-docker`, backend `local`).

## Installation

1. **Environnement virtuel :**
   ```bash
   cd mibeko-python
   python3 -m venv venv
   source venv/bin/activate
   ```
2. **Dépendances :**
   ```bash
   pip install -r requirements.txt
   ```
3. **Configuration (`.env`)** — copier `.env.example` et renseigner les valeurs. Points clés :
   ```env
   # Base de données PostgreSQL (partagée avec Laravel)
   DB_HOST=127.0.0.1
   DB_PORT=5433
   DB_DATABASE=mibeko
   DB_USERNAME=...
   DB_PASSWORD=...

   # MinerU (OCR) : cloud (défaut) ou local
   MINERU_BACKEND=cloud
   MINERU_API_URL="https://mineru.net/api/v4"
   MINERU_API_KEY=...

   # MinIO
   MINIO_HOST=127.0.0.1
   MINIO_PORT=9000
   MINIO_ACCESS_KEY=...
   MINIO_SECRET_KEY=...

   # Exposition (brique interne)
   APP_ENV=production          # verrouille docs + console
   EXPOSE_API_DOCS=false       # Swagger/ReDoc ; true en local
   INGESTION_CONSOLE_ENABLED=false
   ```

## Lancement

### Serveur web (FastAPI)

```bash
# Dans mibeko-python, environnement virtuel activé
python main.py serve --port 8001
```

Le service écoute sur le **port 8000** (valeur par défaut, cohérente avec le `Dockerfile` et le déploiement Docker/Traefik).

- Page d'identité du service : `http://localhost:8000/`
- Health check : `http://localhost:8000/api/v1/health`
- Documentation OpenAPI (hors production ou `EXPOSE_API_DOCS=true`) : `http://localhost:8000/api/v1/docs` (Swagger) et `/api/v1/redoc` (ReDoc).

L'ingestion réelle passe par le front éditeur (`app.mibeko.fr`, espace `/editor`), qui appelle les routes `/api/v1/*` avec un token Sanctum et écoute le flux SSE `/api/v1/stream`. La console HTMX héritée sur `/console` est désactivée par défaut.

### CLI

Le point d'entrée `main.py` (Click) regroupe des commandes utilitaires : `serve`, `merge-chunks` (fusion de chunks MinerU MD/JSON), `suggest-boundaries` / `split-compilation` (découpage de compilations en Actes, en JSON ou en Markdown). Lister les commandes :

```bash
python main.py --help
```

## Structure du projet

```
mibeko-python/
├── main.py                     # CLI (Click) : serve, merge-chunks, split-compilation…
├── requirements.txt            # Dépendances (FastAPI, SQLAlchemy, Minio, PyMuPDF…)
├── schema_postgres.sql         # Référence documentaire du schéma (piloté par Laravel, non appliqué ici)
├── Dockerfile / .deploy/       # Image et déploiement Docker (port 8000, Traefik)
├── docs/                       # Documentation technique (README, architecture, API)
├── src/
│   ├── api/
│   │   ├── main.py             # Application FastAPI : routes, upload, SSE, parse, staging
│   │   ├── auth.py             # Validation des tokens Sanctum + require_editor
│   │   ├── config.py           # Exposition (docs/console, env)
│   │   ├── routers/            # Routeurs (documents)
│   │   ├── templates/          # Page de statut (Jinja2)
│   │   └── static/             # Fichiers statiques
│   ├── db/
│   │   ├── database.py         # Connexion PostgreSQL (init_db = no-op)
│   │   └── models.py           # Modèles SQLAlchemy (à synchroniser manuellement avec Laravel)
│   ├── services/
│   │   ├── minio_service.py    # Client MinIO (S3)
│   │   └── mineru_service.py   # Client MinerU (backends cloud/local)
│   └── extractor/
│       ├── chunk_merger.py         # Fusion de chunks MD/JSON
│       ├── compilation_splitter.py # Découpage (Journaux Officiels, Actes uniformes)
│       └── parser.py               # Structuration NLP locale
└── storage/                    # Fichiers temporaires locaux
```

## Points d'exploitation à connaître

- **Base partagée, schéma Laravel** : `init_db()` est volontairement un no-op ; les modèles Python doivent rester synchronisés à la main (risque de drift).
- **SSE protégé** : `/api/v1/stream` exige `require_editor`.
- **Pas de file durable** : une extraction ou un parsing en cours est perdu si le service redémarre (run orphelin) ; relancer via `/parse` ou `/reprocess`.

