---
title: "Modèle de dispatch européen"
subtitle: "Hypothèses de modélisation et contraintes d'exploitation — étape (vi), formation des prix spot horaires zonaux"
date: "Août 2026"
lang: fr-FR
---

# Objet

Ce document décrit les hypothèses retenues pour produire les prix spot horaires zonaux : le périmètre, la formulation du programme linéaire, les données d'entrée, les coûts, et le détail des rigidités d'exploitation. **La section 7 définit une à une les variables du module de flexibilité nucléaire.**

---

# 1. Périmètre et principe

Le modèle simule le dispatch économique d'un parc européen interconnecté et en déduit les prix horaires zonaux. **Le prix d'une zone est le dual de sa contrainte d'équilibre offre-demande** : il n'est pas postulé, il résulte de l'optimisation.

La sortie native est un coût marginal système (SMC). Une étape (vii) distincte applique une prime calibrée pour passer du SMC au prix spot observé ; les chiffres cités ici sont au niveau SMC.

## Zones

Treize zones : FR, DE-LU, BE, GB, CH, IT-Nord, IT-Sud, ES, PT, NL, DK, PL-CZ, AT-SI.

- La France est résolue **tranche par tranche** (parc réel, réacteur par réacteur).
- Les autres zones sont agrégées en blocs technologiques, le thermique étant découpé en sous-blocs de rendement pour reconstituer une pente d'ordre de mérite.
- PL-CZ, AT-SI et DK sont des zones virtuelles regroupant plusieurs zones de dépôt d'offres.

## Découpage temporel

Résolution par **fenêtres hebdomadaires glissantes**, 52 par année simulée.

Le programme est un **LP pur** : aucune variable binaire, l'engagement des groupes est relaxé en continu. Ce choix est structurant — il garantit l'existence des duaux, donc des prix, mais il autorise des engagements fractionnaires qu'un modèle en nombres entiers interdirait.

---

# 2. Formulation du programme linéaire

Pour chaque zone *z* et chaque heure *t* :

```
Σ gen + res + ens − dump + imports  =  demande        (dual = prix)
0 ≤ flux_aller ≤ NTC(a→b)  ,  0 ≤ flux_retour ≤ NTC(b→a)
gen_min ≤ gen ≤ capacité × disponibilité
```

Les flux sont **dirigés et non négatifs**, avec un coût infinitésimal sur le flux brut afin d'éliminer les boucles de transit dégénérées (deux flux opposés simultanés de même ampleur). `ens` est l'énergie non servie, `dump` l'écrêtement.

## Défaillance et effacement

Trois tranches escaladent le prix sous le plafond, exprimées en fraction de la pointe de la fenêtre :

| Tranche                | Part de la pointe | Prix (€/MWh) |
|:-----------------------|:-----------------:|-------------:|
| Effacement diffus      | 3 %               | 300          |
| Effacement industriel  | 3 %               | 1 000        |
| Pénurie                | 5 %               | 4 000        |
| **Plafond (VoLL)**     | —                 | **15 000**   |

Ces tranches absorbent aussi la sous-modélisation résiduelle des pointes, ce qui évite qu'un épisode de froid touche mécaniquement le plafond.

---

# 3. Coûts et ordre de mérite

```
SRMC  =  combustible / η  +  (intensité CO₂ / η) × prix EUA  +  VOM
```

Les rendements électriques sont tirés d'une plage par technologie ; chaque bloc thermique est réparti sur cette plage pour produire une **dispersion** de coûts au lieu d'un prix unique.

| Technologie             | Rendement min | Rendement max | VOM (€/MWh) |
|:------------------------|:-------------:|:-------------:|------------:|
| CCGT                    | 0,46          | 0,60          | 2,5         |
| Gaz (générique)         | 0,40          | 0,58          | 2,5         |
| Turbine à combustion    | 0,34          | 0,42          | 3,5         |
| Charbon                 | 0,36          | 0,46          | 3,5         |
| Lignite                 | 0,35          | 0,43          | 3,5         |
| Fioul                   | 0,30          | 0,40          | 4,0         |
| Biomasse                | 0,28          | 0,38          | 4,0         |
| Nucléaire               | —             | —             | 9,0         |
| Hydraulique réservoir   | —             | —             | 1,0         |

Un bloc d'adéquation (batteries, effacement, pointe hydrogène à horizon 2040) est offert à **300 €/MWh**. Cette valeur est posée explicitement, elle n'est pas ajustée sur les résultats.

## Capacité disponible

- **Zones voisines** : capacité installée × facteur de dérating ; à défaut, quantile **p99,9** de la production observée. Le p99 a été essayé et rejeté — il sous-dimensionne les pointes et fabrique une pénurie qui n'existe pas.
- **Nucléaire français** : maximum glissant sur 72 h de la production observée, majoré de 3 %, ou flux d'indisponibilités REMIT lorsqu'il est disponible.
- Disponibilité thermique 0,95 ; biomasse 0,85 ; réservoir 1,0 (la contrainte est l'énergie, pas la puissance).

---

# 4. Hydraulique

L'hydraulique de réservoir est offerte à un prix proche de zéro mais soumise à un **budget énergétique hebdomadaire égal à la production réservoir réellement observée** sur la fenêtre.

Conséquence : l'énergie hydraulique est ancrée sur l'histoire, mais le LP la place librement dans la semaine. **La valeur de l'eau est donc endogène** — elle est le dual du budget.

- Courbes de valeur d'eau empiriques, saisonnalisées (les mois d'irrigation sont traités à part).
- Option de recentrage des offres hydrauliques sur le λ structurel d'une programmation dynamique stochastique : le niveau vient de Bellman, la dispersion reste empirique.
- Le fil de l'eau est en production fatale. Les STEP sont **exclues du dispatch** : l'arbitrage de stockage est un raffinement non traité.

---

# 5. Production fatale et prix négatifs

Solaire, éolien, fil de l'eau et déchets sont en **must-take**, bornés par le potentiel horaire. Le comportement à prix négatif dépend du régime de soutien de chaque cohorte.

- **Schémas de soutien millésimés** : obligation d'achat, complément de rémunération, merchant. Chaque cohorte est indexée par son année de mise en service, si bien que la part « payée quoi qu'il arrive » décroît au fil du temps.
- **Déclencheur §51 EEG** (et règle CfD 6 heures pour GB et l'Italie) : après N heures *consécutives* de prix négatif, le soutien cesse. C'est un point fixe, re-résolu à l'intérieur de la fenêtre.
- **Planchers must-run mesurés** : p10 de la production observée par technologie et par mois, appliqués à DE-LU et ES en remplacement d'une heuristique cogénération × facteur thermique qui surestimait largement le socle forcé.

Le résultat structurel attendu est un **croisement** : le nombre d'heures négatives croît avec le développement des renouvelables, tandis que la profondeur des prix négatifs s'atténue à mesure que le stock de contrats payés quoi qu'il arrive s'éteint.

---

# 6. Interconnexions

- **NTC horaire publiée** par les gestionnaires de réseau lorsqu'elle existe.
- À défaut, **NTC dérivée du flux réalisé** : p99,5 par frontière et par sens, multiplié par un **facteur de coïncidence** propre à chaque zone. Ce facteur est nécessaire parce que le p99,5 mesure l'*usage* et non la *capacité* : les frontières d'une même zone ne saturent pas au même moment, donc leur somme surestime l'export simultané physiquement atteignable.
- **Réallocation à total constant** pour la Suisse : ses frontières sont ramenées à leurs proportions physiques sans toucher au total simultané.
- **Plafonds de régime de surplus** : sur les heures de faible résidu de l'exportateur, la capacité est ramenée au p95 du flux observé sur ces heures. Le domaine d'échange se rétracte quand le renouvelable est fort — anti-corrélation qu'un plafond statique ne peut pas représenter.

En projection, les interconnexions évoluent par **marches discrètes** : une ligne par projet avec son année de mise en service, sommée en escalier et jamais interpolée, appliquée en delta MW sur la série horaire de l'année de référence. Un câble est un actif discret, pas une trajectoire lisse.

---

# 7. Flexibilité nucléaire française

Ce module est le cœur de la formation des prix bas en France. Lorsqu'il est actif, le parc nucléaire n'est **pas** réduit à une courbe d'offre agrégée : les rigidités agissent sur des **réacteurs individuels**, car une tranche agrégée n'a ni état de couplage, ni position dans son cycle de combustible, ni démarrage à payer.

## 7.1 Le mécanisme — d'où viennent les prix négatifs

Point essentiel : **aucune offre négative n'est jamais écrite dans le modèle.** Les offres nucléaires sont plafonnées par le bas au coût du combustible. Le prix négatif émerge de la contrainte :

1. Un réacteur couplé doit produire au moins sa bande normale (contrainte **C1**).
2. Descendre sous cette bande — la « modulation profonde » — coûte `c_mod` par MWh.
3. L'offre implicite du réacteur pour conserver son MWh marginal vaut donc `SRMC − c_mod`, quantité qui peut être négative.

**Autrement dit, le prix négatif est une conséquence mesurée de la rigidité physique, pas une hypothèse de comportement.** C'est ce qui rend le résultat falsifiable.

## 7.2 Variables de décision (par réacteur, par heure)

| Symbole | Nom | Unité | Signification |
|:--------|:----|:-----:|:--------------|
| `p` | Puissance | MW | Puissance électrique effectivement produite par le réacteur sur l'heure. |
| `u` | Capacité engagée | MW | Part de la capacité du réacteur **couplée au réseau**. Relaxée en continu (LP pur) : `u = 0` signifie réacteur découplé, `u = capacité` signifie pleinement engagé. C'est `u`, et non `p`, que les contraintes de démarrage et de bande prennent pour référence. |
| `d` | Modulation profonde | MW | **Enfoncement sous le plancher de bande normale.** `d > 0` signifie que le réacteur produit en dessous de `α_band × u`. C'est la variable coûteuse : elle porte `c_mod` et alimente l'effet xénon. |

## 7.3 Paramètres physiques par classe de réacteur

| Symbole | Nom | Contrainte | Signification |
|:--------|:----|:----------:|:--------------|
| `α_band` | Plancher de bande | C1 | Plancher d'exploitation normale, en fraction de la capacité engagée : `p ≥ α_band × u − d`. Le réacteur descend jusque-là sans pénalité. |
| `α_tech` | Minimum technique | C1 | Minimum absolu atteignable, en fraction. Borne la modulation profonde : `d ≤ (α_band − α_tech) × u`. L'écart entre les deux α est donc la **profondeur de modulation disponible**. |
| `r_up` | Rampe à la hausse | C3 | Hausse horaire maximale, en fraction de la capacité. |
| `r_down` | Rampe à la baisse | C3 | Baisse horaire maximale. Plus généreuse que la montée : un réacteur descend plus facilement qu'il ne remonte. |
| `β` | Pénalité xénon | C3 | Pénalité de rampe à la hausse par unité de profondeur de modulation **récente**. Représente l'empoisonnement au xénon 135 : après un enfoncement, le réacteur ne peut pas remonter librement. Contrainte : `r_up × u − β × Σ(d sur 8 h) ≥ 0`. |
| `d_max_8h` | Budget 8 h | C2a | Budget énergétique de modulation profonde sur 8 heures glissantes, en fraction de capacité × heure. |
| `d_max_day` | Budget journalier | C2b | Même chose à l'échelle du jour. Les deux budgets ensemble empêchent une modulation profonde permanente que la physique du combustible n'autorise pas. |
| `ρ_recommit` | Rampe de re-couplage | C5 | Hausse horaire maximale de la **capacité engagée** après un arrêt. Traduit le fait qu'un réacteur ne se recouple pas instantanément. |

### Valeurs retenues par palier

| Classe   | `α_band` | `α_tech` | `r_up` | `r_down` | `β`  | `d_max_8h` | `d_max_day` | `ρ_recommit` |
|:---------|:--------:|:--------:|:------:|:--------:|:----:|:----------:|:-----------:|:------------:|
| 900 MW   | 0,60     | 0,25     | 0,30   | 0,50     | 0,15 | 2,8        | 5,6         | 0,20         |
| 1300 MW  | 0,60     | 0,25     | 0,28   | 0,48     | 0,16 | 2,8        | 5,6         | 0,18         |
| N4       | 0,58     | 0,22     | 0,26   | 0,46     | 0,17 | 2,9        | 5,8         | 0,16         |
| EPR      | 0,55     | 0,20     | 0,30   | 0,55     | 0,14 | 3,2        | 6,4         | 0,22         |
| EPR2     | 0,50     | 0,20     | 0,35   | 0,60     | 0,12 | 3,6        | 7,2         | 0,25         |

Les paliers récents sont plus manœuvrants : bande plus basse, rampes plus rapides, pénalité xénon plus faible, budgets de modulation plus larges. Un palier inconnu bascule sur la classe 1300 MW, la plus fréquente.

## 7.4 Paramètres de régime (annuels, à l'échelle du parc)

| Symbole | Valeur | Signification |
|:--------|:-------|:--------------|
| `κ` (`u_commit_frac`) | 0,85 | **Plancher d'engagement du parc** : fraction minimale de la capacité disponible qui doit rester couplée au réseau. Empêche le LP de découpler massivement le parc pour éviter la modulation, ce qu'un opérateur ne fait pas. |
| `α_band_op` | 0,74 | **Plancher de bande au niveau du parc**, distinct du minimum technique unitaire (0,55–0,60). Le parc entier ne descend jamais simultanément au minimum de chaque tranche ; ce paramètre traduit cette non-simultanéité. |
| `c_mod` | annuel | **Coût de la modulation profonde** (€/MWh). C'est lui qui, retranché au SRMC, produit l'offre implicite négative. |
| `c_start` | annuel, par classe | Coût de démarrage (€/MW). |
| `r_up_req` / `r_down_req` | annuel | Exigences de réserve à la hausse et à la baisse (MW) portées par le parc (C6). Le headroom hydraulique peut y contribuer. |
| `p_minstab` | annuel | Plancher nucléaire de **stabilité du réseau** (MW) : inertie et tenue de fréquence (C7). |

## 7.5 Familles de contraintes

| Famille | Objet |
|:--------|:------|
| **C1** | Bande à deux étages : plancher normal `α_band`, enfoncement `d` borné par `α_tech`. |
| **C2a / C2b** | Budgets énergétiques de modulation profonde, glissant 8 h et journalier. |
| **C3** | Rampes horaires, avec pénalité xénon sur la montée. |
| **C5** | Coûts de démarrage, durée minimale à l'arrêt, rampe de re-couplage. |
| **C6** | Réserves à la hausse et à la baisse. |
| **C7** | Plancher nucléaire de stabilité réseau. |
| **F5** | Report d'état d'une fenêtre hebdomadaire à la suivante (couplage `u`, puissance, historique de `d`). |

## 7.6 Garde-fous

**Plafonnement de β.** La pénalité xénon est bornée par `r_up / (8 × (α_band − α_tech))`. Sans ce plafond, une modulation profonde soutenue force la puissance à la baisse contre le plancher de bande d'un parc engagé : le problème devient infaisable — une « spirale xénon » numérique sans réalité physique.

**Repli de couture.** L'état est repris d'une fenêtre à l'autre ; si cette reprise sur-contraint la fenêtre, le solveur repart d'un état froid. Ce repli est voulu et documenté : il ne perd aucune fenêtre.

**Rigidité fossile.** Les groupes thermiques français portent aussi un minimum technique (gaz 0,45 ; TAC 0,20 ; charbon 0,40 ; lignite 0,50 ; fioul 0,40 ; biomasse 0,50) et un coût de démarrage, avec un re-couplage plus rapide que le nucléaire (0,50) puisqu'il n'y a pas d'effet xénon.

---

# 8. Cas particulier britannique

La Grande-Bretagne a quitté la plateforme de transparence ENTSO-E après le Brexit ; ses données proviennent d'Elexon/BMRS. Deux corrections en découlent.

**Devise.** Les prix britanniques sont cotés en GBP/MWh et convertis en EUR par la série de référence quotidienne de la BCE. Le module d'ingestion **refuse d'écrire sans taux de change**, afin qu'un mélange de devises ne puisse pas atteindre la base sans être vu.

**Production raccordée en distribution.** La demande britannique publiée (ITSDO) est mesurée au périmètre du **réseau de transport**, donc déjà nette du parc raccordé en distribution, alors que la production publiée ne couvre que le transport. Les deux séries ne bouclent pas : l'écart mesuré atteint **5 851 MW en 2024, un cinquième du système**. Il est reconstitué à partir du bilan propre de la zone et réintroduit dans le modèle.

---

# 9. Projection

- Demande et capacités installées évoluées par **ratios TYNDP**, interpolés linéairement entre années d'ancrage.
- **Interconnexions et nucléaire neuf** traités en marches discrètes, projet par projet, avec année de mise en service — jamais interpolés.
- Les cohortes de soutien aux renouvelables **vieillissent** : la part sous obligation d'achat s'éteint progressivement au profit du merchant.

---

# 10. Limites assumées

Ces limites sont connues et acceptées ; elles sont listées ici pour que la lecture des résultats en tienne compte.

- Pas de co-optimisation énergie/réserves : la marge de réserve est statique.
- Pas de réseau intra-zonal : chaque zone est un nœud unique.
- Pas d'offre stratégique : tous les acteurs offrent à leur coût marginal.
- Pas d'arbitrage de stockage : les STEP sont hors dispatch.
- LP pur : l'engagement est fractionnaire, ce qu'un modèle en nombres entiers interdirait.
- Environ 2,8 GW d'interconnexion britannique aboutissent à des zones non modélisées (Norvège, Irlande) ; mesurées sur 2024, elles se compensent à +505 MW net.
- L'adéquation propre de la Grande-Bretagne n'est pas testée par le modèle.

*Écarts résiduels connus au moment de la rédaction : IT-Nord sur-imprime les heures de prix bas d'un facteur voisin de 9 ; DE-LU ressort sous-évalué d'environ 24 €/MWh.*
