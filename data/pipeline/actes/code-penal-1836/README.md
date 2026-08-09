# Découpage du recueil Penal-Code-1836.pdf — 08/08/2026

> Régénérable : refaire `split-compilation-md` avec `boundaries.json` sur
> `data/pipeline/md/Penal-Code-1836.md` (méthode `mineru_local`, cf. metrics)
> reproduit ces fichiers à l'identique.

Contexte : mibeko-dashboard#20 (dépublication+segmentation des 2 compilations),
détail de l'analyse dans le commentaire du 08/08/2026 sur ce ticket.

- `acte_01_code-penal.md` — le vrai Code pénal (L119-2939 du markdown MinerU),
  "DISPOSITIONS PRELIMINAIRES" → "FIN DU CODE PENAL" + notes de bas de page.
  Prêt pour /editor/ingestion (type CODE). 5 lacunes résiduelles (pages
  illisibles rendues en image par MinerU, non du texte) aux lignes 475, 1309,
  1659, 2802 et 2820 de ce fichier — à vérifier en revue éditeur, pas
  bloquant pour la structure d'ensemble.
- `acte_03_ordonnance-62-6.md` — Ordonnance n° 62-6 du 28/07/1962 (interdiction
  des marquages ethniques), complète (3 articles + signature Fulbert Youlou).
  Prêt pour /editor/ingestion (type ORD).
- `acte_02_exclu.md` — **à NE PAS publier.** Fragment endommagé (article 13
  coupé + 14-16 seulement, page de titre perdue) identifié comme la Loi n°
  8/98 du 31/10/1998 (génocide, crimes de guerre, crimes contre l'humanité).
  Conservé ici pour traçabilité de la décision d'exclusion uniquement. Le
  texte complet et officiel de cette loi a été acquis séparément :
  `data/sources/sgg/lois/congo-loi-1998-08.pdf` (carnet id
  `loi-1998-08-genocide-crimes-guerre`, manifeste `sgg-lois`) — à traiter
  comme un texte indépendant, jamais rattaché au Code pénal.

Non repris dans ce découpage (hors scope de ce ticket, à statuer plus tard) :
le préambule historique du recueil (L1-118 du markdown, récit de compilateur
sur l'application coloniale du Code pénal en A.E.F.) et la loi n°54-293 de
1954 sur les amendes pénales qui y est citée en intégralité (ses propres
articles 1-18) — pas du droit pénal congolais actuel, mais potentiellement
une source à part entière si quelqu'un veut la retrouver officiellement.
