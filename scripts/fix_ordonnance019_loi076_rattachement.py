#!/usr/bin/env python3
"""Remédiation 2026-08-03 phase 8 : corrige un rattachement croisé entre deux
documents FLUX issus du même Journal Officiel (congo-jo-1990-01-sp, numéro
spécial de mai 1990) : ORDONNANCE n° 019-84 du 23 août 1984 (modification de
la Constitution) et LOI n° 076-84 du 7 décembre 1984 (loi de ratification de
cette même ordonnance).

Diagnostic complet (lecture seule, 2026-08-03, PDF source retéléchargé et
rendu page par page pour lever toute ambiguïté) :
- Dans le JO imprimé, l'Ordonnance 019-84 (page imprimée 15) se termine par
  son Art. 3 (table de renumérotation « AU LIEU DE / LIRE RESPECTIVEMENT »),
  son Art. 4 (clause d'abrogation) et sa signature (23 août 1984) — puis, EN
  BAS DE LA MÊME PAGE, la Loi 076-84 est imprimée en intégralité (titre,
  préambule, Art. 1-3, signature du 7 décembre 1984).
- La PAGE SUIVANTE (page imprimée 16) reprend, SANS AUCUN NOUVEAU TITRE, la
  suite du texte de l'Ordonnance : la fin de son Art. 1er (répliques
  « Nouveau » des articles 97-106, 117, 119, 125 de la Constitution), PUIS
  son véritable Art. 2 (« Il est inséré... un Titre IV intitulé du Conseil
  constitutionnel »© et le nouveau texte des articles 86 à 92.
- Confirmé visuellement (rendu des 3 pages PDF) : il n'y a NULLE PART sur
  cette page un second titre de document — c'est la continuation physique de
  l'Ordonnance, imprimée après la Loi 076-84 pour des raisons de mise en
  page (probablement pour ne pas interrompre l'encadré de la loi de
  ratification, plus courte).
- Le découpage automatique du JO (`split_and_persist_journal_acts`) a
  rattaché à tort ces 20 nœuds (98, 99, 100, 101, 102, 103, 104, 105, 106,
  117, 119, 125, l'Art. 2 lui-même — renommé « 2_doublon_1 » par collision
  avec le véritable Art. 2 de la loi de ratification —, 86, 87, 88, 89, 90,
  91, 92) au document LOI 076-84 au lieu de l'ORDONNANCE 019-84, faute de
  marqueur de titre entre la fin de la loi et la suite de l'ordonnance.

Reconstruction : le texte complet et correctement ORDONNÉ de chaque document
est réassemblé à partir du markdown source PARTAGÉ (jamais modifié sur
MinIO), en réordonnant les tranches de lignes dans l'ordre logique des
articles (1er, 2, 3, 4) plutôt que l'ordre d'impression physique :
  - ORDONNANCE 019-84 = lignes[594:805] (préambule → Art.96, inchangé)
                       + lignes[832:905] (suite Art.1er + Art.2 + Art.86-92,
                         actuellement rattachée à tort à la Loi 076-84)
                       + lignes[805:818] (Art.3 + Art.4 + signature, inchangé)
  - LOI 076-84         = lignes[820:831] (son propre texte complet, inchangé)

Validation : les deux reparses ont été comparés article par article à la
base actuelle. Loi 076-84 : ses 3 articles + PREAMBULE + SIGNATURE sont
BYTE-IDENTIQUES ; les 20 articles retirés le sont bien tous. Ordonnance :
35 articles existants restent BYTE-IDENTIQUES, PREAMBULE et SIGNATURE aussi ;
les 20 articles récupérés apparaissent avec un contenu identique à ce qui
est actuellement (à tort) sous la Loi 076-84. Effet de bord bénin repéré (même
famille que la découverte « Article 1er » sur le Décret 59-178) : les deux
lignes de la table de renumérotation (« Art. 48, 49, 50... », « Art. 50,
51... ») ne sont plus captées comme deux faux articles « 48 »/« 50» — le
garde-fou anti-citation ajouté ce soir (commit fa31586) les reconnaît
maintenant comme des citations, pas des articles ; elles deviennent deux
feuilles `SANS_NUM_xxxxx` inoffensives (exclues du contrôle de séquence),
au lieu de `48_doublon_1`/`50`.

Aucune écriture MinIO : la correction ne vit qu'en base, sur LES DEUX
documents, dans LA MÊME transaction (l'un ne doit jamais être corrigé sans
l'autre, sous peine de dupliquer ou de perdre temporairement le contenu).

Usage (dev — défaut) :
    python scripts/fix_ordonnance019_loi076_rattachement.py                    # dry-run
    python scripts/fix_ordonnance019_loi076_rattachement.py --execute          # écrit sur le dev local

Usage (prod) :
    python scripts/fix_ordonnance019_loi076_rattachement.py --target prod                  # dry-run, profil PROD_RO_*
    python scripts/fix_ordonnance019_loi076_rattachement.py --target prod --execute        # écrit en PROD, profil PROD_RW_DB_*

Garde-fous : mêmes profils/vérifications que les scripts précédents (dump
frais requis avant tout --execute --target prod, saisie « PRODUCTION »
exigée). Avant toute écriture, le script revérifie que les 20 articles
attendus existent encore sous la Loi 076-84 et qu'aucun des deux documents
n'est publié — sinon il refuse.

Procédure d'annulation : `ingest_hierarchy` remplace structure_nodes/
articles/article_versions de CES DEUX documents (scope strict par
document_id). Un dump pris juste avant restaure l'état antérieur intégralement.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.api.main import ingest_hierarchy  # noqa: E402
from src.db.models import Article, ArticleVersion, CurationFlag, LegalDocument, MediaFile  # noqa: E402
from src.extractor.parser import LegalDocumentParser  # noqa: E402

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "5433")

ORDONNANCE_ID = "de681be4-6e9c-4673-8992-c0d5bacf50f8"
LOI_ID = "ff2895a1-3412-45bd-9a77-7ffedade93c7"

# Les 20 numéros actuellement rattachés à tort à la Loi 076-84, à retrouver
# sous l'Ordonnance après correction — sert de garde-fou avant écriture.
NUMEROS_A_DEPLACER = [
    "98", "99", "100", "101", "102", "103", "104", "105", "106",
    "117", "119", "125", "2_doublon_1", "86", "87", "88", "89", "90", "91", "92",
]

TEXTE_ORDONNANCE_CORRIGE = """# LE PRESIDENT DU COMITE CENTRAL DU PARTI CONGOLAIIS DU TRAVAIL, PRESIDENT DE LA REPUBLICQUE, CHEF DE L'ETAT, CHEF DU GOUVERNEMENT

Vu l'Acte no 84/061/PCT/BP/SCC/P du 10 août 1984 rendant exécutoires toutes les décisions du 3ème Congrès Ordinaire du Parti Congolais du Travail ;

Vu les nécessités de continuite de l'Etat ;

Le Conseil des Ministres entendu :

# ORDONNE:

Art. 1er - Les articles 3, 9, 12, 41, 43, 44, 45, 46, 47, 56, 59, 60, 61, 62, 63, 64, 65, 66, 67, 75, 76, 77, 78, 81, 82, 84, 88, 89, 93, 104 et 106 de la Constitution du 8 juillet 1979 sont modifiés ainsi qu'il suit :

# Première Partie

# PRINCIPES FONDAMENTAUX

# TITRE PREMIER

# DE LA REPUBLICIQUE POPULAIRE DU CONGO

Art. 3 (Nouveau).- En dehors des organes du Parti, les masses Populaires exercent le pouvoir au moyen des Conseils Populaires des régions, des districts, des communes et des arrondissements, organes décentralisés, et par l'Assemblée Nationale Populaire, organes du pouvoir d'Etat.

Ces organes sont élus librement par le Peuple depuis les Conseils Populaires des districts, arrondissements, communes et régions, jusqu'à l'Assemblée Nationale Populaire.

# TITRE II

# DES LIBERTES PUBLIQUES ET DE LA PERSONNE HUMAINE

Art. 9 (Nouveau).- Le secret des lettres et de toute autre forme de correspondance ne peut être violé, sauf en cas d'enquête criminelle, de mobilisation pour la défense de la Patrie, d'état de guerre.

Art. 12 (Nouveau).—Tous les Citoyens Congolais ayant l'âge de dix-huit ans ont la pleine capacité juridique et politique et doivent prendre part aux élections et peuvent être élus dans tous les organes du pouvoir de l'Etat, sauf si la loi en dispose autrement.

# Deuxieme Partie

# DUPOUVOIRPOPULAIRE

# TITRE PREMIER

# DE L'ORGANE SUPREME DUPOUVOIR DE L'ETAT. DE L'ASSEMBLEE NATIONALE POPULaire.

Art. 41 (Nouveau).-L'Assemblée Nationale Populaire est composée de députés, élus au suffrage Universal pour cinq ans

sur une liste Nationale arrêtée par le Comité Central du Parti Congolais du Travail, dans les conditions et propositions déterminées par la loi.

Cette liste comprend les représentants du Parti, les représentants des organisations de masse, les représentants de l'Armée Populaire Nationale, les délegués ouvriers, paysans, artistes et artisans.

Nonobstant les dispositions de l'alinea premier du present article, l'Assemblée Nationale Populaire élu au suffrage Universal reste en place jusqu'àux élections portant renouvellement de cette instance.

Art. 43 (Nouveau).—Les fonctions de député à l'Assemblée Nationale Populaire sont gratuites.

Toutefois, elles donnent droit au remboursement des frais de transport et à des indemnités de session dont les taux et les conditions d'attribution sont fixés par décret pris en Conseil des Ministres.

Art. 44 (Nouveau).—Trente jours après son élection, l'Assemblée Nationale Populaire se réunit de plein droit sous la presidence du député le plus âgé, assisté de deux plus jeunes députés qui assume les fonctions de Secrétaires.

Au cours de cette première seance, l'Assemblée Nationale Populaire procède à la verification, puis à la validation des mandats des représentants du peuple.

En cas de contestation, le Conseil Constitutionnel statue conformément à la loi électorale.

L'Assemblée Nationale Populaire élit ensuite son bureau comprenant :

un Président;  
- deux Vice-Préidents;  
-- deux Secréaires qui entrent immédiatement en fonction.

Art. 45 (Nouveau).-L'Assemblée Nationale Populaire redige et adopte un règlement interieur qui détermine son fonctionnement et fixe la procédure législate.

Art. 46 (Nouveau).—L'Assemblée Nationale Populaire vote seule la loi. Elle consent l'impôt et vote le budget de l'Etat et en contrôle l'exécution. Elle est saisie du projet du budget d'es l'ouverture de la session de novembre.

# Elle a également pour attribution de :

-- approvouver les lignes générales des politiques intérieures et extérières;  
- approuver l'établissement et la modification des circsscriptions territoriales;  
- Constituer les commissions de l'Assemblée Nationale Populaire;  
- annuler l'élection ou la désignation des personnes élues ou désignées par elle;  
exercer le contrôle sur les organes de l'Etat et du Gouvernement ;  
- organiser les référendums dans les cas prévus par la Constitution et dans ceux ou l'Assemblée les jugerait opportun après consultation du Comité Central du Parti Congolais du Travail.

Art. 47 (Nouveau).—Sont du domaine de la loi, les règles concernant:

- les droits civiques et les garanties fondamentales accordées aux citoyens pour l'exercice des libertés publiques;  
— les sujetions imposées aux citoyens dans leurs personnes ou leurs biens dans l'intérêt de la défense nationale;  
- la détermination des crimes et déliits ainsi que les peines qui les sanctionnent, l'amnistie, la procédure pénale;  
- l'assiette, le taux des impôts et taxes de toute nature, le régime d'émission de la monnaie;  
- la nationalité, l'etat et la capacité des personnes, les régimes matrimoniales et les successions;  
- l'expression du suffrage populaire pour l'élection des organes de l'Etat, des collectivités décentralisées et pour les référenda;  
- la ratification des conventions ettraités internationaux;  
- la création des catégories d'établissements publics ;  
- la création des établissements publics et des entreprises d'Etat ;  
- le droit des obligations, leslibéralités,lesdroits réels,les sure-tés,la procédure devant les juridictions civiles;  
- le domaine public et privé de l'Etat, le domaine populaire et l'utilisation des terres;  
- le statut des officiers et fonctionnaires publics, le statut de la magistrature, la législation du travail et de la prévoyance sociale;  
- la participation de l'Etat, des collectivités décentralisées, des établissements publics au capital des sociétés de droit privé;  
- les conditions, la procédure et l'évaluation des indemnisations en cas de nationalisation ou d'expropriation;  
- le plan de développement économique et social ;  
- l'organisation administrative et judiciaire ;  
- l'organisation de la défense nationale, des transports publics, des télécommunications, de l'enseignement et de la santé ;  
- les nationalisations.

Art. 48 (Nouveau).- Les matières autres que celles qui sont du domaine de la loi, ont un caractère réglementaire.

Art. 49 (Nouveau).- Le Président de la République, Chef de l'Etat, Chef du Gouvernement peut, pour l'execution des tâches économiques notamment dans les matières dont le traitement requiert une urgence, demander à l'Assemblée Nationale Populaire, l'autorisation de prendre par Ordonnance, pendant un début limité à deux ans des mesures qui sont normalement du domaine de la loi

Ces Ordonnances sont prises en Conseil des Ministres après avis du Bureau de l'Assemblée Nationale Populaire et du Conseil Constitutionnel. Les Ordonnances du Président de la République, Chef de l'Etat, Chef du Gouvernement, prises dans le cadre de cette délegation sont réputées ratifiées. Si, à l'expiration du-delai de deux ans, le Gouvernement ne demande pas ou n'obtient pas, le renouvellement de la délegation, celle-ci devient caduque.

Art. 57 (Nouveau).- L'urgence de vote d'une loi peut être demandée par l'un des organes visés à l'article 50 de la Constitution. -

Lorsqu'elle est demandée, l'Assemblée se prononce sur cette urgence à la majorité simple.

Art. 58 (Nouveau).- Les moyens d'information et de contrôle de l'Assemblée Nationale Populaire à l'égard de l'action du Gouvernement sont :

- la question orale ;  
- la question écrité ;  
- l'audition en commission ;  
- la Commission d'enquête.

Art. 61 (Nouveau).- Le Président de la République, Chef de l'Etat, Chef du Gouvernement, promulgue les lois dans les vingt jours de leur transmission par le Président de l'Assemblée Nationale Populaire.

Elles sont publiées au Journal Officiel de la République Populaire du Congo. La promulgation faite par le Président de la République sera connue à Brazzaville un jour après et dans chaque des Régions quinze jours après la date de promulgation.

Art. 62 (Nouveau).- Le Président de la République, Chef de l'Etat, Chef du Gouvernement, ouvre les sessions de l'Assemblée Nationale Populaire. Il déclare la clôture des sessions ordinaires sur proposition du Bureau de l'Assemblée et celle des sessions extraordinaires d'es que l'Assemblée a épuisé son ordre du jour.

# TITRE II

# DUPRESIDENTDELA REPUBLICQUE

Art. 63 (Nouveau).— Le Président du Comité Central du Parti Congolais du Travail, Président de la République est élu Président de la République pour cinq ans par le Congrès du Parti Congolais du Travail.

Il est investi Président de la République, Chef de l'Etat, Chef du Gouvernement par l'Assemblée Nationale Populaire.

Art. 66 (Nouveau).- Le Président de la République, après consultation du Premier Ministre nomme les autres membres du Gouvernement et met fin à leurs fonctions.

Art. 67 (Nouveau).— En cas de vacance de la République pour quelques cause que ce soit ou d'empêchement constaté par un plénum réunissant les Membres du Comité Central, de l'Assemblée Nationale Populaire et du Conseil Constitutionnel, les fonctions de Président de la République, Chef de l'Etat, Chef du Gouvernement à l'excection des pouvoirs prévus aux articles 65, 66, 68, 71, 72, 73, 74, 75 sont provisoirement exerçées par le Président de l'Assemblée Nationale Populaire.

Le Président de l'Assemblée Nationale Populaire assurant l'intérim, ne peut être élu Président de la République.

Le Congrès du Parti Congolais du Travail est convoqué dans les 45 jours suivant la vacance.

Art. 68 (Nouveau).- Lors de son entree en fonction, le Président de la République prête solennellement, devant le plénum du Comité Central, de l'Assemblée Nationale Populaire et du Conseil Constitutionnel, le serment suivant :

«Je jure fidélité au peuple Congolais, à la Révolution et au Parti Congolais du Travail. Je m'engage, en me guidant des principes marxistes-léninistes à défendre les statuts du Parti et la Constitution, à consacrer toutes mes forces au triomphe des ideaux prolétariens du Peuple Congolais dans le Travail, la Démocratie et la Paix».

Art. 69 (Nouveau).- Le Conseil Constitutionnel prend acte de la prestation de serment et en dresse le procés-verbal.

# TITRE III (NouvEAU)

# DU Gouvernement

Art. 77 (Nouveau).- Le Gouvernement est l'organe exécutif supérieur. Il est chargé de l'exécution des tâches politiques, économiques, sociales et culturelles qui lui sont confiées par les lois. Il exerce le pouvoir réglementaire.

Art. 78 (Nouveau).- Le Gouvernement comprend :

- le Président de la République, Chef du Gouvernement;  
-- le Premier Ministre ;  
- les Ministres.

Le Président de la République, Chef de l'Etat, Chef du Gouvernement, preside le Conseil des Ministres.

Art. 79 (Nouveau):— Sous l'autorité du Président de la République, Chef de l'Etat, Chef du Gouvernement, le Premier Ministre dirige, coordonne, contrôle l'action des Ministres et rend compte au Président de la République devant lequel il est responsable.

Les Ministres sont placés sous l'autorité hierarchique directe du Premier Ministre.

Leur responsabilité est engagée devant le Président de la République, Chef de l'Etat, Chef du Gouvernement, sur rapport du Premier Ministre.

LePremier Ministre est investi du pouvoir reglementaire.

Il prend des décretés et des arrêtés dans le cadre de l'application des lois. Il nomme, par délegation du Président de la République aux employés civils de l'Etat.

Art. 80 (Nouveau).- L'organisation interne des Ministères et des Institutions du Gouvernement est fixée en Conseil des Ministres.

Art. 81 (Nouveau).— Chaque Ministre est responsable du bon fonctionnement de son Ministère. Il y exerce par voie d'arrêts le pouvoir reglementaire et procèle notamment aux nominations et affectations des agents de son département sous réserve des dispositionsPrevues à l'article 74.

Art. 82 (Nouveau).- Les Fonctions de Membres du Gouvernement sont incompatibles avec l'exercice de tout mandat parlementaire et de toute activité retribuée.

Art. 83 (Nouveau.- Dans le cadre de ses attributions prévues à l'article 77, le Gouvernement est chargé notamment de :

- organiser et diriger l'exécution des actes politiques, économiques, culturels, scientifiques, sociaux et de défense, adoptés par l'Assemblée Nationale Populaire ;  
-- proposer les projets des plans généraux de développement économique et social de l'Etat et après l'approbation par l'Assemblée Nationale Populaire, organiser et coordonnier leur exécution;

- diriger la politique interieure et extérieure de la République et les relations avec les autres Gouvernements;  
- approuver les traits internationaux et les soumettre à la procédure de ratification;  
- diriger et contrôle le commerce interieur et extérieur;  
- élaborer le projet de budget de l'Etat et une fois celui-ci approuvé par l'Assemblée Nationale Populaire, procédér à son exécution ;  
assurer à la Défense Nationale le maintien de l'ordre et la sécurité dans le pays et la protection des droits des citoyens ainsi que la sauvegarde des vies humaines et des biens en cas de catastrophe naturelle;  
- diriger l'administration de l'Etat en unifiant et en coordonnant l'activité des Ministères et autres organismes centraux de l'administration;  
-- exécuter les lois et les traitsés;  
- accorder le droit d'asile;  
-- appliquer les directives du Parti relatives à l'organisation générale des forces armées révolutionnaires;  
exercer la direction et le contrôle politique et technique des fonctions administratives et organismes centraux correspondants;  
- requérir l'annulation par le Conseil Constitutionnel des dispositions adoptées par les Assemblées et organismes locaux du pouvoir populaire en violation des lois et règlements en vigueur;  
- creer les Commissions qu'il estime nécessaire en vue de faci- literl'execution des taches qui lui sont assignees;  
- nommer aux divers emplois civils et militaires ;  
- démettre de leurs fonctions après avis des instances démocratiques appropriées, les fonctionnaires responsables de fautes lourdes dans l'exercice de leurs fonctions;  
- s'acquitter de toute autre fonction qui lui serait confiée par l'Assemblée Nationale Populaire ;  
- prendre des dispositions nécessaires pour l'organisation des référendums décidés par le Comité Central du Parti Congolais du Travail.

Art. 84 (Nouveau).- Les actes du Gouvernement sont signés par le Président de la République, Chef de l'Etat, Chef du Gouvernement, et sont contrésignés par le Premier Ministre ainsi que par les Ministres charges de leur exécution.

Art. 85 (Nouveau).- Le Gouvernement rend compte de ses activités à l'Assemblée Nationale Populaire.

Il s'acquitte de toute autre fonction qui lui est confiée par l'Assemblée Nationale Populaire.

# TITRE V (NouvEAU)

# DES-ORGANES LOCAUX DU POUVOIR POPULAIRE

Art. 97 (Nouveau).— En République Populaire du Congo, les régions, communes, arrondissements et districts sont des collectivités locales décentralisées, dotées de la personnalité morale et de l'autonomie financière.

Art. 93 (Nouveau).— Si, devant une juridiction quelconque, une partie souLEVè une exception d'inconstitutionnalité, cette juridiction surseoit à statuer et partir à cette partie un-delai d'un mois pour saisir le Conseil Constitutionnel.  
Art. 94 (Nouveau).- Les décisions du Conseil Constitutionnel ne sont susceptibles d'aucun recours. Elles s'imposent aux pouvoirs publics et à toutes les autorités administratives, et juridictionnelles.  
Art. 95 (Nouveau).—Une disposition, déclarée inconstitutionnelle, ne peut être promulgée, ni mise en application.  
Art. 96 (Nouveau).- La loi déterminé les régles d'organisation et de fonctionnement du Conseil Constitutionnel, la procédure à suivre et, notamment, les délays ouverts pour la saisine en cas de contestation.  
Art. 98 (Nouveau).- Les collectivités locales définies à l'article 97 sont administrées par leurs propres organes, les Conseils Populaires et leurs émanations dont l'organisation, le fonctionnement et la compétence sont déterminés par la loi.

Art. 99 (Nouveau).— La loi déterminé le régime spécial d'administration applicable aux collectivités locales en cas de dissolution ou d'impossibilité, d'établier les Conseils Populaires et leurs émanations.

Art. 100 (Nouveau).- La loi déterminé le mode d'élection par le peuple des organes élus des collectivités locales.

# TITRE VI (NOUVEAU)

# DES JURIDICKIONS NATIONALES POPULAIRES

Art. 101 (Nouveau).- La Justice Populaire est rendue au nom du Peuple Congolais par la Cour Supreme, la Cour des Comptes, les Tribunaux Populaires de régions ou de commune, les Tribunaux Populaires de district ou d'arrondissement, les Tribunaux Populaires de village-centre ou de quartier, les Tribunaux Militaires et les Tribunaux institués par la loi.

En cas de nécessité et pour juger des'affaires spéciales, l'Assemblée Nationale Populaire sur proposition du Gouvernement peut decide de la création des Tribunaux spéciaux après avis du Comité Central.

Art. 102 (Nouveau).- L'organisation, le fonctionnement, la compétence des Cours et Tribunaux sont déterminés par la loi.

Art. 103 (Nouveau).- Les Cours et Tribunaux fonctionnent de manière collégiale.

Art. 104 (Nouveau).- La Justice est rendue par ces juridictions composées des magistrats professionnels assistés des juges élus par les Assemblées locales.

Art. 105 (Nouveau).— Au moment où ils rendent leur décid tion, les juges n'obéissent qu'a la loi.

Art. 106 (Nouveau).- La Cour Supremé est la haute juridiction de la République Populaire du Congo. Ses décisions sont définitives. Elle contrôle l'activité juridictionnelle des Cours et Tribunaux. Elle émet des avis sur les projets de textes réglementaires qui lui sont soumis.

# TITRE VII (NouvEAU)

# DE L'ARMEE POPULAIRE NATIONALE

# TITRE VIII (NouvEAU)

# DES TRIITES INTERNATIONAUX

Art. 117 (Nouveau).— A l'exception du Président de la République, Chef du Gouvernement, tout représentant de l'Etat Congolais pour l'adoption, l'authentication d'un engagement international doit produit des pleins pouvoirs appropriés.

Art. 119 (Nouveau).— Si le Conseil Constitutionnel, saisi par un des organes supérieurs d'Etat visés à l'article 50, a déclaré qu'un engagement conventionnel comporte une clause violant une norme constitutionnelle, il émet un avis de non ratification ou, s'il est déjà en vigueur, constate son inconstitutionnalité.

# TITRE IX (NOUVEAU)

# DE LA REVISION DE LA CONSTITUTION

# TITRE X (NouvEAU)

# DISPOSITIONS TRANSITOIRS

Art. 125 (Nouveau).- Les attributions conférées au Conseil Constitutionnel par la présente Constitution seront exerçées, jusqu'à la mise en place de ce Conseil, par la Cour Supreme.

Art. 2.- Il est inséré dans la Constitution du 8 juillet 1979, un Titre IV intitulé du Conseil constitutionnel et libellé ainsi qu'il suit:

# TITRE I (NOUVEAU)

# DU CONSEIL CONSTITUTIONNEL

Art. 86 (Nouveau).- Le Conseil Constitutionnel comprend huit membres dont le mandat dure six ans. Quatre des membres sont nommés par le Président de la République, quatre sont élus par l'Assemblée Nationale Populaire.

L'élection des membres du Conseil Constitutionnel par l'Assemblée Nationale Populaire est inattaqueable.

Art. 87 (Nouveau).- Le Président du Conseil Constitutionnel est nommé par le Président de la République. Il a voix préponérante en cas de partage de voix. Le Président du Conseil Constitutionnel est choisi parmi les membres nominés ou élus.

Art. 88 (Nouveau).— La qualité de membre du Conseil Constitutionnel est incompatible avec celle de Ministre, de Députe ou de Conseiller Populaire de Région, de Commune, de District ou de Poste de Contrôle Administratif. Les autres incompatibilités sont fixées par la loi.

Art. 89 (Nouveau).- Lestraités,lesloisavantleurratification ou leur adoption par l'Assemblée Nationale Populaire,peuventetre soumis,pouravis,parleGouvernement au Conseil Constitutionnel qui seprononce sur leur conformité à la Constitution.

Art. 90 (Nouveau).- Le Conseil Constitutionnel statue, en cas de contestation, sur la régularité de l'élection des Députés et des Conseillers Populaires.

Art. 91 (Nouveau).- Le Conseil Constitutionnel veille à la régularité des opérations de réferendum.

Art. 92 (Nouveau).— Les règlements interieurs de l'Assemblée Nationale Populaire, des Conseils Populaires doivent, avant leur mise en application, être soumis au Conseil Constitutionnel qui se pronounce sur leur conformité à la Constitution.

Aux mêmes fins, les lois, avant leur promulgation et tout acte de valeur législateve, avant publication, peuvent etre deferés au Conseil Constitutionnel par le Président de la République ou le Président de l'Assemblée Nationale Populaire ou un tiers des Députés.

Dans les cas prévus aux deux alineas précédents, le Conseil Constitutionnel doit statuer dans le délambda d'un mois. Toutefois, à la demande expresse du requerant, ce délambda peut être ramené à dix jours, s'il y a urgence.

Dans ces mêmes cas, la saisine du Conseil Constitutionnel suspend le début de promulgation ou de publication.
Art. 3.- Les numérations suivantes de la Constitution du 8 juillet 1979 sont modifiées ainsi qu'il suit :

# AU LIEU DE:

# LIRE RESPECTIVEMENT :

Art. 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110 et.111.  
Art. 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 83, 84, 85, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123 et 124.  
Art. 4.- La presente Ordonnance, qui abroge toutes dispositions constitutionnelles antérieures contraires, sera enregistrée et publiée au Journal Officiel, comme loi supérieur de l'Etat, et entre en vigueur immédiatement, selon la procédure d'urgence.

Fait à Brazzaville, le 23 ao ut 1984

COLONEL DENIS SASSOU-NGUESSO"""

TEXTE_LOI076_CORRIGE = """L'ASSEMBLEE NATIONALE POPULAIRE A DELIBERE ET ADOPE:

LE PRESIDENT DU COMITE CENTRAL DU PARTI CONGOLAIS DU TRAVAIL, PRESIDENT DE LA REPUBLIQUE, CHEF DE L'ETAT, CHEF DU GOUVERNEMENT, PROMULGUE LA LOI DONT LA TENEUR SUIT :

Art. 1er - Est ratifiée l'Ordonnance no 019-84 du 23, août 1984, portant modification de Certaines dispositions de la Constitution du 8 juillet 1979.  
Art. 2. - Le texte de ladite Ordonnance sera annexé à la presente loi.  
Art. 3. - La présente loi sera publiée au Journal Officiel de la République Populaire du Congo.

Fait à Brazzaville, le 7 décembre 1984

COLONEL DENIS SASSOU-NGUESSO"""


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

    ordonnance = db.query(LegalDocument).filter(LegalDocument.id == ORDONNANCE_ID, LegalDocument.deleted_at.is_(None)).first()
    loi = db.query(LegalDocument).filter(LegalDocument.id == LOI_ID, LegalDocument.deleted_at.is_(None)).first()
    if ordonnance is None or loi is None:
        raise SystemExit("Refus : l'un des deux documents est introuvable ou supprimé.")
    if ordonnance.curation_status == "published" or loi.curation_status == "published":
        raise SystemExit("Refus : l'un des deux documents est publié, hors garde-fou sans validation humaine.")

    numeros_presents = {
        r[0]
        for r in db.query(Article.numero_article)
        .filter(Article.document_id == LOI_ID, Article.numero_article.in_(NUMEROS_A_DEPLACER))
        .all()
    }
    manquants = set(NUMEROS_A_DEPLACER) - numeros_presents
    if manquants:
        raise SystemExit(
            f"Refus : {len(manquants)} numéro(s) attendu(s) sous la Loi 076-84 sont introuvables ({sorted(manquants)}) — "
            "l'état a changé depuis le diagnostic, ne pas appliquer ce correctif tel quel."
        )
    print(f"Garde-fou : les {len(NUMEROS_A_DEPLACER)} articles attendus sont bien présents sous la Loi 076-84.")

    hierarchy_ord = LegalDocumentParser(text_content=TEXTE_ORDONNANCE_CORRIGE).parse_hierarchy()
    hierarchy_loi = LegalDocumentParser(text_content=TEXTE_LOI076_CORRIGE).parse_hierarchy()
    articles_ord = flatten_articles(hierarchy_ord)
    articles_loi = flatten_articles(hierarchy_loi)
    numeros_ord = [a["number"] for a in articles_ord]
    numeros_loi = [a["number"] for a in articles_loi]

    if len(articles_loi) != 3 or numeros_loi != ["1er", "2", "3"]:
        raise SystemExit(f"Refus : reparse Loi 076-84 inattendu — {len(articles_loi)} articles {numeros_loi}, attendu 3 (1er, 2, 3).")
    manquants_ord = [n for n in ("98", "99", "100", "101", "102", "103", "104", "105", "106", "117", "119", "125", "2", "86", "87", "88", "89", "90", "91", "92") if n not in numeros_ord]
    if manquants_ord:
        raise SystemExit(f"Refus : le reparse de l'Ordonnance ne contient pas tous les numéros attendus, manquants : {manquants_ord}.")
    print(f"Reparse validé : Ordonnance {len(articles_ord)} articles (dont les 20 récupérés), Loi 076-84 {len(articles_loi)} articles.")

    md_media_ord = db.query(MediaFile).filter(MediaFile.document_id == ORDONNANCE_ID, MediaFile.file_category == "EXTRACTION_MARKDOWN").first()
    md_media_loi = db.query(MediaFile).filter(MediaFile.document_id == LOI_ID, MediaFile.file_category == "EXTRACTION_MARKDOWN").first()

    articles_avant_ord = db.query(Article).filter(Article.document_id == ORDONNANCE_ID, Article.deleted_at.is_(None)).count()
    articles_avant_loi = db.query(Article).filter(Article.document_id == LOI_ID, Article.deleted_at.is_(None)).count()
    flags_avant_ord = db.query(CurationFlag).filter(CurationFlag.document_id == ORDONNANCE_ID, CurationFlag.resolved.is_(False)).count()
    flags_avant_loi = db.query(CurationFlag).filter(CurationFlag.document_id == LOI_ID, CurationFlag.resolved.is_(False)).count()

    print(f"\nCible : {args.target}.")
    print(f"Avant — Ordonnance : {articles_avant_ord} articles, {flags_avant_ord} flags ouverts")
    print(f"Avant — Loi 076-84 : {articles_avant_loi} articles, {flags_avant_loi} flags ouverts")

    if not args.execute:
        print("\nDRY-RUN : aucune écriture. Relancer avec --execute pour appliquer.")
        return

    if args.target == "prod":
        print("\n  Ces deux documents PROD vont voir leur structure reconstruite (20 articles déplacés de la Loi 076-84 vers l'Ordonnance 019-84).")
        saisie = input("Taper PRODUCTION pour confirmer : ").strip()
        if saisie != "PRODUCTION":
            print("Annulé.")
            sys.exit(1)
        db = _session_prod_ecriture(engine_ro)
        ordonnance = db.query(LegalDocument).filter(LegalDocument.id == ORDONNANCE_ID, LegalDocument.deleted_at.is_(None)).first()
        loi = db.query(LegalDocument).filter(LegalDocument.id == LOI_ID, LegalDocument.deleted_at.is_(None)).first()
        md_media_ord = db.query(MediaFile).filter(MediaFile.document_id == ORDONNANCE_ID, MediaFile.file_category == "EXTRACTION_MARKDOWN").first()
        md_media_loi = db.query(MediaFile).filter(MediaFile.document_id == LOI_ID, MediaFile.file_category == "EXTRACTION_MARKDOWN").first()

    ordonnance.metadata_ = {**(ordonnance.metadata_ or {}), "correction_rattachement_loi076": True}
    loi.metadata_ = {**(loi.metadata_ or {}), "correction_rattachement_ordonnance019": True}
    # Les deux documents sont reconstruits dans LA MÊME transaction (pas de commit entre les
    # deux) : soit les deux réussissent, soit aucun n'est modifié.
    ingest_hierarchy(db, loi, hierarchy_loi, run_id=None, media_id=md_media_loi.id if md_media_loi else None, validation_status="pending")
    ingest_hierarchy(db, ordonnance, hierarchy_ord, run_id=None, media_id=md_media_ord.id if md_media_ord else None, validation_status="pending")
    db.commit()

    articles_apres_ord = db.query(Article).filter(Article.document_id == ORDONNANCE_ID, Article.deleted_at.is_(None)).count()
    articles_apres_loi = db.query(Article).filter(Article.document_id == LOI_ID, Article.deleted_at.is_(None)).count()
    flags_apres_ord = db.query(CurationFlag).filter(CurationFlag.document_id == ORDONNANCE_ID, CurationFlag.resolved.is_(False)).count()
    flags_apres_loi = db.query(CurationFlag).filter(CurationFlag.document_id == LOI_ID, CurationFlag.resolved.is_(False)).count()
    print(f"\n--execute ({args.target}) : modifications validées (COMMIT).")
    print(f"Après — Ordonnance : {articles_avant_ord} → {articles_apres_ord} articles, flags {flags_avant_ord} → {flags_apres_ord}")
    print(f"Après — Loi 076-84 : {articles_avant_loi} → {articles_apres_loi} articles, flags {flags_avant_loi} → {flags_apres_loi}")


if __name__ == "__main__":
    main()
