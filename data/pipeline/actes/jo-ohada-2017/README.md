# Découpage du JO OHADA spécial du 15 décembre 2017 — 08/08/2026

> Régénérable : `split-compilation-md` avec `boundaries.json` sur
> `data/pipeline/md/ohada-actes/jo-ohada-2017-arbitrage-mediation.md`
> reproduit ces fichiers à l'identique.

Contexte : mibeko-python#8 (sourcing OHADA) et mibeko-dashboard#20 (remplacement
du « Code Bleu Ohada »). Ce PDF unique contient **trois textes distincts**, tous
adoptés le 23 novembre 2017 à Conakry — l'ingérer d'un bloc recréerait un
recueil à numérotations multiples, exactement le défaut qu'on corrige.

| Fichier | Texte | Articles | Type |
| --- | --- | --- | --- |
| `acte_01_au-m-diation-2017.md` | Acte uniforme relatif à la médiation | 18 | AU |
| `acte_02_au-arbitrage-2017.md` | Acte uniforme relatif au droit de l'arbitrage | 36 (« Article premier » → 36) | AU |
| `acte_03_r-glement-d-arbitrage-ccja-2017.md` | Règlement d'arbitrage de la CCJA | 41 | TEXTE |

Les comptes correspondent à ceux relevés indépendamment sur le PDF lors de la
vérification du sourcing (18 et 36). L'article 36 de l'arbitrage — celui qui
abroge l'Acte uniforme du 11 mars 1999 — est bien présent dans le morceau 2.

La couverture du JO et son sommaire (lignes 1-32 du markdown fusionné) sont
volontairement exclus : `slice_markdown_by_boundaries` ignore tout ce qui
précède la première borne.

## Deux points pour la revue éditeur

1. **Le règlement CCJA n'est pas un Acte uniforme** : c'est un règlement de
   procédure, typé `TEXTE` ici. À publier ou non selon la ligne éditoriale — il
   est officiel et utile aux praticiens de l'arbitrage, mais il ne fait pas
   partie des dix Actes uniformes.
2. **Les bornes ont été posées à la main.** `suggest-boundaries-md` a renvoyé
   **zéro** borne sur ce recueil : son jeu de motifs (`^LOI N°`, `^ORDONNANCE N°`,
   `^DÉCRET N°`, `^ARRÊTÉ N°`, `^CODE …`, `^AVIS N°`) **ne contient pas
   « ACTE UNIFORME »**, alors que son homologue côté JSON
   (`suggest_compilation_boundaries`) le gère explicitement. Comme l'invariant du
   projet impose de découper depuis le `.md` et jamais depuis le `.json`, c'est
   le chemin utilisé qui est le moins outillé. Signalé sur mibeko-python#7.
   Attention aussi au faux positif : « ACTE UNIFORME RELATIF AU DROIT DE
   L'ARBITRAGE » réapparaît en titre courant au milieu du document (ligne 578 du
   markdown fusionné) sans être un début d'acte.
