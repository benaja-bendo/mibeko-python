"""Worker de la file d'ingestion durable (mibeko-python#23, § 3.3-3.4 du plan
« boîte de réception » — docs/pipeline/plan-boite-de-reception-2026-09.md).

Réserve un `IngestionJob` par `SELECT … FOR UPDATE SKIP LOCKED`, avec bail
(`locked_at`/`locked_by`) et jeton de tentative (`fencing_token`) : un worker
dont le bail a expiré ne peut plus écrire après qu'un autre l'a repris, vérifié
juste avant l'écriture finale, pas seulement à la réservation.
"""
