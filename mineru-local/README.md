# MinerU en local (Docker, CPU) — pour Mibeko

Faire tourner l'extraction PDF → Markdown/JSON **sans dépendre de l'API SaaS
MinerU**, afin de développer et tester la pipeline d'ingestion (`mibeko-python`)
de bout en bout, hors-ligne et sans coût/quota.

---

## ⚠️ À lire avant de commencer (Mac)

La doc officielle **déconseille Docker pour MinerU sur macOS** : Docker n'accède
ni au GPU Apple (MPS/MLX) ni à CUDA. Cette image tourne donc **en CPU**, en
`linux/amd64` (émulé sur les puces M). **Conséquence : ça marche, mais c'est
lent** (de quelques dizaines de secondes à plusieurs minutes par document selon
la taille). C'est parfait pour *tester et itérer*, pas pour traiter un gros lot.

➡️ Si la vitesse devient gênante, préférez l'**installation native** (plus bas) :
c'est la voie officielle pour Mac, avec accélération Apple GPU.

Sur un **serveur Linux** (CI, VPS), cette même image tourne sans émulation et
peut exploiter un GPU NVIDIA en adaptant le backend (`vlm`) — voir la doc MinerU.

---

## Prérequis

- Docker Desktop avec **≥ 6 Go de RAM** alloués (Settings → Resources). Les
  modèles « pipeline » consomment plusieurs Go en CPU ; en dessous, risque d'OOM.
- ~6–8 Go de disque (image + modèles téléchargés une fois dans un volume).

## Démarrage

```bash
cd mibeko-python/mineru-local
cp .env.example .env            # ajuster le port / la source des modèles au besoin

# 1) Construire l'image (long la 1re fois : torch + deps sous émulation amd64)
docker compose build

# 2) (Recommandé) pré-télécharger les modèles dans le volume partagé
#    Évite une 1re requête de parsing très lente.
docker compose --profile setup run --rm mineru-models

# 3) Lancer le serveur
docker compose up -d

# 4) Vérifier
curl http://localhost:8004/health         # → {"status":"healthy", ...}
open http://localhost:8004/docs           # Swagger (POST /file_parse, /tasks…)
```

Test rapide d'extraction (remplacer par un vrai PDF) :

```bash
curl -X POST http://localhost:8004/file_parse \
  -F "files=@Republique-du-Congo-Constitution-2015.pdf" \
  -F "backend=pipeline" \
  -F "return_md=true" \
  -F "return_middle_json=true" \
  -F "response_format_zip=false"
```

> **Important** : le backend par défaut de l'API exige un GPU → toujours envoyer
> `backend=pipeline` en local (déjà géré par `mibeko-python`).
>
> **Langue OCR** : l'OCR pipeline (PP-OCRv5) **n'accepte pas `fr`**. Pour le
> français (écriture latine) on **omet `lang_list`** : le modèle par défaut `ch`
> lit le latin. Valeurs acceptées : `ch`, `cyrillic`, `arabic`, `devanagari`,
> `korean`, `el`, `arabic`, `th`… (`mibeko-python` gère l'omission automatiquement).

## Brancher `mibeko-python` dessus

Dans `mibeko-python/.env` :

```dotenv
MINERU_BACKEND=local
MINERU_API_URL=http://localhost:8004     # = MINERU_PORT du .env (8000 = ton app Laravel !)
MINERU_LANG=fr                           # latin → lang_list omis automatiquement (pas de 400)
```

C'est tout. Le service `mineru_service.py` détecte `MINERU_BACKEND=local` et
appelle l'endpoint synchrone `/file_parse` ; le reste de la pipeline (parseur,
découpage, fusion de chunks) est inchangé. Repasser en SaaS = `MINERU_BACKEND=cloud`.

> Si `mibeko-python` tourne **aussi dans Docker** (même réseau), remplacer
> `localhost` par le nom du service (`http://mibeko-mineru:8000`) et raccorder
> les deux `compose` au même réseau.

---

## Alternative native (recommandée sur Mac, plus rapide)

Sans Docker, avec accélération Apple GPU (MPS) :

```bash
# Python 3.10–3.13 ; uv recommandé (https://docs.astral.sh/uv/)
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -U "mineru[core]"

# Télécharger les modèles une fois
mineru-models-download -s huggingface -m pipeline

# Lancer le serveur (mêmes endpoints que l'image Docker)
mineru-api --host 0.0.0.0 --port 8004   # 8000 est déjà pris par ton app Laravel
```

Puis la même config `mibeko-python/.env` que ci-dessus. MinerU détecte MPS
automatiquement ; pour forcer un mode : `export MINERU_DEVICE_MODE=mps` (ou `cpu`).

---

## Dépannage

| Symptôme | Cause probable / solution |
|---|---|
| Parsing échoue avec une erreur GPU/CUDA | `backend=pipeline` non transmis. En local Mibeko le gère ; en curl, l'ajouter. |
| Conteneur tué / « OOMKilled » | Augmenter la RAM Docker (≥ 6 Go) ; `shm_size` est déjà à 2 Go. |
| 1re requête très longue | Téléchargement des modèles. Lancer d'abord le profil `setup`. |
| Téléchargement modèles lent/bloqué | Basculer `MINERU_MODEL_SOURCE=modelscope` dans `.env`. |
| Build amd64 très lent | Normal sous émulation. Envisager l'install native ci-dessus. |
| `/health` KO au démarrage | Laisser ~1–3 min (chargement). `docker compose logs -f mineru`. |

## Maintenance

- **Changer de version MinerU** : éditer `ARG MINERU_VERSION` (Dockerfile) + le
  tag `image:` (compose.yaml), puis `docker compose build`.
- **Repartir de zéro (modèles inclus)** : `docker compose down -v` (supprime le
  volume `mineru-models`).

## Références

- Docker deployment — https://opendatalab.github.io/MinerU/quick_start/docker_deployment/
- Quick usage / API — https://opendatalab.github.io/MinerU/usage/quick_usage/
- PyPI (versions, extras) — https://pypi.org/project/mineru/
- Dépôt — https://github.com/opendatalab/MinerU
