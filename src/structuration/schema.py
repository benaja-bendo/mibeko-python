"""Schéma pydantic strict de la sortie LLM (étage 3 de l'usine à textes).

Sortie Mistral validée par ce schéma avant toute insertion en base (règle
mission : jamais d'insertion partielle). Zones calquées sur les feuilles
PREAMBULE/SIGNATURE/TABLEAU/DISPOSITION/NOTE du parser heuristique
(src/extractor/parser.py).
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class ArticleExtrait(BaseModel):
    numero: str
    contenu: str
    page: Optional[int] = None


class TexteJuridique(BaseModel):
    nature: str  # ex. "Loi", "Décret", "Ordonnance", "Code", "Arrêté"
    numero: Optional[str] = None
    date_signature: Optional[str] = None
    date_publication: Optional[str] = None
    autorite: Optional[str] = None
    visas: List[str] = Field(default_factory=list)
    preambule: Optional[str] = None
    articles: List[ArticleExtrait]
    annexes: List[str] = Field(default_factory=list)
    tableaux: List[str] = Field(default_factory=list)
    signature: Optional[str] = None


def validate_texte_juridique(data: dict) -> TexteJuridique:
    return TexteJuridique.model_validate(data)


__all__ = ["ArticleExtrait", "TexteJuridique", "validate_texte_juridique"]
