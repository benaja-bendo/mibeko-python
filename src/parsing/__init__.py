"""Étage 2 de l'usine à textes : parsing en lot (triage natif → MinerU).

Sous-modules :
- triage : décide, PDF par PDF, si le texte natif (PyMuPDF, sans OCR) suffit
           ou s'il faut passer par MinerU.
- batch   : orchestration pilotée par le manifeste (data/manifests/) —
           idempotente, reprend après interruption, écrit data/pipeline/.
"""
