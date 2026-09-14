# Hiérarchie des fichiers

```markdown
Directory structure:
└── benaja-bendo-mibeko-python/
    ├── README.md
    ├── CLAUDE.md
    ├── Dockerfile
    ├── requirements.txt
    ├── schema_postgres.sql
    ├── .dockerignore
    ├── .env.example
    ├── data/
    │   ├── README.md
    │   ├── corpus/
    │   │   ├── README.md
    │   │   └── corpus-v1.yaml
    │   ├── manifests/
    │   │   ├── README.md
    │   │   ├── avocat-alban.jsonl
    │   │   ├── code-penal.jsonl
    │   │   ├── divers.jsonl
    │   │   ├── ohada-actes.jsonl
    │   │   ├── sgg-autres.jsonl
    │   │   ├── sgg-codes.jsonl
    │   │   ├── sgg-decrets.jsonl
    │   │   └── sgg-lois.jsonl
    │   ├── pipeline/
    │   │   ├── README.md
    │   │   ├── actes/
    │   │   │   ├── code-penal-1836/
    │   │   │   │   └── README.md
    │   │   │   └── jo-ohada-2017/
    │   │   │       └── README.md
    │   │   ├── json/
    │   │   │   └── .keep
    │   │   ├── md/
    │   │   │   └── .keep
    │   │   └── metrics/
    │   │       └── .keep
    │   └── sources/
    │       ├── README.md
    │       └── .keep
    ├── docs/
    │   ├── README.md
    │   ├── API.md
    │   └── architecture.md
    ├── mineru-local/
    │   ├── README.md
    │   ├── compose.yaml
    │   ├── Dockerfile
    │   └── .env.example
    ├── scripts/
    │   ├── audit_preamble_backfill.py
    │   ├── backfill_doublon_flags.py
    │   ├── backfill_preamble.py
    │   ├── build_arrete_3277_repair.py
    │   ├── delete_title_regex_phantoms.py
    │   ├── detect_document_duplicates.py
    │   ├── fix_decret_59178_page_pivotee.py
    │   ├── fix_ordonnance019_loi076_rattachement.py
    │   ├── purge_document_hors_perimetre_rdc.py
    │   ├── reclassify_embedded_series_flags.py
    │   ├── reingest_flat_journals.py
    │   ├── resolve_false_positive_flags.py
    │   ├── restore_wrongly_retired_jo_acts.py
    │   ├── restructure_stock_codes.py
    │   ├── restructurer_code_travail.py
    │   ├── retire_flat_jo_documents.py
    │   └── split_jo_1990_02_et_1959_23.py
    ├── src/
    │   ├── acquisition/
    │   │   ├── __init__.py
    │   │   ├── acquire.py
    │   │   ├── backfill.py
    │   │   ├── config.py
    │   │   ├── manifest.py
    │   │   ├── ohada.py
    │   │   ├── politeness.py
    │   │   └── sgg.py
    │   ├── api/
    │   │   ├── auth.py
    │   │   ├── config.py
    │   │   ├── schemas.py
    │   │   ├── upload_utils.py
    │   │   ├── routers/
    │   │   │   ├── __init__.py
    │   │   │   └── documents.py
    │   │   ├── static/
    │   │   │   └── .gitkeep
    │   │   └── templates/
    │   │       ├── index.html
    │   │       └── status.html
    │   ├── db/
    │   │   ├── database.py
    │   │   ├── models.py
    │   │   ├── prod_readonly.py
    │   │   └── schema_check.py
    │   ├── extractor/
    │   │   ├── chunk_merger.py
    │   │   ├── compilation_splitter.py
    │   │   ├── latex_artifacts.py
    │   │   ├── page_furniture.py
    │   │   ├── parser.py
    │   │   ├── tables.py
    │   │   └── text_quality.py
    │   ├── parsing/
    │   │   ├── __init__.py
    │   │   ├── batch.py
    │   │   └── triage.py
    │   ├── promotion/
    │   │   ├── __init__.py
    │   │   └── push_corpus.py
    │   ├── services/
    │   │   ├── mineru_service.py
    │   │   ├── minio_service.py
    │   │   ├── mistral_service.py
    │   │   └── pdf_pages.py
    │   └── structuration/
    │       ├── __init__.py
    │       ├── batch.py
    │       ├── journals.py
    │       ├── schema.py
    │       ├── structurer.py
    │       └── typage.py
    ├── tests/
    │   ├── conftest.py
    │   ├── test_acquisition_acquire.py
    │   ├── test_acquisition_backfill.py
    │   ├── test_acquisition_manifest.py
    │   ├── test_acquisition_politeness.py
    │   ├── test_anomaly_detection.py
    │   ├── test_article_numero_composes.py
    │   ├── test_auth_soft_delete.py
    │   ├── test_auth_token_id.py
    │   ├── test_body_size_limit.py
    │   ├── test_cli_structure_batch_force.py
    │   ├── test_compilation_splitter.py
    │   ├── test_document_deletion_shared_media.py
    │   ├── test_ingest_hierarchy_doublon_flags.py
    │   ├── test_ingest_order.py
    │   ├── test_ingest_tables.py
    │   ├── test_latex_artifacts.py
    │   ├── test_ocr_quality.py
    │   ├── test_ordinal_sub_numbering.py
    │   ├── test_page_annotation.py
    │   ├── test_page_furniture.py
    │   ├── test_parser_article_boundary_ocr.py
    │   ├── test_parser_implicit_dispositions.py
    │   ├── test_parser_preamble.py
    │   ├── test_parser_split_headings.py
    │   ├── test_parser_tables.py
    │   ├── test_parsing_batch.py
    │   ├── test_prod_readonly.py
    │   ├── test_push_corpus.py
    │   ├── test_push_corpus_etat_cible.py
    │   ├── test_reaper.py
    │   ├── test_reingest_flat_journals.py
    │   ├── test_schema_check.py
    │   ├── test_sgg_autoindex.py
    │   ├── test_sgg_grammar.py
    │   ├── test_split_official_journal_markdown.py
    │   ├── test_structuration_batch.py
    │   ├── test_structuration_journals_split.py
    │   ├── test_structuration_mistral_service.py
    │   ├── test_structuration_schema.py
    │   ├── test_structuration_structurer.py
    │   ├── test_tables.py
    │   ├── test_triage.py
    │   ├── test_typage.py
    │   └── test_upload_security.py
    ├── .deploy/
    │   └── docker-compose.yml
    └── .github/
        └── workflows/
            ├── cleanup-ghcr.yml
            └── deploy-prod.yml
```
