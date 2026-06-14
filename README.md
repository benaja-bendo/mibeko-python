# Mibeko Python - Interface d'Ingestion et Parsing Juridique

Ce projet Python a pour but de centraliser l'ingestion, l'extraction OCR et la structuration (parsing) de textes juridiques. Il offre à la fois un **Tableau de Bord Web temps réel (FastAPI + HTMX/Alpine.js)** et une **CLI (Command Line Interface)**.
Il orchestre le flux de documents entre le stockage local/S3 (MinIO), l'OCR asynchrone (via MinerU), et le stockage en base de données PostgreSQL.

## 🚀 Fonctionnalités Principales

1. **Interface Web Réactive** : Upload de documents PDF (STOCK et FLUX) et suivi en temps réel du statut de l'OCR via SSE (Server-Sent Events).
2. **Intégration MinIO** : Stockage automatique des PDF sources et des fichiers d'extraction (`.md`, `.json`) dans des buckets S3.
3. **OCR avec MinerU** : Soumission asynchrone des documents à MinerU et téléchargement automatique des résultats de parsing structurés.
4. **Parsing et NLP local** : Structuration fine et insertion en base de données via SQLAlchemy, PyMuPDF et spaCy.

## 🛠️ Prérequis

- Python 3.10 ou plus
- **PostgreSQL** en cours d'exécution (port 5433)
- **MinIO** en cours d'exécution pour le stockage S3
- Tesseract OCR (Optionnel, selon les méthodes de parsing locales)

## 📦 Installation et Configuration

1. **Créer et activer un environnement virtuel :**
   ```bash
   cd mibeko-python
   python3 -m venv venv
   source venv/bin/activate
   ```
2. **Installer les dépendances :**
   ```bash
   pip install -r requirements.txt
   ```
3. **Télécharger le modèle de langue français pour spaCy :**
   ```bash
   python -m spacy download fr_core_news_sm
   ```
4. **Configuration de l'environnement (`.env`) :**
   Créez un fichier `.env` à la racine de `mibeko-python` :
   ```env
   # Base de données PostgreSQL
   DB_CONNECTION=pgsql
   DB_HOST=127.0.0.1
   DB_PORT=5433
   DB_DATABASE=mibeko
   DB_USERNAME=root
   DB_PASSWORD=root

   # Configuration MinIO
   MINIO_HOST=127.0.0.1
   MINIO_PORT=9000
   MINIO_ACCESS_KEY=root
   MINIO_SECRET_KEY=password
   MINIO_SECURE=false

   # Configuration MinerU (OCR)
   MINERU_API_URL=https://mineru.net/api/v4
   MINERU_API_KEY=votre_cle_api_ici
   ```

## 💻 Utilisation (Serveur Web & CLI)

Le point d'entrée principal est le fichier `main.py` qui utilise `Click` pour grouper les commandes.

### 1. Lancer le Tableau de Bord Web (Recommandé)

Pour démarrer l'interface graphique permettant d'uploader et de suivre les documents :

```bash
# Dans le dossier mibeko-python, avec l'environnement virtuel activé
python main.py serve --port 8001
```

- Ouvrez votre navigateur sur **<http://localhost:8001>**
- La documentation API interactive est disponible sur **<http://localhost:8001/api/v1/docs>** (Swagger UI) et **<http://localhost:8001/api/v1/redoc>** (ReDoc).
- Voir la [Documentation détaillée de l'API](docs/API.md) pour plus d'informations.
- L'interface utilise les Server-Sent Events (SSE) pour rafraîchir le tableau automatiquement dès qu'un document est uploadé ou que MinerU a terminé son extraction.

### 2. Flux de l'Interface Web

- **Onglet STOCK / FLUX** : Choisissez la nature du document.
- **Upload** : Fournissez un PDF.
  - Si vous ne fournissez **que** le PDF, il sera envoyé en tâche de fond à MinerU.
  - Si vous fournissez les options **.md** ou **.json**, le document bypass MinerU et sauvegarde directement les extractions dans MinIO et PostgreSQL. Vous pouvez sélectionner **plusieurs fichiers** (.md ou .json) ; ils seront automatiquement fusionnés (utile pour les gros documents découpés en plusieurs morceaux).
- **Extraire le contenu** : Une fois le statut passé à `completed`, un bouton apparaît dans le tableau pour lancer le parsing SQL depuis le `.md` ou `.json`. Des `CurationFlag` sont également générés en cas d'anomalies de numérotation (trous ou doublons).

## 📂 Structure du projet

```
mibeko-python/
├── main.py                     # Point d'entrée principal (CLI & Web)
├── requirements.txt            # Liste des dépendances (FastAPI, SQLAlchemy, Minio, etc.)
├── schema_postgres.sql         # Définition exacte du schéma SQL
├── src/
│   ├── api/                    # Serveur FastAPI
│   │   ├── main.py             # Routes et logique Web (Upload, SSE, Parse)
│   │   ├── static/             # Fichiers CSS/JS (si besoin)
│   │   └── templates/          # Vues HTML (index.html avec Tailwind/Alpine.js)
│   ├── db/
│   │   ├── database.py         # Connexion PostgreSQL
│   │   └── models.py           # Modèles SQLAlchemy (LegalDocument, MediaFile, ExtractionRun...)
│   ├── services/
│   │   ├── minio_service.py    # Client MinIO S3
│   │   └── mineru_service.py   # Client HTTP asynchrone pour MinerU
│   └── extractor/
│       ├── chunk_merger.py           # Fusion de fichiers MD/JSON extraits en morceaux
│       ├── compilation_splitter.py   # Découpage spécifique (ex: Journaux Officiels)
│       └── parser.py                 # Logique NLP/Structuration locale et extraction de tables
└── storage/                    # Fichiers temporaires
```

