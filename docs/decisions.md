# Registre des décisions — service Python (ingestion)

> Statut : à jour au 7 octobre 2026 · **Fait autorité sur** : les décisions en vigueur qui ne changent que le code de ce dépôt. Les décisions qui touchent plusieurs dépôts (sources, périmètre du corpus, tableaux, LaTeX, historique des textes…) sont dans le registre transverse (`docs/decisions.md` du monorepo, dépôt `mibeko-docs`), qui donne aussi le gabarit et les règles (D-001).

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

### PY-010 · 2026-10-01 · Une fiche de JO créée par le pipeline naît publiée ; le push ne crée que celles de ses documents
**Statut** : en vigueur · **Réf.** : mibeko-python#38, D-056, dashboard#218

**Contexte** : le push du 07/08/2026 (`--limit 100`) a créé les 39 fiches de JO du plan entier pour 100 documents, et 24 numéros sont restés publiés sans texte (mesure du 30/09). `ensure_official_journal` crée les fiches `is_published=True`, en dev (puis poussées) comme en production (veille, PY-007). L'upload manuel de l'API, lui, les crée non publiées.
**Décision** : `--limit` coupe aussi les journaux (`limiter_plan`) : une fiche n'arrive qu'avec au moins un de ses textes. La fiche reste créée publiée : D-056 fait d'un numéro publié un PDF officiel lisible, et ce PDF est le PDF source du document qui l'accompagne.
**Écarté** : faire naître la fiche non publiée, comme les documents en `draft`. Cela ajoutait une publication manuelle par numéro sans protéger le lecteur, puisque le PDF officiel est public par nature.
**Conséquences** : un numéro peut apparaître au kiosque avant que ses textes soient publiés ; il s'affiche alors « Texte intégral (PDF) » (front#60).
**On rouvre si** : une fiche poussée ou créée par la veille est publiée sans que son PDF soit servi (404), ou annonce un numéro que son PDF ne porte pas.

### PY-011 · 2026-10-02 · `push-corpus --execute` exige `--attendu N` et refuse tout plan qui n'a pas ce nombre de documents
**Statut** : en vigueur · **Réf.** : mibeko-python#39, `docs/pipeline/runbook.md` (monorepo) § 3.7

**Contexte** : le 01/10/2026, une liste de `--document-key` construite par `$(sed …)` sur un fichier absent est restée vide, et le push a tourné sans filtre sur toute la base de dev : 24 documents et 5 260 lignes au lieu des 23 et 3 379 annoncés. Le Code des assurances CIMA, un brouillon hors plan, est arrivé en production. La simulation affichait « À pousser : 24 » ; le seul garde-fou était la lecture du résumé avant de taper `PRODUCTION`.
**Décision** :
- `--attendu N` compare N au nombre de documents du plan limité (`limiter_plan`), celui que `executer_push` déroulera. Un écart est refusé (code 1) avant toute écriture, simulation comprise, avec les clés en trop ou manquantes si `--document-key` est donné, ou la liste du plan sinon.
- `--execute` sans `--attendu` est refusé avant toute connexion. En simulation l'option reste facultative : elle sert à découvrir le nombre la première fois.
- N s'écrit dans l'annonce de l'opération avant la simulation, jamais recopié depuis elle ; la même valeur figure dans la simulation et dans l'exécution.
**Écarté** : refuser `--execute` sans `--document-key` ni `--limit`. Cela ne protège ni d'une liste fausse mais non vide, ni d'un `--limit` mal calibré, et interdit la promotion complète légitime sauf à passer un `--limit` géant, qui apprend le contournement. Un seul invariant, le compte, couvre les trois cas.
**Conséquences** : une commande d'exécution porte un argument de plus ; une annonce fausse bloque l'opération, et on corrige l'annonce ou le plan, jamais l'option sans comprendre. Si N est recopié de la simulation, le garde-fou ne vaut rien.
**On rouvre si** : un écart passe malgré `--attendu` (même nombre, mauvais documents) : il faudrait alors comparer les clés et non leur seul nombre ; ou si un plan légitime bouge entre la simulation et l'exécution au point de bloquer l'opération.

### PY-012 · 2026-10-07 · La veille ne redépose plus une entrée dont le dernier échec est définitif et le fichier inchangé
**Statut** : en vigueur · **Réf.** : mibeko-python#49

**Contexte** : la veille redépose toute entrée du manifeste au statut `erreur`. Pour `congo-jo-2026-17` (source vide, 0 octet, échec `definitive`), cela fabriquait un job `failed` par nuit : 5 du 02 au 06/10/2026, après 14 jobs `transitoire` du 17/09 au 01/10 (mesure en production, lecture seule, le 07/10). Le mail de résultat prévu par #49 aurait annoncé le même échec chaque nuit. L'acquisition ne retélécharge jamais une entrée déjà au manifeste (`déjà au manifeste`) : un fichier source corrigé à la même URL n'est donc pas repris par la veille.
**Décision** : `_deposer_jobs_veille` ne redépose pas une entrée si elle est au statut `erreur`, si son dernier job (toutes sortes confondues) est `failed` en `definitive`, et si son SHA-256 est celui de la provenance du premier dépôt. Elle la liste dans `echecs_definitifs_ignores` du rapport, donc dans le journal de la nuit. Les échecs `transitoire` et `information_manquante`, une entrée sans provenance et une entrée remise à la main à `telecharge` restent éligibles. Aucune écriture de manifeste, aucune migration.
**Écarté** : un statut de manifeste dédié (par exemple `echec_definitif`). Le manifeste est la seule trace non régénérable du corpus : il décrit ce qui a été acquis, pas l'historique des traitements, et l'étendre obligeait à toucher le worker et tous les lecteurs du statut.
**Conséquences** : le geste humain pour forcer un nouveau traitement reste de remettre l'entrée à `telecharge`, ou de déposer une reprise. Une entrée `information_manquante` est toujours redéposée chaque nuit ; si elle devient du bruit, la même règle s'étend à cette classe. Si un fichier est re-sourcé à la main sous la même entrée puis échoue de nouveau, la provenance garde l'ancien SHA et la veille le redépose chaque nuit : la provenance n'est pas mise à jour au redépôt.
**On rouvre si** : une entrée en échec définitif devrait être retentée sans que son SHA ait changé (cause externe corrigée, par exemple une clé de service), ou si le re-sourçage manuel devient fréquent au point de justifier la mise à jour de la provenance au redépôt.
