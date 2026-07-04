# syntax=docker/dockerfile:1
#
# MinerU — image CPU pour développement local.
#
# Pourquoi une image maison plutôt que l'image officielle ?
#   L'image officielle d'opendatalab est basée sur vLLM/CUDA (GPU obligatoire).
#   Sur Mac, Docker n'a accès ni au GPU NVIDIA ni au GPU Apple (MPS/MLX) : on
#   construit donc une image CPU minimale, en linux/amd64 pour la compatibilité
#   des wheels (torch, doclayout-yolo…).
#
# Le backend « pipeline » (modèles classiques layout + OCR) tourne en CPU.
# Le backend « vlm » (modèle multimodal) n'est PAS installé : il exige un GPU.
#
FROM --platform=linux/amd64 python:3.12-slim

# Version de MinerU épinglée pour la reproductibilité (cf. README pour bumper).
ARG MINERU_VERSION=3.4.0

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Force l'inférence CPU (aucun GPU disponible dans Docker sur Mac).
    MINERU_DEVICE_MODE=cpu \
    # Source des modèles au 1er téléchargement (huggingface | modelscope | local).
    MINERU_MODEL_SOURCE=huggingface \
    # Caches modèles + config sur le volume monté (cf. compose.yaml) → persistants
    # entre redémarrages, donc téléchargés une seule fois.
    HF_HOME=/models/huggingface \
    MODELSCOPE_CACHE=/models/modelscope \
    MINERU_TOOLS_CONFIG_JSON=/models/mineru.json

# Dépendances système : OpenCV (libgl/libglib), OpenMP (libgomp), wget (healthcheck).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libgl1 \
      libglib2.0-0 \
      libgomp1 \
      wget \
 && rm -rf /var/lib/apt/lists/*

# MinerU + dépendances « core » = backend pipeline + serveur API + gradio, SANS
# les moteurs GPU (vllm/sglang/lmdeploy). Sur linux/amd64, pip installe la roue
# CPU de torch par défaut (les roues CUDA passent par un index dédié).
RUN pip install --upgrade pip \
 && pip install "mineru[core]==${MINERU_VERSION}"

# Répertoire de sortie par défaut de mineru-api (monté en volume dans compose).
WORKDIR /app
RUN mkdir -p /app/output /models

EXPOSE 8000

# Le serveur démarre vite ; les modèles se chargent à la 1re requête de parsing.
# IMPORTANT : le backend par défaut de l'API exige un GPU → les requêtes doivent
# passer backend=pipeline (géré côté mibeko-python, cf. README).
CMD ["mineru-api", "--host", "0.0.0.0", "--port", "8000"]
