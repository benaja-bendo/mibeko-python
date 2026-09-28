# Registre des décisions — service Python (ingestion)

> Statut : à jour au 28 septembre 2026 · **Fait autorité sur** : les décisions en vigueur qui ne changent que le code de ce dépôt. Les décisions qui touchent plusieurs dépôts (sources, périmètre du corpus, tableaux, LaTeX, historique des textes…) sont dans le registre transverse (`docs/decisions.md` du monorepo, dépôt `mibeko-docs`), qui donne aussi le gabarit et les règles (D-001).

Identifiants `PY-NNN`, jamais réutilisés ; une nouvelle décision s'ajoute à la fin. Les décisions reprises le 28/09/2026 ne portent « Écarté » et « On rouvre si » que si l'original les donnait ; texte d'origine : `docs/_archive/2026-09-28-journal-decisions-2026-07-a-09.md` (dépôt `mibeko-docs`).

### PY-001 · 2026-08-02 · Un Journal officiel est scindé en actes distincts dès l'ingestion
**Statut** : en vigueur

**Décision** : `structure-batch` passe par le découpeur en actes, celui de l'upload manuel, à partir du `.md` MinerU et jamais du `.json`.
**Contexte** : un document plat par JO avait produit 5 525 renumérotations silencieuses sur 62 documents (audit du 02/08).

### PY-002 · 2026-08-02 · La référence détectée d'un acte de JO scindé va dans `metadata`, pas dans `reference_nor`
**Statut** : en vigueur

**Décision** : un même numéro d'acte revient dans des JO différents, ce qui viole la contrainte `UNIQUE` de la colonne. La référence reste conservée en `metadata`.

### PY-003 · 2026-08-09 · Le filtre du mobilier de page exige une ancre stricte et vit dans le parseur
**Statut** : en vigueur · **Réf.** : mibeko-python#5

**Décision** : le filtre est dans `LegalDocumentParser.extract_text` (`src/extractor/page_furniture.py`), seul point de passage des appelants. Une ligne n'est retirée que si elle appartient au vocabulaire du mobilier ET à un bloc ancré par un bandeau strict (rien après « du Congo »). Le même `strip_page_furniture` sert à la prévention et à la remédiation.
**Écarté** : un motif seul. Un nombre seul sur sa ligne est parfois un numéro d'arrêté, et « Journal officiel de la République du Congo et communiqué… » est une formule légitime.

### PY-004 · 2026-08-31 · `DELETE /api/v1/documents/{id}` retire, il ne purge pas
**Statut** : en vigueur · **Réf.** : mibeko-python#3

**Décision** : la route pose `deleted_at` sur le document et ses articles vivants, ne touche jamais MinIO, et `POST …/restore` ne restaure que ce qu'elle a retiré. Une purge exceptionnelle serait un outil distinct, jamais exposé par l'API générique.

### PY-005 · 2026-08-08 · L'authentification Sanctum filtre `deleted_at`, mais pas `suspended_at`
**Statut** : en vigueur

**Décision** : parité stricte avec Laravel, où suspendre un compte révoque ses jetons, et c'est le seul mécanisme.
**On rouvre si** : la suspension doit bloquer à chaque requête. La décision se prend alors d'abord côté Laravel.

### PY-006 · 2026-08-08 · Une collision de slug avec un document supprimé se gère dans le plan de `push-corpus`
**Statut** : en vigueur

**Décision** : le plan lit tous les slugs de la cible, soft-deleted compris, et annonce la collision dès le dry-run.
**Écarté** : rendre l'index `legal_documents_slug_unique` partiel (un slug supprimé deviendrait réutilisable : conflit à la restauration, URL publiques ambiguës).

### PY-007 · 2026-09-13 · La veille périodique découvre les nouveaux JO et écrit des brouillons directement en production
**Statut** : en vigueur · **Réf.** : mibeko-python#21, `docs/pipeline/runbook.md` (monorepo) § 3.8

**Décision** :
- périmètre : les nouveaux Journaux officiels seulement ;
- cadence : quotidienne ;
- emplacement : un conteneur `veille` dans ce dépôt ;
- alerte : un ping de surveillance (`VEILLE_HEALTHCHECK_URL`) ;
- écriture : des `draft` directement en production, comme l'upload manuel. « Staging ≠ publié » reste le garde-fou.

**Écarté** : l'installer dans `vps_infra` (hors périmètre) ; un endpoint appelé par le scheduler Laravel (canal fragile pour un traitement long).

### PY-008 · 2026-09-14 · OCR de production : Mistral OCR, en version épinglée
**Statut** : en vigueur · **Réf.** : `docs/pipeline/plan-boite-de-reception-2026-09.md` (monorepo)

**Décision** : modèle épinglé, jamais `-latest`. L'artefact est conservé dans `pipeline/md/`, avec une clé `MISTRAL_API_KEY` dédiée à l'OCR pour lire les coûts à part. Le durcissement du triage fait partie du même lot.
**Écarté** : renouveler la clé cloud MinerU (tarif non vérifié, triage non corrigé) ; MinerU local sur un VPS agrandi (au moins 6 Go de RAM).

### PY-009 · 2026-09-03 · `ruff` et `pyproject.toml`/`uv` sont acceptés sous conditions ; `sphinx` reste écarté
**Statut** : en vigueur, non appliquée · **Réf.** : mibeko-python#16

**Décision** : le nettoyage se fait module par module, jamais par un `--fix` global. Les faux positifs `B008` de FastAPI sont neutralisés par configuration. On liste explicitement les règles (`select`). Il ne doit y avoir qu'une source de vérité pour les dépendances, que le `Dockerfile` doit réellement utiliser, et `.python-version` doit correspondre à l'image.
**Conséquences** : la PR #16 a été fermée sans fusion ; rien n'est intégré à ce jour.
