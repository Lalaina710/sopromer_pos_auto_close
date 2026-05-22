# SOPROMER POS Auto Close

Fermeture automatique des sessions POS encore ouvertes a une heure programmee,
avec calcul automatique du `balance_end_real` (pas de comptage manuel).

## Description fonctionnelle

SOPROMER opere 40 PdV a Madagascar. Les caissiers oublient regulierement de
fermer leur session POS le soir, ce qui :

- empeche la generation des rapports journaliers
- pollue le pipeline de reporting (sessions ouvertes plusieurs jours)
- gene la consolidation comptable

Ce module automatise la cloture a une heure programmee (par defaut 19h00,
fuseau Madagascar). Le `cash_register_balance_end_real` est rempli avec le
solde theorique calcule par Odoo (`cash_register_balance_end` =
`balance_start` + somme des mouvements de caisse traces). Aucun comptage
physique n'est requis : tous les flux cash sont deja traces dans Odoo
(ventes especes, in/out, paiements multi-modes).

## Architecture technique

### Modeles etendus

| Modele | Champs ajoutes / methodes |
|--------|--------------------------|
| `res.config.settings` | `pos_auto_close_enabled`, `pos_auto_close_hour_global`, `pos_auto_close_email_to` (ICP) |
| `pos.config` | `auto_close_enabled`, `auto_close_hour_override`, `auto_close_email_to_override` |
| `pos.session` | `_cron_auto_close_sessions()`, `_auto_close_dispatch()`, `_auto_close_session()`, `_send_consolidated_auto_close_email()`, `_resolve_auto_close_email_recipients()` |

Depuis v18.0.1.4.0, `_auto_close_session()` ne déclenche plus l'envoi email
unitaire : la méthode retourne un dict (payload) décrivant la session fermée
(nom, PdV, heure, soldes, mouvements de caisse). Le cron accumule les payloads
de toutes les sessions fermées pendant le run, puis appelle
`_send_consolidated_auto_close_email(closed_results)` une fois en fin de run
pour envoyer **un seul email regroupant toutes les sessions**.

Aucun nouveau modele : pas de `ir.model.access.csv` necessaire.

### Cron

Defini dans `data/ir_cron.xml`, `noupdate=1` :

- Code : `model._cron_auto_close_sessions()`
- Frequence : toutes les heures (`interval_number=1`, `interval_type='hours'`)
- `numbercall=-1`, `active=True`
- User : `base.user_root`

### Flow de fermeture

```
_cron_auto_close_sessions()
    |--> read ICP enabled + hour_global
    |--> search pos.session state=opened
    |--> for each session:
            |--> _auto_close_dispatch(session, global_hour)
                    |--> skip if config.auto_close_enabled = False
                    |--> target_hour = override OR global
                    |--> compute current_hour in tz societe (defaut Indian/Antananarivo)
                    |--> skip if current_hour != target_hour
                    |--> skip if state = 'opening_control' (warning)
                    |--> skip if draft orders (activity manager)
                    |--> _auto_close_session()
                            |--> action_pos_session_closing_control()
                            |--> balance_end_real := cash_register_balance_end
                            |--> action_pos_session_close()
                            |--> message_post detaille
```

Chaque session est traitee dans son propre try/except : une erreur sur une
session ne bloque pas le reste du batch. Stack trace logguee + postee en
chatter.

## Notification email

Depuis v18.0.1.4.0, le module envoie **un seul email consolidé par run du
cron**, regroupant toutes les sessions fermées pendant ce run. Auparavant,
1 mail était envoyé par session fermée : avec 40 PdV SOPROMER fermant
simultanément à 19h00, cela produisait jusqu'à 40 mails identiques en
quelques minutes. Le mail consolidé est plus lisible et réduit la pression
SMTP / la pollution boîte de réception.

### Comportement

- **1 mail unique par run cron** (pas 1 par session), même si N sessions
  sont fermées dans le batch
- **Destinataires** : union des recipients résolus pour chaque PdV fermé
  + ICP global `pos_auto_close_email_to`, **dédupliqués**
- **Chatter per-session préservé** : chaque session fermée reçoit toujours
  son message détaillé dans le chatter (audit individuel utile)
- **`auto_delete=True`** : le `mail.mail` est supprimé après envoi (pas de
  persistance d'état `sent` dans la base)
- **Tolérance erreur globale** : exception attrapée + loggée
  (`_logger.error()`) en fin de run, ne bloque pas le cron ni les fermetures
  déjà effectuées

### Configuration des destinataires (2 niveaux)

| Niveau | Champ | Stockage | Format |
|--------|-------|----------|--------|
| Global | `pos_auto_close_email_to` (Settings -> Point de Vente) | ICP `sopromer_pos_auto_close.email_to` | 1 ou plusieurs emails separes par `,` ou `;` |
| PdV | `auto_close_email_to_override` (form pos.config) | Champ direct | Idem |

Résolution par session fermée :

1. Si l'override PdV est rempli -> utilise cette adresse
2. Sinon -> fallback sur le destinataire global
3. Tous les emails résolus de toutes les sessions du run + ICP global sont
   ensuite **unifiés et dédupliqués** pour produire la liste finale du
   mail consolidé
4. Si la liste finale est vide -> aucun email envoyé (skip silencieux)

### Exemples

| PdV | Override PdV | Global | Destinataire effectif |
|-----|--------------|--------|----------------------|
| Magasin A | (vide) | `compta@sopromer.mg` | compta@sopromer.mg |
| Magasin B | `directeur.b@sopromer.mg` | `compta@sopromer.mg` | directeur.b@sopromer.mg, compta@sopromer.mg |
| Magasin C | `manager@x; super@y` | -- | manager@x, super@y |
| Magasin D | (vide) | (vide) | aucun email envoye |

Si Magasin A, B, C ferment dans le même run, le mail consolidé est envoyé à
`compta@sopromer.mg, directeur.b@sopromer.mg, manager@x, super@y` (union
dédupliquée).

### Format du mail consolidé

- **Sujet** : `[SOPROMER] Cloture automatique POS - DD/MM/YYYY (N session(s))`
  où `DD/MM/YYYY` est la date du run et `N` le nombre de sessions fermées
- **Body QWeb** : intro + 1 section par PdV fermé, chaque section contient :
  - heure de fermeture
  - solde initial (`balance_start_fmt`)
  - solde théorique (`expected_fmt`)
  - solde réel = théorique (`balance_end_real`)
  - mouvements de caisse en liste à puces (`cash_moves_html`)
- **From** : `ir.mail_server.smtp_user` (1er actif) ou fallback `company.email`
- **Mode** : `mail.mail` standard avec `auto_delete=True`, envoi async

### Édition du template via UI

Le contenu HTML du mail consolidé est désormais **éditable sans
redéploiement** via un `mail.template` standard Odoo.

- **Path UI** : Settings → Technique → Email → Modèles → chercher
  "SOPROMER : Cloture POS auto - rapport consolide"
- **External ID** : `sopromer_pos_auto_close.mail_template_consolidated`
- **Fichier source** : `data/mail_template_auto_close.xml` (`noupdate=1` →
  modifications UI préservées lors d'un upgrade)

Variables disponibles dans le QWeb (`ctx` = `email_values['email_context']`) :

| Variable | Type | Description |
|----------|------|-------------|
| `ctx['sessions']` | list[dict] | Liste des sessions fermées dans le run |
| `ctx['count']` | int | Nombre de sessions fermées |
| `ctx['date_str']` | str | Date du run au format `DD/MM/YYYY` |
| `ctx['email_from']` | str | Adresse expéditeur résolue |
| `ctx['email_to']` | str | Liste destinataires dédupliquée (séparés `,`) |

Chaque entrée de `ctx['sessions']` est un dict :

| Clé | Type | Description |
|-----|------|-------------|
| `name` | str | Nom de la session (ex `POS/00496`) |
| `pdv` | str | Nom du PdV (`pos.config.name`) |
| `now_str` | str | Heure de fermeture format `HH:MM` |
| `balance_start_fmt` | str | Solde initial formaté FR (`857 300,00`) |
| `expected_fmt` | str | Solde théorique formaté FR |
| `cash_moves_html` | `Markup` | HTML pré-rendu de la liste `<ul><li>` des mouvements de caisse |

> **Note importante** : `cash_moves_html` est wrappé en `markupsafe.Markup`
> côté Python depuis v18.0.1.4.1, donc `<t t-out="ctx['sessions'][i]['cash_moves_html']"/>`
> dans le QWeb rend le HTML brut (balises `<ul><li>` interprétées comme
> structure) au lieu de l'échapper en texte brut. Si tu modifies le template
> et veux afficher ce champ, utilise toujours `t-out` (jamais `t-esc`).

## Configuration (2 niveaux)

### Niveau global (Settings -> Point de Vente)

Bloc "Fermeture automatique" :

- `pos_auto_close_enabled` (Boolean, defaut **True**)
- `pos_auto_close_hour_global` (Selection 00:00..23:00, defaut **19**)

Stockes en `ir.config_parameter` :

- `sopromer_pos_auto_close.enabled`
- `sopromer_pos_auto_close.hour_global`

### Niveau PdV (Point de Vente -> form pos.config)

Bloc "Fermeture automatique" :

- `auto_close_enabled` (Boolean, defaut **True**) -- desactive ce PdV
- `auto_close_hour_override` (Selection 00:00..23:00, optionnel) -- override

### Exemple

| PdV | enabled | override | Heure effective |
|-----|---------|----------|----------------|
| Magasin A | True | (vide) | 19h (global) |
| Magasin B | True | 22 | 22h |
| Magasin C | False | -- | jamais auto-ferme |

Si le toggle global est decoche, **aucun** PdV n'est ferme, peu importe les
parametres par PdV.

## Edge cases geres

| Cas | Comportement |
|-----|--------------|
| Session bloquee `opening_control` | Skip + warning log + chatter |
| Session avec orders `draft` | Skip + activity manager |
| Toggle PdV `auto_close_enabled = False` | Skip silencieux |
| Heure courante != heure cible | Skip silencieux |
| Toggle global desactive | Cron skip immediat |
| Erreur Python pendant cloture | Log + chatter, autres sessions continuent |
| Timezone societe inconnue | Fallback `Indian/Antananarivo` |
| `hour_global` corrompu en ICP | Fallback 19h |
| Multi-PdV meme heure | Traitement sequentiel, transactions isolees |
| Week-end / dimanche | Cron toujours actif (POS SOPROMER ouvre 7/7) |

## Tests fonctionnels

### Test 1 : config globale
1. Settings > Point de Vente > scroll jusqu'a "Fermeture automatique"
2. Verifier toggle `True` par defaut, heure `19:00`
3. Modifier a `20:00`, sauvegarder
4. Verifier dans `ir.config_parameter` :
   - `sopromer_pos_auto_close.hour_global` = `20`

### Test 2 : config PdV
1. Point de Vente > Configuration > <un PdV>
2. Section "Fermeture automatique"
3. Verifier toggle `True` par defaut, override vide
4. Mettre override = `22:00`, sauvegarder

### Test 3 : cron skip si toggle global Off
1. Toggle global = False
2. `model = env['pos.session']; model._cron_auto_close_sessions()`
3. Aucune session fermee, log "Disabled globally, cron skipped."

### Test 4 : fermeture effective
1. Toggle global = True, heure = heure courante
2. Ouvrir une session POS sur un PdV (auto_close_enabled=True)
3. Faire une vente especes (pour avoir balance_end != balance_start)
4. Lancer cron manuellement : Settings > Technical > Scheduled Actions >
   "SOPROMER : Fermeture automatique des sessions POS" > Run Manually
5. Verifier session :
   - state = `closed`
   - `cash_register_balance_end_real` = `cash_register_balance_end`
   - chatter contient le message detaille avec mouvements

### Test 5 : skip session draft
1. Ouvrir session, creer une commande draft (sans payment)
2. Lancer cron a l'heure cible
3. Verifier session reste `opened` + activity creee sur `user_id`

### Test 6 : skip opening_control
1. Ouvrir une session mais ne pas valider le fond (laisser
   `state = 'opening_control'`)
2. Lancer cron a l'heure cible
3. Verifier session reste en `opening_control` + chatter explique

### Test 7 : exception non bloquante
1. Forcer une erreur (e.g. statement_line corrompue)
2. Verifier autres sessions toujours fermees
3. Verifier log `_logger.error` + chatter avec stack trace sur la session
   defaillante

## Deploiement

Module au standard SOPROMER : depend uniquement de `point_of_sale`,
pas de modele custom, pas d'asset frontend.

### Sequence

1. **Test 45** : copier dossier, restart, install module via UI
   - Settings > Apps > Update Apps List
   - Installer "SOPROMER POS Auto Close"
   - Tests 1 a 7 ci-dessus
2. **Prod 43 le soir** (apres heures travail) :
   - `./scripts/deploy.sh sopromer_pos_auto_close 43`
   - `docker restart odoo-dev`
   - Install via UI
   - Verifier cron actif dans Settings > Technical > Scheduled Actions

### Vigilance prod 43

- Heure par defaut **19h00** : tous les PdV ouverts seront fermes des 19h
  apres install. Communiquer aux superviseurs avant deploiement.
- Si besoin de tester en douceur : passer a `auto_close_enabled = False`
  globalement, activer PdV par PdV.

### Gotcha Odoo 18 : nouveau fichier XML data ignoré par `-u`

Depuis v18.0.1.4.0, le module embarque un nouveau fichier
`data/mail_template_auto_close.xml`. Sur un module **déjà installé** (cas
upgrade 45 ou 43), un simple `-u sopromer_pos_auto_close` peut ignorer
silencieusement ce nouveau fichier `data` : le `mail.template` n'est alors
pas créé en base et l'envoi du mail consolidé échoue (`ValueError: External
ID not found: sopromer_pos_auto_close.mail_template_consolidated`).

**Avant l'upgrade**, forcer le state du module en SQL :

```sql
UPDATE ir_module_module
SET state='to upgrade'
WHERE name='sopromer_pos_auto_close';
```

Puis lancer l'upgrade + restart container, et **valider en post-deploy** que
le template est bien présent :

```sql
SELECT id, name
FROM mail_template
WHERE name LIKE 'SOPROMER%Cloture%';
-- doit retourner 1 ligne : "SOPROMER : Cloture POS auto - rapport consolide"
```

Si la requête retourne 0 ligne, refaire un upgrade complet du module via
Apps UI ("Mettre à jour"), pas un `-u` ligne de commande.

## Historique des versions

### 18.0.1.4.1 - 2026-05-22

- **Fix** : `cash_moves_html` wrappé dans `markupsafe.Markup` pour éviter
  l'échappement HTML par `t-out` Odoo 18 (sinon balises `<ul><li>` affichées
  brutes dans le mail au lieu d'être rendues en liste à puces)
- Import `from markupsafe import Markup` ajouté en tête de `pos_session.py`
- Validé runtime 45 SOPROMER-REST200526 : 3 sessions test → mail consolidé
  reçu avec rendu HTML correct

### 18.0.1.4.0 - 2026-05-22

- **Feature majeure** : email consolidé — 1 SEUL mail par run cron au lieu
  d'1 mail par session fermée
- Nouveau `mail.template` éditable via UI : `mail_template_consolidated`
  (`data/mail_template_auto_close.xml`, `noupdate=1`)
- Path UI éditable : Settings → Technique → Email → Modèles
- Sujet : `[SOPROMER] Cloture automatique POS - DD/MM/YYYY (N session(s))`
- Body QWeb avec `t-foreach="ctx['sessions']"` → 1 section par PdV fermé
- Destinataires : union override PdV + ICP global, dédupliqués
- Refactor cron : `_auto_close_session` retourne payload dict, cron accumule
  puis appelle `_send_consolidated_auto_close_email(closed_results)` en fin
- Méthode `_send_auto_close_email` supprimée (remplacée)
- `action_force_auto_close` et `_force_auto_close_all` empruntent le même
  chemin consolidé
- Chatter per-session **préservé** (audit individuel utile)
- Tolerance erreur globale autour du send consolidé (try/except, log error,
  ne bloque pas le cron)
- Validé runtime 45 : 4 sessions test (force-close batch) → 1 mail envoyé
  à Fitahiana + Joël, sujet `(4 session(s))`, SMTP send OK

### 18.0.1.3.2 - 2026-05-01

- Email/chatter mouvements de caisse : afficher le **numéro Sage** du move
  (`account.move.name`, ex `CSE8/26-27/0063`) au lieu du seul `payment_ref`
- Format `[TYPE] <num move> — <payment_ref> : <signe><montant>`
- Source : `cm.move_id.name`

### 18.0.1.3.1 - 2026-05-01

- Helper `_fmt_money(amount)` : format français milliers/décimales
  (`857300.0` → `857 300,00`, `361767.04000000004` → `361 767,04`)
- Tag explicite `[CASH IN]` / `[CASH OUT]` selon signe du mouvement
- Vue Settings et form pos.config : 2 lignes séparées HH / MM (au lieu de
  side-by-side qui rendait les selects collés visuellement)

### 18.0.1.3.0 - 2026-05-01

- Ajout champ **Minute (MM)** override par PdV (`auto_close_minute_override`)
  + globale ICP `sopromer_pos_auto_close.minute_global`
- Selection `MINUTE_SELECTION` step 5 (00, 05, 10, ..., 55)
- Cron principal passé de `1 hours` à `5 minutes` pour respecter granularité
- Logique dispatch : fenêtre `[target_total, target_total+5min[` (idempotent
  via check `state='opened'`)
- Helper `_read_int_icp(IrConfig, key)` factorisé

### 18.0.1.2.1 - 2026-05-01

- Nouveau cron de test **`SOPROMER : [TEST] Forcer fermeture sessions POS
  (manuel)`** dans `data/ir_cron.xml`
- `active=False` + `nextcall=+10ans` → ne fire pas auto, mais "Exécuter
  manuellement" déclenche immédiatement
- Code : `model._force_auto_close_all()` (bypass check heure)

### 18.0.1.2.0 - 2026-05-01

- Méthode publique `action_force_auto_close()` sur `pos.session` : force la
  fermeture en réutilisant `_auto_close_session()`, bypass check heure mais
  garde protections (opening_control, draft orders, opted-out)
- Méthode `_force_auto_close_all()` : entry point pour cron / server action
- 2 server actions dans `data/server_actions.xml` :
  - "SOPROMER : Forcer fermeture auto sessions POS (toutes)" — globale
    via Settings > Technique > Actions serveur
  - "SOPROMER : Forcer fermeture auto (sélection)" — contextuelle, menu
    Action de la liste/form pos.session (binding model)
- Notification client `display_notification` avec liste fermées / ignorées

### 18.0.1.1.5 - 2026-04-30

- Heure globale vide (ICP non défini ou '') → cron **skip silencieusement**
  au lieu de fallback 19h
- Permet d'installer le module sans déclencher de fermeture tant qu'aucune
  heure n'est configurée (global ou override PdV)
- Skip explicite par session si override vide ET global vide

### 18.0.1.1.4 - 2026-04-30

- Cleanup phantom `Écart d'espèces` stmt_line créée par Odoo natif sur
  sessions sans transactions (artefact). Detection : montant = -balance_start
  + label contient `Écart` ou `Cash difference`
- `flush_recordset` + `invalidate_recordset` après set balance_end_real
  pour forcer Odoo à voir la nouvelle valeur lors de closing_control compute
- Body chatter et email reflètent maintenant correctement "aucun mouvement"
  pour sessions sans transactions

### 18.0.1.1.3 - 2026-04-30

- Pre-fill `cash_register_balance_end_real = cash_register_balance_end`
  AVANT `action_pos_session_closing_control()` (au lieu d'après)
- Garantit `cash_register_difference = 0` → pas de perte/gain comptable
- Skip `action_pos_session_close()` si state déjà `closed` (Odoo 18 finalise
  parfois direct via closing_control)

### 18.0.1.1.2 - 2026-04-30

- `email_from` cascade : `ir.mail_server.smtp_user` (1er actif) prioritaire
  sur `company.email`. Évite mismatch from_filter SMTP

### 18.0.1.1.1 - 2026-04-30

- Fix HOUR_SELECTION clés string `'00'..'23'` (Odoo 18 ValidationError sur
  Selection avec clés int)
- Suppression champ `numbercall` dans ir_cron.xml (retiré Odoo 18)
- Skip `action_pos_session_close()` si state == 'closed' après closing_control

### 18.0.1.1.0 - 2026-04-30

- Notification email apres chaque fermeture automatique reussie
- Config 2 niveaux : ICP global `sopromer_pos_auto_close.email_to` +
  override par PdV `auto_close_email_to_override`
- Parsing multi-destinataires (separateurs `,` et `;`)
- Tolerance aux erreurs SMTP (log + skip, pas de rollback)
- Ajout dependance `mail`

### 18.0.1.0.0 - 2026-04-30

- Version initiale
- 2 niveaux de config (global + PdV)
- Cron horaire
- Fermeture auto avec balance_end_real = expected
- Edge cases : opening_control, orders draft, exceptions

## Auteur

SOPROMER -- 2026
