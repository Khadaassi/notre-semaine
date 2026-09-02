# Notre Semaine — application Django

Application familiale complète : routines quotidiennes (enfants/parents), rotation des
tâches ménagères, menu de la semaine avec recettes et liste de courses. Comptes utilisateurs
réels (Django auth : mots de passe hashés, sessions sécurisées, CSRF), inscription protégée
par un code d'invitation familial.

## Lancer en local

```bash
python3 -m venv venv
source venv/bin/activate          # Windows : venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # puis éditez .env (SECRET_KEY, code d'invitation...)
python manage.py migrate
python manage.py createsuperuser  # optionnel, pour l'admin Django
python manage.py runserver
```

Le fichier `.env` (jamais commité, voir `.gitignore`) contient vos réglages locaux —
`DEBUG`, `SECRET_KEY`, `FAMILY_INVITE_CODE`. `.env.example` sert de modèle sans secrets.

Ouvrez http://127.0.0.1:8000 — la première visite propose de créer un compte
(`/inscription/`) avec le code d'invitation défini dans `FAMILY_INVITE_CODE`
(par défaut **Barakah2026**, à changer en production — voir plus bas).

## Mettre le code sur GitHub

```bash
cd notre_semaine_django
git init
git add .
git commit -m "Première version — Notre Semaine"
git branch -M main
git remote add origin https://github.com/<votre-compte>/notre-semaine.git
git push -u origin main
```

Créez d'abord le dépôt vide sur github.com (bouton "New repository"), en **Private**
si vous voulez que le code source reste privé (ça n'affecte pas l'accès à l'app une
fois déployée, juste la visibilité du code).

## Déployer gratuitement sur Render.com

Render propose un plan gratuit avec PostgreSQL inclus, suffisant pour cet usage.

1. Créez un compte sur [render.com](https://render.com) (gratuit, connexion possible
   directement avec GitHub)
2. **New** → **Blueprint** → connectez votre dépôt GitHub `notre-semaine`
3. Render détecte automatiquement le fichier `render.yaml` inclus dans ce projet et
   propose de créer le service web + la base PostgreSQL gratuite
4. Avant de valider, changez la valeur de `FAMILY_INVITE_CODE` dans les variables
   d'environnement pour un code de votre choix (pas obligatoire mais recommandé)
5. Cliquez **Apply** — le premier déploiement prend quelques minutes
6. Une fois en ligne, Render vous donne une URL du type
   `https://notre-semaine.onrender.com`

Créez ensuite vos deux comptes (vous et votre mari) via `/inscription/` avec le
code d'invitation.

**Note sur le plan gratuit Render** : le service se met en veille après 15 minutes
sans trafic et met ~30-50 secondes à se "réveiller" au premier accès de la journée —
normal sur le plan gratuit, pas un bug.

## Sécurité — ce qui est réellement en place

- Mots de passe : jamais stockés en clair, hashés par Django (PBKDF2)
- Comptes séparés : chaque personne a son propre identifiant/mot de passe
- Inscription fermée : un code d'invitation est nécessaire (`FAMILY_INVITE_CODE`) —
  changez-le et ne le partagez qu'en privé
- HTTPS forcé, cookies de session sécurisés (activés automatiquement en production
  via les variables d'environnement `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`,
  `CSRF_COOKIE_SECURE` déjà présentes dans `render.yaml`)
- Protection CSRF sur tous les formulaires
- **Changez `SECRET_KEY`** en production — `render.yaml` en génère une automatiquement,
  ne réutilisez jamais celle de développement

## Administration

`/admin/` (après `createsuperuser`) permet de modifier directement les recettes,
réglages, membres de la famille, etc. sans passer par l'interface.

## Structure

```
config/          réglages Django, URLs racine
planner/         toute la logique de l'app
  models.py      les données (réglages, activités, tâches, recettes, courses)
  task_logic.py  génère la liste de tâches du jour pour chaque personne
  views.py       les pages et actions (cocher une tâche, changer le menu...)
  default_data.py  recettes et liste de courses de départ
templates/       les pages HTML
static/          la feuille de style
```
