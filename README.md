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
| `pos.session` | `auto_close_fail_count` (Integer, compteur d'echecs consecutifs, circuit-breaker), `_cron_auto_close_sessions()`, `_auto_close_dispatch()`, `_auto_close_session()`, `_send_consolidated_auto_close_email()`, `_resolve_auto_close_email_recipients()` |

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
                    |--> skip if current_time < target_time (borne basse only)
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
- `sopromer_pos_auto_close.max_retries` (Integer, defaut **3**) -- circuit-breaker :
  nombre maximal d'echecs consecutifs de fermeture auto par session avant que
  le cron cesse de la retenter (anti-spam chatter)

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
| Heure courante avant l'heure cible | Skip silencieux (borne basse only, plus de borne haute : ferme des que `current_time >= target_time`, idempotence garantie par `state='opened'`) |
| Toggle global desactive | Cron skip immediat |
| Erreur Python pendant cloture | Rollback curseur, incrementation `auto_close_fail_count` en transaction fraiche, log warning ; chatter **uniquement au 1er echec** (anti-flood). Au-dela de `max_retries` (defaut 3) echecs consecutifs, la session n'est plus retentee (reste `opened`, fermeture manuelle requise) ; compteur remis a 0 a la prochaine fermeture reussie |
| Crash / timeout worker en milieu de batch | `cr.commit()` par session reussie -> les sessions deja fermees sont preservees, jamais annulees par une erreur ulterieure ou un timeout (`limit_time_cpu`/`real`) |
| Session deja fermee par une iteration anterieure | Re-check d'etat frais (`invalidate_recordset(['state'])`) en debut d'iteration -> skip si `state != 'opened'` |
| Timezone societe inconnue | Fallback `Indian/Antananarivo` |
| `hour_global` corrompu en ICP | Fallback 19h |
| Multi-PdV meme heure | Traitement sequentiel, transactions isolees |
| Week-end / dimanche | Cron toujours actif (POS SOPROMER ouvre 7/7) |

> **Note operateur** : depuis 1.4.3 il n'y a plus de borne haute. Toute session
> ouverte alors que l'heure locale est deja >= l'heure cible sera fermee au
> prochain tick du cron (ex. session ouverte tot le matin avant une reconfig).
> C'est voulu (idempotence par `state='opened'`), mais a connaitre pour eviter
> une fermeture surprise.

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

### Test 8 : tests automatises (depuis 18.0.1.4.6)

`tests/test_auto_close_cash_moves.py`, `TransactionCase` taggue `post_install` :

```bash
odoo-bin -d <base> -u sopromer_pos_auto_close \
         --test-enable --test-tags /sopromer_pos_auto_close --stop-after-init
```

- `test_only_cash_sale_line_is_tagged_vente_du_jour` : session avec une vente
  cash (contrepartie `asset_receivable`), un cash in (`asset_current`) et un
  ecart de cloture (`expense`) — seule la vente porte `[VENTE DU JOUR]`
- `test_payment_ref_html_is_escaped_but_template_stays_html` : un `payment_ref`
  contenant `<b>` et `&` est echappe, le gabarit (`<li>`, `&mdash;`) reste HTML

La fixture ne simule pas une vente POS complete : le critere lu par le code est
le `account_type` de la contrepartie, resolue a la creation de la ligne de
releve sur `journal.suspense_account_id`. Le test permute ce compte d'attente
autour de chaque create pour reproduire les trois signatures comptables reelles.

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

### 18.0.1.4.7 - 2026-09-03

**Test-only — aucune modification de `models/`, le runtime 1.4.6 est inchange.**

- **Test (durcissement)** : `test_only_cash_sale_line_is_tagged_vente_du_jour`
  n'etait pas contraignant. Le `payment_ref` de la ligne de vente etait
  `TEST-AC/VENTE` — il contenait litteralement « VENTE » — et la vente portait
  le plus gros montant. Un `'VENTE' in payment_ref` ou un « plus gros montant »
  passait les 3 assertions, alors que **ne pas dependre du `payment_ref` est
  precisement la raison d'etre de la 1.4.5**. Le test validait le comportement
  observable, pas le mecanisme qu'il documente
- **Test** : fixture reconstruite en 4 lignes, chacune tuant une famille de
  leurre — vente `POS/09912` (libelle neutre, != nom de session, montant 1000
  ni max ni min, creee en 3e position), apport `POS/09913` (**libelle de meme
  forme que la vente**, montant 1500 > vente), piege `VENTE COMPTOIR - avance`
  (libelle qui crie « vente », contrepartie compte d'attente → doit rester
  `CASH IN`), ecart `-25` sur compte de charge. Seul le `account_type` de la
  contrepartie discrimine
- **Test** : verifie en rejouant la fixture contre **15 implementations
  naives** (matching de libelle `'VENTE' in ref` / `startswith('POS/')` /
  `^POS/\d+$` / `'/' in ref` / `== session.name`, montant max / min positif /
  seuil, signe, rang de creation, contrepartie `!= asset_current`) : **les 15
  echouent**, seule la regle reelle passe
- **Test** : ajout de `assertEqual(len(tags), 4)` — garantit que les 4 lignes
  sont rendues et qu'aucune assertion ne devient vacante (ni ligne avalee par
  le nettoyage de l'ecart fantome, ni ratee par le parsing)
- **Test** : `assertIn('&lt;b&gt;Naka&lt;/b&gt;', body)` ajoute au test
  d'echappement. `assertNotIn('<b>Naka</b>')` + `assertIn('Naka')` passaient
  aussi si c'etait `html_sanitize` qui retirait la balise plutot que notre
  echappement qui la neutralise — la cause est desormais epinglee
- Note : `set_opening_control(500.0)` inchange, l'ecart de `-25` echappe
  toujours au nettoyage de l'ecart fantome (`abs(-25 + 500) = 475`), et
  `balance_end_real` restant aligne sur `cash_register_balance_end`, la
  difference reste nulle — pas de ligne parasite creee par le closing

### 18.0.1.4.6 - 2026-09-02

- **Fix (sécu, Majeur)** : asymétrie `sudo()` dans `_auto_close_session()`. Le
  set des lignes de vente etait calcule sur `self.sudo().statement_line_ids`
  mais la boucle de rendu iterait `self.statement_line_ids` non sudo. Ne levait
  pas d'`AccessError` uniquement parce que le fetch sudo prechauffe le cache
  partage de la transaction (dans `Field.__get__`, un hit cache court-circuite
  le controle d'acces) — donc correct par effet de bord, et casse au moindre
  reordonnancement du bloc. Le recordset sudo est desormais calcule une fois
  dans `statement_lines` et sert aux deux usages
- **Fix (doc)** : commentaire de justification du `sudo()` corrige. Il citait
  `account.account`, qui n'est pas le mur : `base.group_user` a deja READ
  dessus. L'ACL n'est pas le mur non plus — `point_of_sale` donne READ a
  `group_pos_user` sur `account.move` ET `account.move.line`. Le vrai blocage
  vient des deux record rules POS du core,
  `point_of_sale.rule_invoice_pos_user` (`[('pos_order_ids','!=',False)]`) et
  `point_of_sale.rule_invoice_line_pos_user`
  (`[('move_id.pos_order_ids','!=',False)]`) : l'ecriture d'une ligne de releve
  de session est l'ecriture de caisse, jamais une facture, donc
  `pos_order_ids` y est toujours vide (verifie en base : 0 pos_order sur 100 %
  des `statement_line.move_id` POS)
- **Fix (sécu, P2)** : `payment_ref` (texte libre saisi par le caissier comme
  motif de cash in/out) etait interpole en `%s` dans une `str` ensuite wrappee
  `Markup()` — zero echappement, donc rendu casse dans le chatter ET le mail
  des qu'un libelle contient `<`, `>` ou `&`. Les lignes sont maintenant
  construites via `Markup("...") % (...)`, qui echappe les arguments substitues
  sans toucher au gabarit (`<li>`, `<b>` et `&mdash;` restent du HTML).
  Applique aussi a `move_ref` par coherence
- **Refactor (mineur)** : `sale_statement_line_ids` renomme `sale_st_line_ids`
  — le suffixe `_ids` suggerait un recordset alors que c'est un `set()` d'int
  (annotation ajoutee)
- **Test** : ajout de `tests/test_auto_close_cash_moves.py` (`TransactionCase`,
  `post_install`). Couvre l'heuristique de 1.4.5 — session avec 1 vente cash
  (contrepartie `asset_receivable`), 1 cash in (`asset_current`) et 1 ecart de
  cloture (`expense`) : seule la vente porte `[VENTE DU JOUR]`, les deux autres
  gardent `[CASH IN]` / `[CASH OUT]`. Second test sur l'echappement d'un
  `payment_ref` hostile. Premier repertoire `tests/` du module
- Note : pas de changement de comportement metier. Le tag `[VENTE DU JOUR]` et
  la discrimination de 1.4.5 sont inchanges — seuls le chemin d'acces, le
  rendu HTML des libelles et la couverture de test bougent

### 18.0.1.4.5 - 2026-09-01

- **Feature** : dans la section "Mouvements de caisse" (chatter + email
  consolide), la ligne de reglement des ventes especes du jour est desormais
  taguee `[VENTE DU JOUR]` au lieu de `[CASH IN]`. Les apports et retraits
  manuels du caissier gardent `[CASH IN]` / `[CASH OUT]`. Rendu :
  `[VENTE DU JOUR] CSH10/26-27/0242 — POS/05818 : +364 077,78`
- **Technique** : la discrimination ne repose pas sur le libelle mais sur la
  contrepartie comptable de la ligne de releve — seule la ligne de vente porte
  une contrepartie sur un compte client (`asset_receivable`), posee par le core
  dans `_get_combine_statement_line_vals` / `_get_split_statement_line_vals`.
  Les cash in/out (`try_cash_in_out`) tombent sur le compte d'attente du
  journal (`asset_current`), les ecarts de cloture sur les comptes
  perte/profit (`expense` / `income`)
- **Technique** : critere valide sur 45 (base `SOPROMER-040826`, 8036 lignes de
  releve POS) — 4652/4652 lignes de vente captees (4479 consolidees +
  173 detaillees), 0 faux positif sur 1388 cash in/out et 1995 ecarts de
  cloture. Le `payment_ref` seul aurait rate les 173 ventes detaillees, dont le
  libelle vaut le nom du paiement (vide en pratique) et non celui de la session
- **Technique** : repli conservateur — une ligne non formellement identifiee
  comme vente reste `[CASH IN]` / `[CASH OUT]`, jamais l'inverse
- Note : pas de changement de comportement metier — libelle d'audit uniquement.
  Un seul point modifie (`_auto_close_session()`), donc chatter et email
  consolide changent ensemble

### 18.0.1.4.4 - 2026-06-09

- **Fix (QA)** : docstring de `_auto_close_dispatch` mise a jour — supprimait
  encore la mention obsolete "fenetre 5 min" / "target window" alors que la
  borne haute a ete retiree en 1.4.3. Decrit maintenant exactement la logique :
  borne basse uniquement, ferme des que `current_time >= target_time`,
  idempotence par `state='opened'`
- **Fix (QA)** : `traceback.format_exc()` desormais capture une seule fois dans
  une variable `tb` en tete du `except` (avant `rollback()`), puis reutilise
  pour le `_logger.error` ET le chatter. Avant, l'appel tardif a `format_exc()`
  (apres le try/except interne du compteur d'echecs) pouvait logger/poster la
  mauvaise trace (celle d'une exception interne rattrapee, pas l'exception
  originale de la fermeture)
- Note : pas de changement de comportement metier — corrections de
  diagnostic/documentation uniquement. Build + test runtime 45 (6/6 PASS) deja
  valides sur le code 1.4.3 identique cote logique. Revue code QA : verdict
  GO-avec-corrections, les 2 corrections ci-dessus appliquees

### 18.0.1.4.3 - 2026-06-09

- **Feature** : nouveau champ `auto_close_fail_count` (Integer, `default=0`,
  `copy=False`) sur `pos.session` — compteur d'echecs consecutifs de la
  fermeture auto
- **Feature** : circuit-breaker anti-spam — au-dela de `max_retries` echecs
  consecutifs (ICP `sopromer_pos_auto_close.max_retries`, defaut **3**), la
  session n'est plus retentee par le cron (reste `opened`, fermeture manuelle
  requise) au lieu de poster un chatter a chaque tick toute la nuit (ex.
  picking refuse par `stock_no_negative`). Compteur remis a 0 sur fermeture
  reussie (reset defensif dans `_auto_close_session()` pour les echecs transitoires)
- **Fix** : `cr.commit()` par session fermee — chaque fermeture est isolee
  dans sa propre transaction. Un timeout worker (`limit_time_cpu`/`real`) ou
  une erreur sur une session suivante ne peut plus annuler les sessions deja
  fermees avec succes
- **Fix** : sur exception, `cr.rollback()` AVANT toute autre operation (le
  curseur peut etre "aborted" post-exception, empoisonnant les sessions
  suivantes), puis incrementation du compteur dans une transaction fraiche
  (`invalidate_recordset` -> relit valeur fraiche -> +1 -> commit) pour qu'il
  survive meme si une session ulterieure rollback
- **Fix** : chatter poste **uniquement au 1er echec** (`new_count == 1`) —
  stoppe le flood de messages sur une session bloquee. Les echecs suivants
  restent dans le log (`_logger.warning`)
- **Fix** : re-check d'etat frais en debut d'iteration
  (`invalidate_recordset(['state','auto_close_fail_count'])` puis skip si
  `state != 'opened'`) — `opened_sessions` est lu une fois en debut de run
  mais on commit par session, une session peut donc deja avoir ete fermee
- **Fix** : suppression de la borne haute de la fenetre de fermeture dans
  `_auto_close_dispatch`. Avant : fenetre `[target_total, target_total+5min[`
  (fermeture seulement pendant le tick de 5 min) ; si le worker manquait de
  temps en milieu de batch, les sessions non fermees n'etaient JAMAIS reprises.
  Maintenant : borne basse uniquement (`if current_total < target_total:
  return None`) -> ferme des que l'heure locale >= cible. Idempotence toujours
  garantie par le check `state='opened'`

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
