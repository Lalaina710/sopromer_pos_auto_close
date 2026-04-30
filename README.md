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
| `pos.session` | `_cron_auto_close_sessions()`, `_auto_close_dispatch()`, `_auto_close_session()`, `_send_auto_close_email()`, `_resolve_auto_close_email_recipients()` |

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

Apres chaque fermeture automatique reussie, un email est envoye au(x)
destinataire(s) configure(s). Le contenu reprend le message poste dans le
chatter (solde initial, solde theorique, solde de cloture, mouvements de
caisse).

### Configuration des destinataires (2 niveaux)

| Niveau | Champ | Stockage | Format |
|--------|-------|----------|--------|
| Global | `pos_auto_close_email_to` (Settings -> Point de Vente) | ICP `sopromer_pos_auto_close.email_to` | 1 ou plusieurs emails separes par `,` ou `;` |
| PdV | `auto_close_email_to_override` (form pos.config) | Champ direct | Idem |

Resolution :

1. Si l'override PdV est rempli -> utilise cette adresse
2. Sinon -> fallback sur le destinataire global
3. Si les deux sont vides -> aucun email envoye (skip silencieux)

### Exemples

| PdV | Override PdV | Global | Destinataire effectif |
|-----|--------------|--------|----------------------|
| Magasin A | (vide) | `compta@sopromer.mg` | compta@sopromer.mg |
| Magasin B | `directeur.b@sopromer.mg` | `compta@sopromer.mg` | directeur.b@sopromer.mg |
| Magasin C | `manager@x; super@y` | -- | manager@x ET super@y |
| Magasin D | (vide) | (vide) | aucun email envoye |

### Format email

- **Subject** : `[SOPROMER] Session POS auto-fermee - <session.name> (<pos_config.name>)`
- **Body HTML** : intro + meme contenu que le chatter (balance_start, expected,
  balance_end_real, liste des mouvements de caisse)
- **From** : `company.email` ou fallback `noreply@sopromer.mg`
- **Mode** : `mail.mail` standard avec `auto_delete=True`, envoi async

### Tolerance erreur

Si l'envoi email echoue (DNS, SMTP down, adresse invalide) :

- `_logger.error()` avec stack trace complet
- La fermeture de session reste **valide** (pas de rollback)
- Le cron continue avec les autres sessions

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

## Historique des versions

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
