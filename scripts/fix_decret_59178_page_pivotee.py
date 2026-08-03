#!/usr/bin/env python3
"""Remédiation 2026-08-03 phase 7 : récupère les articles 25 à 44 du Décret
n° 59-178 du 21 août 1959 (statut commun des cadres des personnels des
douanes), perdus non pas par une extraction MinerU bâclée mais par une page
scannée à l'envers (rotation 180°) dans le PDF source — confirmé en
téléchargeant le PDF et en le faisant pivoter visuellement : le texte est
parfaitement lisible et intégralement récupérable, rien n'est perdu côté
source.

Diagnostic complet (lecture seule, 2026-08-03) :
- `curation_flags` signalait « 20 numéros d'articles consécutifs absents
  (25-44) : perte de pages probable à l'extraction », correctement — c'est
  un vrai bug, pas un faux positif comme la plupart des autres flags résolus
  cette nuit (cf. `resolve_false_positive_flags.py`).
- Le document_id ne porte QUE sa propre tranche d'articles (`articles.
  document_id` scope déjà correctement chaque JO éclaté en actes séparés,
  même si plusieurs documents FLUX partagent le même fichier markdown source
  `congo-jo-1959-23.md`, 4703 lignes) : aucun risque de mélanger des articles
  d'un autre acte du même JO.
- La page pivotée occupe les lignes 2248-2386 du markdown source PARTAGÉ
  (texte totalement inversé par MinerU, ex. « sauenop saspinqnQ.8 » pour
  « Art. 25... des douanes »). Le document lui-même va des lignes 2048 à 2491
  dans ce même fichier (borné par le titre du décret précédent en 2044-2046
  et celui du suivant, « Décret n° 59-179 », en 2493).
- Article 24, actuellement en base, contient AUJOURD'HUI son propre texte +
  tout le charabia de la page pivotée + la fin (intacte) de l'article 44, qui
  n'a jamais pu être détachée puisque aucun « Article 25 » à « Article 44 »
  n'a pu être reconnu entre les deux.
- Reconstruction : PDF source retéléchargé (lecture seule MinIO), page
  d'index 22 (0-based) pivotée à 180° avec PyMuPDF, OCR par colonne (gauche
  puis droite — la colonne unique donnait un ordre de lecture mélangé) via
  `Pixmap.pdfocr_save`, puis relecture manuelle pour ne conserver que les
  articles 25 à 44 (la coquille OCR « Art. 83 » a été corrigée en « Art. 33 »
  — seul le numéro d'article a été retouché, jamais le contenu juridique).
- Validation : le texte complet du document (lignes 2048-2491 avec la page
  corrigée insérée) reparse en 57 articles, 0 collision, 25 à 44 tous
  présents ; les 35 autres articles du document (hors zone 25-44) sont
  BYTE-IDENTIQUES à ce qui est actuellement en base — seul l'article 24 (dont
  le charabia et la fin orpheline sont retirés) diffère, comme attendu.

Contrairement à `restructure_stock_codes.py` (qui relit le markdown déjà
stocké sans le modifier), ce script réinsère un texte reconstruit à partir du
PDF source pour UN SEUL document — jamais d'écriture MinIO, jamais de
re-upload : la correction ne vit qu'en base (articles/structure_nodes), le
markdown original (fautif) reste inchangé sur MinIO, conformément à
l'interdit absolu d'écriture MinIO même autorisée.

Usage (dev — défaut) :
    python scripts/fix_decret_59178_page_pivotee.py                    # dry-run
    python scripts/fix_decret_59178_page_pivotee.py --execute          # écrit sur le dev local

Usage (prod) :
    python scripts/fix_decret_59178_page_pivotee.py --target prod                  # dry-run, profil PROD_RO_*
    python scripts/fix_decret_59178_page_pivotee.py --target prod --execute        # écrit en PROD, profil PROD_RW_DB_*

Garde-fous : mêmes profils/vérifications que les scripts précédents cette
nuit (dump frais requis avant tout --execute --target prod, saisie
« PRODUCTION » exigée). Avant toute écriture, le script revérifie que
l'article 24 actuel contient encore le marqueur de charabia connu et
qu'aucun article 25-44 n'existe déjà — sinon il refuse (état différent de
celui diagnostiqué).

Procédure d'annulation : `ingest_hierarchy` remplace structure_nodes/
articles/article_versions de CE SEUL document (`clear_document_structure`
scope strictement par document_id). Un dump pris juste avant restaure l'état
antérieur intégralement.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

from src.api.main import ingest_hierarchy  # noqa: E402
from src.db.models import Article, ArticleVersion, CurationFlag, LegalDocument, MediaFile  # noqa: E402
from src.extractor.parser import LegalDocumentParser  # noqa: E402

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "5433")

DOCUMENT_ID = "d29c1493-bf39-4292-925a-16abee582897"

# Marqueur de charabia connu (page pivotée non détectée par MinerU) — sert de
# garde-fou : si l'article 24 actuel ne contient plus ce texte, l'état a
# changé depuis le diagnostic et le script refuse plutôt que de deviner.
MARQUEUR_CHARABIA = "sauenop saspinqnQ"

# Texte complet et corrigé du document (lignes 2048-2491 du markdown source
# partagé congo-jo-1959-23.md, avec les lignes 2248-2386 — la page pivotée —
# remplacées par leur contenu reconstruit : PDF source retéléchargé, page
# pivotée à 180°, OCR par colonne). Validé : 57 articles, 0 collision, les 35
# articles hors zone 25-44 sont byte-identiques à la base actuelle.
TEXTE_DOCUMENT_CORRIGE = """LE PREMIER MINISTRE,

Vii les lois constitutionnelles du 20 février 1959;

Vu la délibération n° 42/57 du 14 août 1957 portant le statut général des fonctionnaires des cadres de la Répub

Vu l'arrêté n° 1968/FP du 14 juin 1958 fixant la liste limitative des cadres des fonctionnaires de la République du Congo et les textes modificatifs suivents:

Vu le décret n° 59-023/FP. du 30 janvier 1959 complétant l'arrêté précité en ce qui concerne les personnes de la douane;

Vu la loi n° 10-59/FP. du 17 février 1959 abrogeant l'article 3, paragraphe 2 du décret n° 56-1228 du 3 décembre 1956 modifié par le décret n°\\57-480 du 4 avril 1957 en ce qui concerne la police et la douane;

Vu l'arrêté n° 2425/FP. du 15 juillet 1958 fixant les éché-nements indicaires des cadres des fonctionnaires de la République du Congo et ses actes modificatifs subséquents;

Vu les arrêtés portant statuts communs des cadres des services administratifs et financiers et les arrêtés et décrites modificatifs suivents;

- Vu l'avis du comité consultatif de la fonction publique;

Le conseil des ministres entendu,

DECRETÉ :

Art.  $1^{\\text{er}}$ . — Leprésent décret fixe, en application de l'article 2 de la délibération  $\\mathfrak{n}^{\\circ}$  42/57 du 14 août 1957, le statut commun des cadres des catégories A, B, C, D, E des personnels de l'administration des douanes de la République du Congo.

CHAPITRE PREMIER

Dispositions générales.

Art. 2. — Leprésent statut s'applique aux cadres suivants, qui sont classés dans les services administratifs et financiers de la République du Congo.

Ils sont répartis en deux hierarchies correspondant l'une aux cadres sédentaires, l'autre aux cadres actifs, conformément au texte ci-dessous :

Cadres sédentaires :

Categorie A.

Inspecteurs principaux hors classe.

Inspecteurs principaux.

Catégorie B.

Inspecteurs hors classe.

Inspecteurs.

Categorie C.

Verificateurs.

Catégorie D.

Contrôleurs.

Catégorie E 1.

Agents de constatation.

Cadres actifs :

Catégorie B.

Officiers des douanes (capitaines, lieutenants).

Catégorie C.

Adjudants-chefs.

Adjudants.

Catégorie D.

Brigadiers-chefs.

Catégorie E 1.

Brigadiers.

Catégorie E 2.

Preposés.

Section I. Fonctions et emplois.

Art. 3. — Les fonctions et employés des fonctionnaires de chaque cadre des personnels de l'administration des douanes de la République du Congo sont définis et précises aux arti

# $1^{\\circ}$  Services sédentaires.

Art. 4. — Les inspecteurs principaux hors classe et inspecteurs principaux des douanes ont vocation pour occuper des employés comptant des fonctions de direction, de conception administrative et d'organisation générale du service des douanes.

Ces fonctionnaires peuvent être, en outre, appelés à:gérer les bureaux centraux des douanes.

Art. 5. — Les inspecteurs hors classe et inspecteurs sont charges, dans les services d'exécution, des employés comptant des fonctions d'organisation, de contrôle, et de la recherche de la fraude dans ses aspects les plus techniques. Ils sont également charges de superviser le travail effectué par les vérificateurs pour ce qui concerne la visite des marchandises.

Les inspecteurs peuvent aussi être appelés à servir auprès de la direction. Ils portent alors le titre d'inspecteurs-rédacteurs.

Les inspecteurs hors classe peuvent, à défaut d'inspecteurs principaux, être chargés de la gestion des bureaux centraux.

Art. 6. — Les vérificateurs sont charges, sous l'autorité directe des inspecteurs, de la vérification et de la poursuite des infractions. Ils sont également appelés à superviser le travail des contrôleurs et des agents de constatation en ce qui concerne la tenue des registres de la section.

Les vérificateurs peuvent être nommés adjoints à un chef de bureau central.

Ils sont normalement chef de bureau secondaire.

Art. 7. — Les contrôleurs sont charges de la tenue des écritures et des registres comptables. Ils peuvent occuper les fonctions de chef de section ou d'adjoint.

S'ils représentent les qualités requisées, les contrôleurs peuvent également être chargés de la gestion des bureaux secondaires des douanes.

Art. 8. — Les agents de constatation des douanes sont charges, dans les sections d'écriture, de la tenue des différents registres, de concert avec les contrôleurs.

Art. 9. — En raison des sujetions particulieres inherentes à la profession, seuls les employés d'agent de constatation et de contrôleurs sont ouverts aux candidats de sexe féminin.

# $2^{\\circ}$  Services actifs.

Art. 10. — Les officiers des douanes sont charges du commandement général du personnel des brigadiers et de la liaison entre les différents brigadiers.

Art. 11. — Les adjudants-chefs et adjudants sont charges, sous les ordres. des officiers des douanes, de l'encadrement des brigadiers-chefs et des brigadiers.

Ils sont placés à la tete des brigades importantes ou des groupes de brigades.

Ils agissant, par l'intermédiaire de leurs officiers, sous les ordres des chefs des bureaux centraux et secondaires.

Art. 12. — Les brigadiers-chefs sont charges, sous l'autorité des adjudants et adjudants-chefs, de l'encadrement des • brigadiers et préposés.

Ils sont charges de la recherche et de la poursuite de la fraude.

Les brigadiers-chefs sont placés à la tête des brigades à faible effectif.

Art. 13. — Les brigadiers sont charges de l'encadrement des préposés.

Les brigadiers et préposés assurent la surveillance des frontières de terre et de mer dont la garde leur est confie. Ils constatent les infractions aux lois et règlements de douane et de toutes autres réglementations pour l'application desquelles il est fait appel au concours de l'administration des douanes. Ils participent, en outre, à la visite des marchandises et des voyageurs.

Art. 14. — En raison des conditions d'aptitude physique exigeées des fonctionnaires des cadres des services actifs des douanes, l'accès de ces cadres est réservé aux seuils candidats du sexe masculin, qui replissent, en outre, les conditions voulues pour être classés dans le « service armé » par l'autorité militaire, plus particulièrement en ce qui concerne les acuités visuelle et auditive (V = 3 pour la vue; A = 3 pour l'ouie).

Art. 15. — Les agents responsables de postes de commandement ont droit, dans l'exercice de leurs fonctions, au port d'armes à feu.

# Section II. Carrière.

Art. 16. — La carrière des fonctionnaires de chacune des catégories de cadres de l'administration des douanes de la République du Congo comporte un grade.

Ce grade est divisé en 10 échelons normaux et un échelon élève ou stagiaire.

La répartition de ce grade est faite exceptionnellement, ainsi qu'il suit, pour les cadres désignés aux articles 17, 18 et 19 du present décret.

Art. 17. — Le cadre de la catégorie A comprend deux hierarchies ainsi définies :

Inspecteurs principaux hors classe 4 échelons

Inspecteurs principaux 9 échelons

Le grade d'inspecteur ne comporte pas d'échelon élève.

Art. 18. — Les cadres sédentaires et actifs de la catégorie B comprément deux hierarchies ainsi définies :

Inspecteurs hors classe ou capitaines 4 échelons

Inspecteurs ou lieutenants 10 échelons

Eleveinspecteur 1echelonunique

Le grade d'officier des douanes ne comporte pas d'échélon élève.

Art. 19. -- Le cadre de la catégorie C des services actifs comprend deux hierarchies ainsi définies :

Adiudants-chefs 4 échelons

6 échelons

Ce grade ne comporte pas d'échelon élève.

Art. 20. — Les échéonnements individaires des cadres de la douane des services sédentaires et des services actifs, sont ceux qui sont fixés pour les services administratifs et financiers par l'arrêté  $\\mathbf{n}^{\\circ}$  2425/FP. du 15 juillet 1958 fixant les échéonnements individaires des cadres de fonctionnaires de la République du Congo.

Art. 21. — Par dérogation aux régles du statut général, et compte tenu du caractère militaire de la hierarchie des cadres actifs de l'administration des douanes :

$1^{\\circ}$  Les préposés de la catégorie E 2 sont appelés préposés principaux de  $1^{\\text{er}}$ ,  $2^{\\text{e}}$ ,  $3^{\\text{e}}$  et  $4^{\\text{e}}$  échelon au lieu de préposés de  $7^{\\text{e}}$ ,  $8^{\\text{e}}$ ,  $9^{\\text{e}}$  et  $10^{\\text{e}}$  échelon.

$2^{\\circ}$  Les brigadiers de la catégorie E 1 sont dénommés brigadiers de  $2^{\\circ}$  classe  $1^{\\text{er}}$ ,  $2^{\\circ}$ ,  $3^{\\circ}$ ,  $4^{\\circ}$ ,  $5^{\\circ}$  et  $6^{\\circ}$  échelon au début de leur hierarchie, puis brigadier de  $1^{\\text{re}}$  classe  $1^{\\text{er}}$ ,  $2^{\\circ}$ ,  $3^{\\circ}$  et  $4^{\\circ}$  échelon aux lieu et place des  $7^{\\circ}$ ,  $8^{\\circ}$ ,  $9^{\\circ}$  et  $10^{\\circ}$  échelon du statut général.

3° Les brigadiers-chefs de la catégorie D sont dénommés de 2° classe 1er, 2°, 3°, 4°, 5° et 6° échelon au début de leur hierarchie, puis brigadiers-chefs de 1re classe 1er, 2°, 3° et 4° échelon aux lieu et place de 7°, 8°, 9° et 10° échelon du statut général.

4° A partir du 7° échelon inclus de leur grade, les adjudants sont dénommés adjudants-chef.

CHAPITRE II

Recrutement.

# Section I. - Recrutement direct.

Art. 22. - Les candidats à unemploi dans les divers cadres de l'administration des douanes de la République du Congo seront choisis, par priorité, parmi les candidats nés sur le territoire de la République ou qui y ont résidé pendant dix ans consécutivement.

# A. — Cadres sédentaires.

Art. 23. — Il n'y a pas de recrutement direct pour le grade d'inspecteur principal.

Art. 24. — Peuvent être nommés élèves-inspecteurs des douanes :

a) Sur titres, sans concours, les candidats titulaires d'une licence, lorsqu'le nombre des candidats est inférieur ou au plus égal au nombre des places à pouvoir.

Dans le cas contraire, un concours sera organise pour départager les candidats;

b) Après concours, les candidats titulaires du baccalauréat complet de l'enseignement secondaire.

Les candidats ainsi recrutés doivent suivre un stage de deux ans à l'école nationale des douanes, dans les conditions qui seront fixées ultérieurement en accord avec la direction de l'école des douanes de Neuilly-sur-Seine.

Art. 25. — Peuvent seuls être nommés élèves-vérificateurs des douanes, les candidats titulaires du baccalauréat de l'enseignement secondaire.

a) Sur titres, sans concours, lorsque le nombre des candidats est inférieur ou au plus égal au nombre des places à pourvoir ;

b) Après concours dans le cas contraire.

Pour être titularisés, les élèves-vérificateurs devront suivre, pendant un an, un stage de formation professionnelle.

Art. 26. — Peuvent seuls être nommés élèves-contrôleurs des douanes, les candidats titulaires du B.E. ou du B.E.P.C., reçus au concours général de recrutement d'élèves-fonctionnaires, élèves au titre de la République du Congo de la section des douanes du centre de préparation aux carrières administratives (C.P.C.A.) ou de l'organisme appelé à le remplacer, qui auront satisfait aux conditions de scolarité et aux examens de sortie de cette école.

Les candidats au concours général de recrutement d'élèves-fonctionnaires, titulaires de la première partie du baccalauréat seront dispensés des épreuves théoriques et classés en tête de liste.

Pour être titularisés, les élèves-vérificateurs devront suivre, pendant un an, un stage de formation professionnelle.

Art. 27. — Peuvent seuls être nommés élèves-agents de constatation des douanes, les candidats justifiant avoir accompli une année complète de scolarité dans une classe de 3e d'un lycée, collège ou établissement privé d'enseignement secondaire reconnu, admis, après concours, à suivre un cycle d'enseignement professionnel du service des douanes de six mois.

Pour être titularisés ils devront accomplir un stage de formation professionnelle d'un an.

Art. 28. — Les conditions d'organisation des concours et des stages prévus aux articles ci-dessus feront l'objet de décrets ultérieurs établis en conseil des ministres. Jusqu'à l'intervention de ces textes, les règlements actuels concernant ces matières restent provisoirement en vigueur.

# B. — Cadres actifs.

Art. 29. — Il n'y a pas de recrutement direct pour les cadres des officiers des douanes, des adjudants et adjudants-chefs, et des brigadiers-chefs qui constituent le débouché professionnel pour les fonctionnaires des cadres des catégories E 1 et E 2 des services actifs.

Art. 30. — Peuvent seuls être nommés élèves-brigadiers des douanes, les candidats âgés de 20 ans au moins justifiant avoir accompli une année complète de scolarité dans une classe de 3e d'un lycée, collège, ou établissement privé d'enseignement secondaire reconnu, admis après concours, à suivre un cycle d'enseignement professionnel du service des douanes de six mois.

Le concours comprendra des épreuves sportives dont la nomenclature sera fixée par un décret dans le cadre de l'organisation générale des concours de la fonction publique.

Pour être titularisés les élèves-brigadiers devront accomplir un stage professionnel d'un an.

Art. 31. — Peuvent seuls être nommés élèves-préposés des douanes :

1° Les candidats, titulaires du C.E.P., reçus au concours local de recrutement des élèves-préposés, lequel comporte des épreuves physiques ;

2° Dans la limite de 1/5e des emplois disponibles, les anciens combattants, et à défaut, les anciens militaires de carrière, ayant cinq années de services actifs, âgés de 35 ans au plus au 1er janvier du concours. Ils devront savoir lire et écrire le français et subir une épreuve psychotechnique (mémoire et attention).

Pour être titularisés, les élèves-préposés doivent accomplir un an de stage professionnel.

Art. 32. — Le programme des matières, les épreuves, les modalités d'organisation des concours et stages prévus aux articles ci-dessus feront l'objet d'un décret ultérieur.

Jusqu'à la parution de ce décret les textes actuels concernant ces matières restent provisoirement en vigueur.

# Section II. — Recrutement professionnel.

# A. — Cadres sédentaires.

Art. 33. — Peuvent seuls être nommés inspecteurs principaux stagiaires des douanes, les inspecteurs hors classe et inspecteurs remplissant les conditions prévues à l'article 51 de la délibération n° 42/57 du 14 août 1957 et qui auront satisfait aux épreuves d'un concours professionnel.

Les candidats devront être âgés de 35 ans au moins et de 45 ans au plus.

Art. 34. — Peuvent seuls être nommés inspecteurs stagiaires, les vérificateurs remplissant les conditions prévues à l'article 51 de la délibération n° 42/57 du 14 août 1957 et qui auront satisfait aux épreuves d'un concours professionnel.

Les fonctionnaires admis doivent suivre, en qualité d'auditeurs libres, un stage professionnel accéléré à l'école nationale des douanes, au titre de la République du Congo.

Art. 35. — Peuvent seuls être nommés vérificateurs stagiaires, les contrôleurs des douanes remplissant les conditions prévues à l'article 51 de la délibération n° 42/57 du 14 août 1957 et qui auront satisfait aux épreuves d'un concours professionnel.

Art. 36. — Peuvent seuls être nommés contrôleurs stagiaires, les agents de constatation remplissant les conditions prévues à l'article 51 de la délibération n° 42/57 du 14 août 1957 et qui auront satisfait aux épreuves d'un concours professionnel.

Art. 37. — Peuvent seuls être nommés agents de constatation stagiaires, les préposés et brigadiers remplissant les conditions prévues à l'article 51 de la délibération n° 42/57 du 14 août 1957 et qui auront satisfait aux épreuves d'un concours professionnel.

# B. — Cadres actifs.

Art. 38. — Peuvent seuls être nommés officiers stagiaires des douanes au grade de lieutenant stagiaire, les adjudants-chefs et adjudants et, par dérogation spéciale aux règles du recrutement professionnel du statut général, les brigadiers-chefs remplissant les conditions requises par l'article 51 de la délibération n° 42/57 du 14 août 1957, ayant au minimum 35 ans d'âge au 1er janvier de l'année du concours.

Ils devront, avant d'être titularisés, suivre un stage de commandement à l'école nationale des douanes, dans les conditions qui seront fixées par un décret ultérieur.

Art. 39. — Il n'y a pas de recrutement professionnel prévu par voie de concours pour l'accès au grade d'adjudant stagiaire. Seules des promotions sur liste d'aptitude permettent d'y accéder dans les conditions définies à l'article 45 du présent décret.

Art. 40. — Peuvent seuls être nommés brigadiers-chefs stagiaires les brigadiers remplissant les conditions prévues à l'article 51 de la délibération n° 42/57 du 14 août 1957 et qui auront satisfait aux épreuves d'un concours professionnel.

Art. 41. — Peuvent seuls être nommés brigadiers stagiaires les préposés remplissant les conditions prévues à l'article 51 de la délibération n° 42/57 du 14 août 1957 et qui auront satisfait aux épreuves d'un concours professionnel.

# C. — Tous cadres.

Art. 42. — Le programme des matières, les épreuves, les modalités d'organisation de ces concours professionnels feront l'objet d'un décret ultérieur.

Jusqu'à la parution de ce texte les arrêtés actuels concernant ces matières restent provisoirement en vigueur.

Art. 43. — Les nominations des fonctionnaires intéressés reçus aux concours professionnels prévus aux articles 33 à 41 inclus du présent décret seront prononcées dans les conditions prévues à l'article 60 de la délibération n° 42/57 du 14 août 1957, portant statut général des fonctionnaires des cadres de la République du Congo.

# Section III. — Recrutement sur liste d'aptitude.

Art. 44. — Peuvent seuls être nommés, dans les cadres sédentaires :

1° Inspecteurs des douanes ;

2° Vérificateurs des douanes ;

3° Contrôleurs des douanes,
au titre du recrutement sur liste d'aptitude, respectivement :

$1^{\\circ}$  Les vérificateurs des douanes;  
$2^{\\circ}$  Les contrôleurs des douanes;  
$3^{\\circ}$  Les agents de constatation des douanes,

replissant les conditions déterminées par le décret  $n^{\\circ}59 - 30 / \\mathbb{F}\\mathbb{P}$  du 30 janvier 1959 fixant les conditions dans lesquelles sont opérées les promotions sur liste d'aptitude, en application de l'article 52 de la délibération  $n^{\\circ}42 / 57$  du 14 août 1957.

Art. 45. — Peuvent seuls être nommés, dans les cadres actifs :

$1^{\\circ}$  Adjudants des douanes;  
$2^{\\circ}$  Brigadiers-chefs des douanes,

au titre du recrutement sur liste d'aptitude, respectivement :

$1^{\\circ}$  Les brigadiers-chefs des douanes;  
$2^{\\circ}$  Les brigadiers des douanes,

replissant les conditions déterminées par le décret  $n^{\\circ}59 - 30 / \\mathrm{FP}$  du 30 janvier 1959, fixant les conditions dans lesquelles sont opérées les promotions sur liste d'aptitude, en application de l'article 52 de la délibération  $n^{\\circ}42 / 57$  du 14 août 1957.

Art. 46. -- Il n'y a pas de recrutement sur liste d'aptitude prévu pour l'accès aux cadres suivants : inspecteurs principaux des douanes, officiers des douanes, agents de constatation des douanes.

Art. 47. — Les nominations, prononcées au titre des articles 44 et 45 ci-dessus, intervennent dans les conditions prévues à l'article 60 de la délibération n° 42/57 du 14 août 1957..

Section IV. Dispositions transitoires, intégration.

Art. 48. — Les régles président à l'intégration des fonctionnaires des anciens cadres des douanes dans les nouveaux cadres institués par leprésent décret, sont celles fixées par les décrets  $\\mathrm{n}^{\\mathrm{os}}$  59-23/FP. et 59-24/FP. du 30 janvier 1959, sauf exceptions fixées aux articles 49, 50 et 51 ci-dessous.

Art. 49. — Par dérogation aux dispositions du décret  $\\mathfrak{n}^{\\circ}59 - 30 / \\mathbb{F}\\mathbb{P}$ . du 30 janvier 1959, les contrôleurs adjoints du cadre supérieur des douanes de l'A. E. F., titulaires du diplôme de l'école des cadres supérieur, les fonctionnaires appartenant à la hierarchie supérieur du corps commun des douanes de l'A. E. F., en voie d'extinction, seront intégrés, sauf option contraire de leur part, dans le cadre de la catégorie C des vérificateurs des douanes, dans les conditions prévues à l'article 60 de la délibération  $\\mathfrak{n}^{\\circ}42 / 57$  du 14 août 1957.

Art. 50. — En application des dispositions de l'article 54 de la délibération n° 42/57 du 14 août 1957, un recrutement initial pourra être effectué :

1° Pour l'accès au grade d'inspecteur des douanes, au besoin, parmi les vérificateurs actuelsment en service, n'avant pas bénéficié d'une promotion sur liste d'aptitude.  
Les fonctionnaires ainsi désignés devront être aptes à suivre un stage de formation professionnelle accélérée à l'école des douanes de Neuilly-sur-Seine. Leur nomination n'intervendra que s'ils sont proposés à la fin de ce stage;  
2° Pour l'accès au grade d'adjudant, au besoin parmi les brigadiers-chefs réunissant au minimum douze années de services et n'avant pas bénéficié d'une promotion sur liste d'aptitude.  
Les fonctionnaires ainsi désignés devront être aptes à suivre un stage de formation professionnelle, dont les conditions seront fixées ultérieurement.

Pour l'accès au grade de brigadier-chef :

a) Au besoin, parmi les brigadiers réunissant quinze ans de services et moins de 50 ans d'âge, n'avant pas bénéficié, d'une promotion sur liste d'aptitude.  
b) Au besoin, parmi les agents des brigades justifient avoir accompli une année complète dans une classe de  $3^{\\circ}$  des établissements secondaires publics ou privés, réunissant au moins quatre ans de service, et reconnus aptes au commandement.

Les fonctionnaires ainsi désignés doivent effectuer un stage spécial de commandement dont le lieu et les conditions d'organisation feront l'objet d'un décret ultérieur.

Art. 51. — Les préposés des douanes seront intégrés dans le nouveau cadre des préposés, conformément au tableau de concordance ci-après :

<table><tr><td>Ancienne hierarchie
(cadre des préposés)</td><td>Nouvelle hierarchie
(cadre des préposés)</td></tr><tr><td>Prép. ppal 2e éch. ind. 126
d° 1er éch. ind. 120
Préposé.. 2e éch. ind. 110
d° 1er éch. ind. 106
Prép. sta. ind. 100</td><td>1er éch. ind. 140 1/2 A.C.
1er éch. ind. 140 A.C.
1er éch. ind. 140 A. Sup.
1er éch. ind. 140 A. Sup.
Elève.. ind. 120 A.C.</td></tr></table>

Le maximum d'anciennete conservée est de deux ans.

Art. 52. — La nomination des fonctionnaires intérêtsés interviendra dans les conditions prévues à l'article 60 de la délibération n° 42/57 du 14 août 1957.

Art. 53. — En application de l'article 154 de la délibération n° 42/57 du 14 août 1957, les dispositions transitoires relatives à l'intégration dans les cadres de certains contractuels et décidations seront déterminées par un décret ultérieur, pris après avis du comité consultatif de la fonction publique.

CHAPTER III

Avancement.

Art. 54. — Les avancements d'échéon des fonctionnaires du cadre de la douane sont alloués dans les conditions pré-vues à l'article 72 de la délibération n° 42/57 du 14 août 1957.

L'examen des situations des fonctionnaires susceptibles de bénéficier d'un avancement d'échelon s'effectue en commum pour l'ensemble de chaque cadre.

Lorsque l'effectif d'un cadre est inférieur à cinq unités, l'examen de la situation des fonctionnaires de ce cadre susceptibles de bénéficier d'un avancement d'échelon s'effectue en commun avec celui des personnels d'un ou plusieurs autres cadres d'une catégorie correspondante des services administratifs et financiers de la République du Congo.

CHAPTER IV

Dispositions particulieres.

Art. 55. — Les agents des différentes hierarchies du corps des douanes préteront serment devant les tribunaux dans les mêmes conditions que les fonctionnaires du cadre métropitain des douanes.

Ils recoivent une commission d'emploi délivrée par le directeur du service des douanes, par dérogation et au nom du Premier ministre de la République du Congo.

Ils jouissent, au point de vue exécution du service des douanes, sur le territoire de la République du Congo, des mêmes prerogatives et ont les mêmes devoirs que les agents du cadre métropolitain des douanes.

CHAPTER V

Dispositions diverse.

Art. 56. — Le nombre total des détachements et des mises en disponibilité ne pourra excéder  $20\\%$  de l'effectif total de chaque hierarchie du cadre des douanes.

Cette limitation ne s'applique pas aux fonctionnaires déta-chés dans les services d'Etat.

Art. 57. — Leprésent décret sera publié au Journal officiel de la République du Congo et communiqué partout où besoin sera.

Fait à Brazzaville, le 21 août 1959.

Abbe Fulbert Youlou.

Par le Premier ministre :

Le secretaire d'Etat à la fonction publique, V. SATHOUD.

Le ministre des finances,

J. VIAL."""


def _guard_dev_only() -> None:
    if (DB_HOST, str(DB_PORT)) != ("127.0.0.1", "5433"):
        raise SystemExit(
            f"Refus : cible dev demandée mais l'environnement pointe {DB_HOST}:{DB_PORT}, "
            "pas 127.0.0.1:5433. Utiliser --target prod pour la production."
        )


def _session_dev():
    db_user = os.getenv("DB_USERNAME", "root")
    db_pass = os.getenv("DB_PASSWORD", "root")
    db_name = os.getenv("DB_DATABASE", "mibeko-db")
    url = f"postgresql://{db_user}:{db_pass}@{DB_HOST}:{DB_PORT}/{db_name}"
    engine = create_engine(url)
    return sessionmaker(bind=engine)(), None


def _session_prod_readonly():
    from src.db.prod_readonly import SQLSTATE_LECTURE_SEULE, assert_read_only, charger_cible, creer_engine

    cible = charger_cible()
    engine = creer_engine(cible)
    sqlstate = assert_read_only(engine)
    if sqlstate != SQLSTATE_LECTURE_SEULE:
        raise SystemExit(f"Refus : lecture seule non prouvée par SQLSTATE {SQLSTATE_LECTURE_SEULE} (obtenu : {sqlstate}).")
    print(f"Préflight PROD : lecture seule prouvée ({cible.resume()}).")
    return sessionmaker(bind=engine)(), engine


def _session_prod_ecriture(engine_ro):
    from src.promotion.push_corpus import CibleProdAmbigue, ConfigurationProdManquante, charger_cible_ecriture

    try:
        engine_rw = charger_cible_ecriture()
    except (ConfigurationProdManquante, CibleProdAmbigue) as exc:
        raise SystemExit(f"Refus : {exc}")

    with engine_rw.connect() as cnx_rw, engine_ro.connect() as cnx_ro:
        compte_sql = "select count(*) from legal_documents"
        if cnx_rw.execute(text(compte_sql)).scalar() != cnx_ro.execute(text(compte_sql)).scalar():
            raise SystemExit(
                "Refus : la cible RW (PROD_RW_DB_*) ne répond pas comme la cible RO "
                "(PROD_RO_DB_*) — les deux profils ne visent pas la même base."
            )
    return sessionmaker(bind=engine_rw)()


def flatten_articles(nodes):
    out = []
    for node in nodes:
        if node["type"] == "ARTICLE":
            out.append(node)
        if node.get("children"):
            out.extend(flatten_articles(node["children"]))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true", help="Écrit réellement (par défaut : dry-run, aucune écriture).")
    parser.add_argument("--target", choices=["dev", "prod"], default="dev", help="Cible (défaut : dev).")
    args = parser.parse_args()

    engine_ro = None
    if args.target == "dev":
        _guard_dev_only()
        db, _ = _session_dev()
    else:
        db, engine_ro = _session_prod_readonly()

    document = db.query(LegalDocument).filter(LegalDocument.id == DOCUMENT_ID, LegalDocument.deleted_at.is_(None)).first()
    if document is None:
        raise SystemExit(f"Refus : document {DOCUMENT_ID} introuvable ou supprimé.")
    if document.curation_status == "published":
        raise SystemExit("Refus : document publié, hors garde-fou sans validation humaine.")

    article_24 = db.query(Article).filter(Article.document_id == DOCUMENT_ID, Article.numero_article == "24").first()
    version_24 = (
        db.query(ArticleVersion).filter(ArticleVersion.article_id == article_24.id).first() if article_24 else None
    )
    if version_24 is None or MARQUEUR_CHARABIA not in version_24.contenu_texte:
        raise SystemExit(
            "Refus : l'article 24 actuel ne contient plus le marqueur de charabia attendu — "
            "l'état a changé depuis le diagnostic, ne pas appliquer ce correctif tel quel."
        )
    articles_existants_25_44 = (
        db.query(Article)
        .filter(Article.document_id == DOCUMENT_ID, Article.numero_article.in_([str(n) for n in range(25, 45)]))
        .count()
    )
    if articles_existants_25_44 > 0:
        raise SystemExit(
            f"Refus : {articles_existants_25_44} article(s) 25-44 existent déjà pour ce document — "
            "déjà corrigé ou état inattendu, ne pas réappliquer."
        )
    print("Garde-fous : article 24 contient bien le charabia connu, aucun article 25-44 existant.")

    hierarchy = LegalDocumentParser(text_content=TEXTE_DOCUMENT_CORRIGE).parse_hierarchy()
    articles = flatten_articles(hierarchy)
    numeros = [a["number"] for a in articles]
    attendus = [str(n) for n in range(2, 58)]
    manquants = [n for n in attendus if n not in numeros]
    if len(articles) != 57 or manquants:
        raise SystemExit(
            f"Refus : le reparse ne donne pas le résultat attendu (57 articles, 2 à 57 tous présents) — "
            f"obtenu {len(articles)} articles, manquants : {manquants}."
        )
    from collections import Counter
    doublons = {k: c for k, c in Counter(numeros).items() if c > 1}
    if doublons:
        raise SystemExit(f"Refus : collisions inattendues dans le reparse : {doublons}.")
    print(f"Reparse validé : {len(articles)} articles, 25-44 tous présents, 0 collision.")

    md_media = (
        db.query(MediaFile)
        .filter(MediaFile.document_id == DOCUMENT_ID, MediaFile.file_category == "EXTRACTION_MARKDOWN")
        .first()
    )
    flags_avant = (
        db.query(CurationFlag).filter(CurationFlag.document_id == DOCUMENT_ID, CurationFlag.resolved.is_(False)).count()
    )
    articles_avant = db.query(Article).filter(Article.document_id == DOCUMENT_ID, Article.deleted_at.is_(None)).count()

    print(f"\nCible : {args.target}. Document : {document.titre_officiel!r}")
    print(f"Avant  — articles : {articles_avant}, flags ouverts : {flags_avant}")

    if not args.execute:
        print("\nDRY-RUN : aucune écriture. Relancer avec --execute pour appliquer.")
        return

    if args.target == "prod":
        print("\n  Ce document PROD va voir sa structure (articles/structure_nodes) reconstruite (57 articles, dont 20 récupérés).")
        saisie = input("Taper PRODUCTION pour confirmer : ").strip()
        if saisie != "PRODUCTION":
            print("Annulé.")
            sys.exit(1)
        db = _session_prod_ecriture(engine_ro)
        document = db.query(LegalDocument).filter(LegalDocument.id == DOCUMENT_ID, LegalDocument.deleted_at.is_(None)).first()
        md_media = (
            db.query(MediaFile)
            .filter(MediaFile.document_id == DOCUMENT_ID, MediaFile.file_category == "EXTRACTION_MARKDOWN")
            .first()
        )

    document.metadata_ = {**(document.metadata_ or {}), "correction_page_pivotee_25_44": True}
    ingest_hierarchy(db, document, hierarchy, run_id=None, media_id=md_media.id if md_media else None, validation_status="pending")
    db.commit()

    articles_apres = db.query(Article).filter(Article.document_id == DOCUMENT_ID, Article.deleted_at.is_(None)).count()
    flags_apres = (
        db.query(CurationFlag).filter(CurationFlag.document_id == DOCUMENT_ID, CurationFlag.resolved.is_(False)).count()
    )
    print(f"\n--execute ({args.target}) : modifications validées (COMMIT).")
    print(f"Après  — articles : {articles_avant} → {articles_apres} (attendu +20)")
    print(f"Après  — flags ouverts : {flags_avant} → {flags_apres}")


if __name__ == "__main__":
    main()
