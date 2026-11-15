# Temps d'attente (métro, RER, tram, bus – PRIM / IDFM)

Une appli web pour configurer vos arrêts de métro, RER, tram et bus en Île-de-France, et voir le temps d'attente des prochains passages en temps réel.

**En ligne : https://frapil.github.io/temps-attente/**

C'est un site statique : le dossier `docs/`, publié par GitHub Pages (branche `main`, dossier `/docs`). Il appelle directement les API PRIM et IDFM depuis le navigateur, ce qu'elles autorisent. Aucun serveur n'est nécessaire.

## Installation

1. Sur Android, ouvrez l'adresse dans Chrome, puis menu ⋮ → **Installer l'application**. Sur ordinateur, ouvrez simplement l'adresse.
2. Saisissez votre jeton PRIM, dans le bandeau orange ou dans le menu ☰. Il est stocké uniquement sur l'appareil.
3. Ajoutez vos arrêts avec **+**.

Le jeton et les cartes sont enregistrés dans le navigateur, propres à chaque appareil et à chaque adresse.
Pour transférer vos cartes vers un autre appareil :
- sur l'appareil de départ, ☰ → **Copier** (ou **Exporter**) ;
- sur l'autre, ☰ → collez le texte dans la zone prévue, puis **Importer ce texte** (ou **Importer un fichier…**).

## Jeton PRIM

1. Créez un compte sur https://prim.iledefrance-mobilites.fr.
2. Générez un jeton dans « Mon jeton d'authentification ».
3. Abonnez-vous à l'API **« Prochains passages (plateforme Île-de-France Mobilités) - requête unitaire »**.

Le quota par défaut est de **1 000 requêtes par jour et 5 par seconde**. Chaque carte consomme une requête à chaque actualisation.
Demandez plutôt les **quotas augmentés**, gratuits (1 000 000 par jour) : page de cette API → « Gérer mes quotas ».

## Fonctionnement

- **Recherche d'arrêts** : référentiels publics IDFM « arrets-lignes » et « referentiel-des-lignes » (couleurs), sans clé.
- **Temps réel** : API PRIM `stop-monitoring` (SIRI Lite).
  - `MonitoringRef=STIF:StopPoint:Q:xxx:` ou `STIF:StopArea:SP:xxx:` ;
  - filtre `LineRef`.
- **Perturbations** : API Navitia de PRIM (`line_reports`). Chaque carte les résume sur une ligne repliée (⚠️ incidents, ℹ️ travaux et infos, ♿ ascenseurs) ; un clic la déplie.
- **Sens** : en ajoutant une ligne, choisissez « Les deux sens » ou un sens précis. Vous pouvez créer une carte par sens, et changer le sens d'une carte via ⚙.
  - Métro, tram et bus : chaque sens a son propre quai.
  - RER : le quai est commun. Les trains sont triés selon la position de leur terminus par rapport à la gare. C'est plus fiable que le champ « Aller/Retour » de SIRI, incohérent sur le RER A.
  - Un clic sur une destination la masque, ce qui est pratique pour certaines missions RER.
- **Onglets Aller / Retour** : chaque carte est rangée dans « Aller », « Retour » ou les deux, via ⚙.
  - Une nouvelle carte va dans l'onglet ouvert.
  - Au démarrage, l'onglet est choisi selon l'heure : Retour à partir de 12 h, réglable dans ☰.
- **Affichage** : A− / A+ règlent la taille. Le nombre de passages se règle pour toutes les cartes, ou carte par carte via ⚙.
- **Actualisation** : toutes les minutes par défaut. Réglable dans ☰, de 30 s à 5 min, ou manuelle. ⟳ force une actualisation.
  - Seules les cartes de l'onglet affiché sont actualisées, l'une après l'autre.
  - Pause automatique après 15 min sans interaction.
  - Après une erreur 429 (quota dépassé) : arrêt pendant 10 min. Un bandeau prévient, et les derniers horaires restent affichés.
- **Cache** : 20 s pour les passages, 2 min pour les perturbations, 24 h pour les référentiels.

## Fichiers (`docs/`)

- `index.html` : l'interface.
- `prim-direct.js` : appels aux API PRIM et IDFM, et traitement des données (sens, perturbations, cache).
- `manifest.webmanifest`, `sw.js`, `icon-*.png` : appli installable (PWA).

Après une modification, augmentez `VERSION` dans `docs/sw.js` pour que les appareils récupèrent la nouvelle version.

Pour tester en local :

```bash
cd docs && python3 -m http.server 8080
```

puis ouvrez http://localhost:8080.
