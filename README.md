# Mes prochains passages (métro, RER, tram, bus – PRIM / IDFM)

Une page web pour configurer vos arrêts de métro, RER, tram et bus en Île-de-France, et voir le temps d'attente des prochains passages en temps réel.

Deux façons de l'utiliser, avec **la même interface** (`docs/`) :

| | Sur le Mac (mode serveur) | Sur Android / navigateur (mode autonome) |
|---|---|---|
| Lancement | `python3 server.py` puis http://localhost:8000 | ouvrir l'adresse GitHub Pages, « Installer l'application » |
| Jeton PRIM | fichier `.env` | saisi une fois dans l'appli (☰), stocké sur l'appareil |
| Cartes | `config.json` | stockées sur l'appareil |

La page détecte toute seule le mode : si le serveur Python répond (`/api/status`), elle passe par lui ; sinon elle appelle directement les API PRIM et IDFM (qui autorisent les appels depuis un navigateur).

## Jeton PRIM

Créez un compte sur https://prim.iledefrance-mobilites.fr, puis générez un jeton dans « Mon jeton d'authentification ».
Abonnez-vous à l'API **« Prochains passages (plateforme Île-de-France Mobilités) - requête unitaire »**.

## Mode serveur (Mac)

1. Copiez `.env.example` en `.env` et collez-y votre jeton.
2. Lancez le serveur (Python 3.8 ou plus, aucune dépendance) :

   ```bash
   python3 server.py
   ```

3. Ouvrez http://localhost:8000

## Mode autonome (Android, GitHub Pages)

1. Le dossier `docs/` est publié par GitHub Pages (Settings → Pages → branche `main`, dossier `/docs`).
2. Sur Android, ouvrez l'adresse dans Chrome, puis menu ⋮ → **Installer l'application**.
3. Saisissez votre jeton PRIM (bandeau orange ou menu ☰).
4. Pour récupérer vos cartes du Mac : sur le Mac, ☰ → **Copier** (ou **Exporter**) ; sur le téléphone, ☰ → **Importer…** puis collez le texte (ou choisissez le fichier).

Après une modification de l'appli, augmentez `VERSION` dans `docs/sw.js` pour que les téléphones récupèrent la nouvelle version.

## Fonctionnement

- Recherche d'arrêts : référentiel public IDFM « arrets-lignes » (pas de clé nécessaire).
- Couleurs des lignes : référentiel public « referentiel-des-lignes ».
- Temps réel : API PRIM `stop-monitoring` (SIRI Lite), avec `MonitoringRef=STIF:StopPoint:Q:xxx:` ou `STIF:StopArea:SP:xxx:` et un filtre `LineRef`.
- Perturbations : API Navitia de PRIM (`line_reports`).
- Actualisation toutes les minutes par défaut (réglable dans ☰ : 30 s à 5 min, ou manuelle ; au plus une toutes les 10 s, sauf ⟳).
  Cache de 20 s pour les passages et 2 min pour les perturbations.
- **Quota PRIM** : le jeton par défaut est limité à **1 000 requêtes par jour et 5 par seconde** (une requête par carte et par actualisation).
  L'appli interroge les cartes l'une après l'autre, se met en pause après 15 min sans interaction, et s'arrête 10 min après une erreur 429 (bandeau d'avertissement, derniers horaires conservés).
  Pour un usage confortable, demandez les **quotas augmentés** (gratuits, 1 000 000 par jour) sur la page de l'API « Prochains passages – requête unitaire » de PRIM → « Gérer mes quotas ».
- **Sens** : en ajoutant une ligne, choisissez « Les deux sens » ou un sens précis. Vous pouvez créer une carte par sens.
  Le sens d'une carte existante se change via ⚙.
  - métro, tram, bus : chaque sens a son propre quai, la carte suit donc un seul quai ;
  - RER : le quai est commun, les trains sont triés selon la position de leur terminus par rapport à la gare
    (plus fiable que le champ « Aller/Retour » des données SIRI, incohérent sur le RER A).
- **Onglets Aller / Retour** : chaque carte est rangée dans « Aller », « Retour » ou les deux (via ⚙ ; une nouvelle carte va dans l'onglet ouvert).
  Au démarrage, l'onglet est choisi selon l'heure (Retour à partir de 12 h, réglable dans ☰). Seules les cartes de l'onglet affiché sont actualisées.
- On peut aussi masquer une destination précise d'un clic (pratique pour certaines missions RER).
- **Affichage** : A− / A+ pour la taille, et nombre de passages par carte (réglage général, ou par carte via ⚙).
- **Perturbations** : une ligne repliée par carte (⚠️ incidents, ℹ️ travaux/infos, ♿ ascenseurs) ; un clic la déplie.

## Fichiers

- `server.py` : serveur local (mode serveur).
- `docs/index.html` : l'interface (commune aux deux modes).
- `docs/prim-direct.js` : moteur du mode autonome (port JavaScript de `server.py`).
- `docs/manifest.webmanifest`, `docs/sw.js`, `docs/icon-*.png` : appli installable (PWA).
