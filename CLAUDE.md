# CLAUDE.md — mibeko-python

## Contexte
Service d'ingestion du corpus juridique Mibeko — **Congo-Brazzaville (jamais la RDC)** + OHADA : une API FastAPI (`python.mibeko.fr`) et une CLI Click (`main.py`). **Brique interne, jamais un site** : consommée par le backend Laravel (`mibeko-tableau-de-bord`) et le front éditeur (`mibeko-front`, espace `/editor`) via `/api/v1/*`, authentifiée par token Sanctum lu directement en base (`src/api/auth.py`). Un des 7 dépôts du monorepo — le `CLAUDE.md` à la racine fait foi sur la carte complète, les ports et les règles transverses.

Chaîne : PDF source → triage natif PyMuPDF ou OCR MinerU → parseur heuristique + Mistral (métadonnées d'en-tête **uniquement**) → PostgreSQL **en staging**.

## Invariants non négociables

1. **Le schéma DB est piloté uniquement par les migrations Laravel.** `init_db()` (`src/db/database.py`) est un no-op délibéré ; `src/db/models.py` et `schema_postgres.sql` (référence documentaire, jamais rejouée) se synchronisent **à la main** → vérifier le drift à chaque évolution du schéma côté Laravel.
   - Le détecteur `src/db/schema_check.py` (exécuté au démarrage, résultat dans `GET /api/v1/health` → `schema_ok` / `schema_issues`) ne voit qu'**un seul sens de l'écart, par construction** : colonne attendue par un modèle mais absente en base, ou nullabilité divergente. Il ne signale **ni** les colonnes présentes en base et inconnues du modèle, **ni** les types/longueurs. Il ne bloque jamais le démarrage.
   - Nullabilités des modèles alignées sur la base et `.sql` resynchronisé le 08/08/2026 (`schema_ok=true`). Restent deux angles morts **assumés**, à ne pas « corriger » sans arbitrage : `CurationFlag` ignore les colonnes de détection ajoutées côté Laravel (`node_id`, `suggestion`, `anchor`, `confidence`, `run_id`, `resolved_at`, `resolved_by`) — un flag écrit depuis Python les laisse à NULL (cf. commentaire dans `flag_low_ocr_quality`) ; et `ArticleVersion` ne mappe ni `created_at` ni `updated_at` (défauts DB à l'insertion — sans conséquence tant que Python ne fait qu'insérer/supprimer des versions, jamais d'UPDATE en place ; si un UPDATE apparaît un jour, `updated_at` ne bougera pas).
2. **Toute lecture de `legal_documents` / `articles` filtre `deleted_at IS NULL`** (SoftDeletes Laravel), sinon des documents supprimés remontent silencieusement.
3. **Un Journal officiel se découpe à partir du `.md` MinerU, jamais du `.json`** — le JSON produit de faux actes. `create_or_update_jo_documents_from_markdown` n'utilise le JSON que pour tamponner les pages (`[[MIBEKO_PAGE:N]]`, citabilité) ; le texte reconstruit depuis le JSON n'est qu'un repli quand aucun `.md` n'existe.
4. **`data/sources/` est immuable.** Les manifestes JSONL de `data/manifests/` (URL, date de récupération, SHA-256) sont la seule trace **non régénérable** ; tout `data/pipeline/` se recalcule. Contrat détaillé : `data/README.md`.
5. **Le pipeline écrit en staging** (`draft` / `review`). Seul `published` est public, et **la publication passe par l'API Laravel**, jamais par un `UPDATE curation_status` en SQL direct. L'upload JO refuse d'ailleurs `published` (422) pour ne pas contourner le garde-fou d'anomalies.
6. **Production : lecture seule.** Diagnostic via `src/db/prod_readonly.py` + `python main.py prod-preflight` (tunnel SSH, ports 5434/9100 volontairement distincts du dev 5433/9000 ; le module refuse de démarrer si la cible « prod » est en fait le dev). Toute écriture exige une autorisation humaine, précédée d'un dump frais et livrée sous forme rejouable — **par opération** pour tout ce qui touche un document publié ou la publication (Classe 2), **par lot dans le terminal humain** pour la curation de staging (Classe 1, file d'opérations — `docs/infra/production.md` § 6, classes du 08/08/2026). `push-corpus` et `backfill-type-codes` sont en dry-run par défaut et exigent de taper `PRODUCTION`. Ne jamais basculer le `.env` vers la prod.
7. **Le LLM est un composant d'étape** : Mistral ne produit que les métadonnées d'en-tête, validées par `src/structuration/schema.py` avant toute écriture ; échec de validation = erreur tracée et revue humaine, jamais d'insertion partielle.

## Commandes

```bash
source venv/bin/activate            # pas de pyproject : requirements.txt + venv local
python main.py --help               # liste faisant foi des sous-commandes
python main.py serve --port 8001    # ⚠️ le défaut CLI est 8000 — voir Pièges
pytest                              # suite complète, contre la base Postgres de dev réelle
```

Sous-commandes par étage de l'usine à textes (idempotentes, la plupart avec `--dry-run`) :

| Étage | Commandes |
| --- | --- |
| Acquisition | `acquire`, `backfill-manifest`, `ohada-recon` |
| Parsing (triage natif → MinerU) | `process-batch` |
| Structuration | `structure-batch`, `link-journals`, `backfill-type-codes` |
| Découpage manuel de compilations | `merge-chunks`, `suggest-boundaries[-md]`, `split-compilation[-md]` |
| Production | `prod-preflight`, `push-corpus`, `proposer-nettoyage-masthead` (lecture seule : produit un mapping pour `php artisan mibeko:corriger-contenu-article`) |

Aucun linter ni formateur n'est configuré dans ce dépôt (ni ruff, ni black, ni mypy) — ne pas en introduire un sans arbitrage.

## Pièges connus

- **Port 8001, pas 8000.** Le défaut de `serve` est 8000, mais 8000 est pris par Laravel et le proxy Vite du front route `/py` → `localhost:8001` (`mibeko-front/vite.config.ts`). Toujours `--port 8001` en local. Le `README.md` de ce dépôt affirme encore « le service écoute sur le port 8000 » : c'est vrai pour le Dockerfile/prod, pas pour le dev.
- **Un 500 s'affiche comme une « erreur CORS » dans le navigateur.** Une exception non gérée est traitée par `ServerErrorMiddleware`, qui court-circuite le middleware CORS. Un `@app.exception_handler(Exception)` global (`src/api/main.py`) ré-attache l'en-tête et logge la traceback côté serveur. Si un agent voit « CORS », lire les logs du service — pas la configuration CORS.
- **Aucune migration au démarrage, aucune file durable.** `on_startup` = `init_db()` (no-op) + `reap_orphaned_runs()` (bascule en `failed` runs, JO et documents restés `running`/`queued`/`processing` après un redémarrage) + `run_schema_check()`. Le pipeline repose sur `BackgroundTasks` en mémoire : un parsing en cours est perdu au redémarrage → relancer via `/reprocess` ou `/parse`.
- **Importer `src.services.minio_service` ouvre une connexion MinIO** : le singleton `minio_service = MinioService()` en fin de module appelle `make_bucket` dès l'import. `src.api.main` l'importe, et `src/structuration/structurer.py` importe `src.api.main` — donc toute commande de structuration touche MinIO à l'import. C'est précisément pourquoi `prod_readonly.py` n'importe ni `src.db.database` ni `minio_service` : ne pas casser cette autonomie.
- **`DELETE /api/v1/documents/{id}` est une suppression PHYSIQUE**, pas un soft-delete : articles, versions, nœuds, runs et `media_files` supprimés en base, puis objets MinIO purgés (best-effort, après commit). Irréversible.
- **Le seuil `pg_trgm.strict_word_similarity_threshold = 0.35` se perd à chaque restauration de dump.** Posé par `ALTER DATABASE … SET` (déclaré dans `schema_postgres.sql`), que `pg_dump` n'emporte pas : une base dev restaurée retombe silencieusement à 0.5, et l'opérateur `%>>` (filet flou de la recherche) écarte alors des variantes attendues. Laravel compense par session (`SearchesArticles.php`, `set_config`), mais toute session Python ou psql travaille au seuil de la base. Posé en prod (vérifié en lecture seule le 08/08/2026) ; en dev, le reposer après chaque restauration : `ALTER DATABASE "mibeko-db" SET pg_trgm.strict_word_similarity_threshold = 0.35;`
- **La suite de tests écrit dans la base réelle.** `tests/conftest.py` refuse de collecter si `DB_HOST`/`DB_PORT` ne valent pas `127.0.0.1:5433` (dérogation : `MIBEKO_ALLOW_NON_DEV_DB=1`). Ne pas contourner ce garde-fou : lancée contre un tunnel de prod, la suite corromprait la production.
- **MinerU local** : harnais Docker CPU dans `mineru-local/` (lent sur Mac, émulation `linux/amd64`). Port hôte conseillé `MINERU_PORT=8004` (8000 = Laravel), puis `MINERU_BACKEND=local` + `MINERU_API_URL=http://localhost:8004` — **sans** `/api/v4`. ⚠️ `.env.example` et `compose.yaml` montrent encore 8000 par défaut ; `mineru-local/README.md` fait foi.
- **`MIBEKO_DATA_DIR` relatif se résout depuis le cwd du shell**, pas depuis `mibeko-python/`. Lancer toutes les commandes depuis `mibeko-python/`, ou mettre un chemin absolu.
- **`structure-batch` n'a pas d'option de retraitement forcé, et ce n'est pas un oubli** : l'idempotence tient à `document_key`, qu'un « force » ne pourrait contourner sans créer un second document pour le même texte. Retraiter un document déjà structuré relève donc de la curation, pas d'un drapeau. Le flag `--force` a existé jusqu'au 25/08/2026 — accepté, transmis, ignoré (`mibeko-python#4`) ; il est désormais rejeté par la CLI. Celui de `process-batch`, lui, agit bien (il court-circuite le test de SHA source).
- En `APP_ENV=production` (le **défaut** quand la variable est absente), `DB_USERNAME`/`DB_PASSWORD`/`MINIO_*` sans valeur font échouer le démarrage : plus aucun repli `root/root` silencieux.

## Renvois (ne pas dupliquer ici)

- Usine à textes : `docs/pipeline/README.md` (pourquoi et quoi) et `docs/pipeline/runbook.md` (commandes, parcours, dépannage) — dépôt `docs/`.
- Corpus local : `data/README.md` — contrat du dossier `data/`, fait foi sur ce à quoi le code tient vraiment.
- Production : `docs/infra/production.md` (dépôt `docs/`). ⚠️ plusieurs fichiers renvoient encore à `docs/infra/production.md`, renommé depuis.
- Ce dépôt : `README.md`, `docs/architecture.md`, `docs/API.md`.

## Conventions de travail

- Commits en français, format `type(scope): titre court` à l'impératif ou au substantif ; le corps explique le **POURQUOI** (problème résolu, compromis), jamais la liste des fichiers touchés. Petits commits atomiques, un sujet cohérent par commit. **Aucune mention d'agent IA dans les commits** (pas de trailer `Co-Authored-By`) — retiré le 07/08/2026 (`docs/decisions.md`).
- **Jamais de commit, push ou tag sans l'accord explicite de l'utilisateur** : proposer les commandes, s'arrêter, attendre la réponse.
- Toute décision structurante = une ligne datée dans `docs/decisions.md` (dépôt `docs/`, transverse aux 7 dépôts).
- Docs-as-code : tout fichier de `docs/` commence par `# Titre` + `> Statut : à jour au <date> · <portée>`.
- Avant de corriger un constat d'audit, le vérifier contre le code actuel — les références fichier:ligne bougent vite sur ce projet.
