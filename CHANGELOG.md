## Unreleased

### Feat

- **ingestion**: propager le nombre de pages aux actes découpés d'un JO
- **ingestion**: mesurer le nombre de pages des PDF source
- **extraction**: déséchapper les indices LaTeX et l'exposant zéro du numéro
- **extraction**: retenir la page de fin d'un article à cheval
- **documents**: répercuter le libellé descriptif des actes en abrégé
- **db**: implement soft deletes for structure_nodes and articles
- **parseur**: ajouter le support des dispositions implicites et notes distinctes
- **rattrapage**: proposer la normalisation des tableaux déjà en base
- **tableaux**: stocker les tableaux en texte pur et forme canonique
- **corpus**: script de purge physique du Code pénal RDC hors périmètre
- **prod**: proposer le nettoyage du mobilier de page déjà publié
- **extraction**: retirer le mobilier de page des Journaux officiels
- **corpus**: sourcer les actes uniformes OHADA, la loi 8/98 et la constitution
- **structuration**: classer les actes uniformes OHADA en STOCK
- **corpus**: etendre le carnet aux JO natifs 2005+ et a l'archive scannee avant 2005
- **structuration**: replier vers un article Texte integral si la hierarchie est vide
- **remediation**: retirer les 2 documents JO à plat avant push-corpus
- **remediation**: scinder les JO 1990-02 et 1959-23 en leurs actes réels
- **remediation**: corriger le rattachement croisé Ordonnance 019-84/Loi 076-84
- **remediation**: récupérer les articles 25-44 du Décret 59-178 (page pivotée)
- **remediation**: résoudre 46 signalements confirmés faux positifs
- **remediation**: détecter les documents dupliqués issus de la réingestion
- **remediation**: supprimer les 33 fantômes confirmés du bug title_regex
- **reingest**: supporter --target prod pour scripts/reingest_flat_journals.py

### Fix

- **documents**: rendre réversible la suppression d'un document
- **parseur**: cesser de couper les numéros d'articles sur le tiret de ponctuation
- **documents**: préserver les sources MinIO partagées
- **parseur**: corriger la détection des actes vides issus des sommaires
- **parseur**: recoller un titre d'article éclaté un mot par ligne par MinerU
- **parseur**: refuser d'ouvrir un acte au milieu d'une phrase
- **parseur**: réassembler les tableaux MinerU étalés sur plusieurs lignes
- **latex**: coller l'ordinal à son chiffre quand la base reste hors du $
- **corpus**: retirer du carnet pénal le PDF hors périmètre (RDC)
- **acquisition**: accepter les URL de téléchargement paramétrées
- **db**: résorber le drift documentaire modèles/.sql relevé par l'audit du 08/08
- **auth**: refuser les tokens des comptes soft-deleted
- **push-corpus**: voir les slugs soft-deleted de la cible dans le plan
- **ingestion**: retirer les échappements LaTeX de MinerU du texte ingéré
- **promotion**: ne pas écarter les actes d'un JO scindé partageant un même SHA-256
- **ingestion**: reconstituer les titres de JO coupés sur plusieurs lignes
- **promotion**: remapper institution_id et cibler le push par document_key
- **structuration**: purger les octets NUL du markdown avant insertion
- **parsing**: tolerer les PDF illisibles au triage plutot que d'interrompre le lot
- **extraction**: corriger la ligature fi/fl mal decomposee par certains PDF
- **structuration**: ne jamais marquer extraction_status=completed sans contenu produit
- **remediation**: re-structurer le Code du travail (76 articles vides)
- **structuration**: flusher les articles avant les flags de doublon
- **pipeline**: reconnaître les avis et les titres longs comme bornes d'acte
- **pipeline**: détecter les débuts d'acte en casse mixte sans titre markdown
- **remediation**: annuler le retrait à tort de 2 actes JO légitimes
- **remediation**: re-résoudre 3 flags régénérés par le correctif Ordonnance/Loi076
- **reingest**: bloquer la scission de documents plats JO acquis en double
- **parser**: étendre ARTICLE_PATTERN aux formats Code Pénal/CEMAC restants
- **structuration**: ne pas confondre une citation au pluriel avec un article
- **ingestion**: ne plus confondre une clause de clôture avec un nouvel acte
- **structuration**: fusionner les frontières d'article dégradées par l'OCR
- **ingestion**: reconnaître les séries de numérotation incrustées comme des annexes
- **ingestion**: flaguer chaque collision de numérotation au lieu de la renommer en silence
- **structuration**: scinder les Journaux officiels en actes distincts

### Refactor

- **structuration**: retirer le --force sans effet de structure-batch

## v0.1.0 (2026-09-01)

### Feat

- **structuration**: typer systématiquement les documents (type_code)
- **structuration**: identité des journaux officiels et correctifs du rechargement
- **data**: rapatrier le corpus data/ dans mibeko-python
- **promotion**: push additif du corpus validé vers la production
- **diagnostic**: profil de lecture seule vers la production
- **structuration**: découper les JO en actes via le parseur heuristique et Mistral
- **ocr**: ajouter un garde-fou de qualité OCR à la publication
- **acquisition**: découvrir les JO via l'autoindex sgg.cg
- **api**: add centralized config, secure endpoints, and feature flags
- resolve preamble loss and add curation flags
- **mineru**: add support for local self-hosted MinerU backend
- **extractor**: support du staging non-destructif et bornes d'actes
- **postgres schema**: add fuzzy search support
- add preamble parsing, soft deletes, backfill tooling
- **parser**: amélioration de l'extraction (tableaux, pages, JO)
- **api**: upload multi-fichiers et détection des anomalies de numérotation
- **extractor**: ajout des outils de fusion et de découpage de compilations
- add legal scope tracking, improve document API and parser
- **api**: add bearer auth and editor role checks for protected routes
- implement official journal upload and processing
- add app.mibeko.fr to CORS allowed origins
- **api**: add automatic static directory creation and git tracking
- add api v1 versioning, docker deployment and project setup
- **api**: add initial REST API for document management
- add fastapi web api and real-time dashboard
- add cli tool and fix duplicate article crash
- add hierarchy parser and improve nlp loading stability
- add mineru ocr and minio s3 storage services
- add sqlalchemy database models and connection engine
- add database schema and python dependencies

### Fix

- **db**: ne pas crier au loup à chaque démarrage en production
- **structuration**: joindre les autorites cosignataires en une seule chaine
- **structuration**: televerser PDF/markdown/JSON vers MinIO comme le flux manuel
- **demarrage**: rattraper les traitements orphelins et détecter le drift de schéma
- **stockage**: purger l'objet MinIO à la suppression d'un document
- **upload**: streamer les fichiers vers disque et plafonner leur taille
- **api**: restreindre le CORS et ne plus exposer les erreurs internes
- **config**: refuser les identifiants par défaut en production
- **auth**: valider strictement le token_id Sanctum
- **parsing**: add fallback parsing for empty hierarchies
- **api**: éviter le 500 sur upload (stock_code trop long) + CORS sur erreurs
- add sse ping and headers to avoid timeouts
