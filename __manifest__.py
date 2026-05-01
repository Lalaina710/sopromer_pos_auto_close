# -*- coding: utf-8 -*-
# Copyright 2026 SOPROMER
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
{
    'name': 'SOPROMER POS Auto Close',
    'version': '18.0.1.3.2',
    'category': 'Point of Sale',
    'summary': 'Fermeture automatique sessions POS avec calcul auto balance + notification email',
    'description': """
SOPROMER - Fermeture automatique sessions POS
==============================================

Ferme automatiquement les sessions POS encore ouvertes a une heure programmee
(par defaut 19h, fuseau Madagascar). Le balance_end_real est calcule
automatiquement a partir de la balance_start + mouvements de cash traces dans
Odoo (pas de comptage manuel requis).

Fonctionnalites
---------------
* Configuration globale dans Settings > Point de Vente :
  - Toggle global "Fermeture automatique" (active par defaut)
  - Heure de fermeture globale (selection 00:00..23:00, defaut 19:00)
* Configuration par PdV (form pos.config) :
  - Toggle par PdV (active par defaut)
  - Override de l'heure pour ce PdV (optionnel)
* Cron horaire qui parcourt les sessions ouvertes et declenche la fermeture
  quand l'heure courante == heure cible
* Fermeture sequentielle, transactions isolees, log message_post detaille
  (balance_start, expected_cash, balance_end_real, mouvements de caisse)
* Edge cases :
  - Sessions en opening_control : skip + warning
  - Sessions avec orders draft : skip + activity manager
  - Erreurs : log + chatter, pas de crash cron

Cas d'usage business
--------------------
SOPROMER opere 40 PdV a Madagascar. Les caissiers oublient regulierement de
fermer leur session le soir, ce qui empeche la generation des rapports et
pollue le pipeline reporting. Ce module automatise la cloture pour garantir
que tous les PdV demarrent une nouvelle session le lendemain matin.
    """,
    'author': 'SOPROMER',
    'website': 'https://github.com/Lalaina710/sopromer_pos_auto_close',
    'license': 'LGPL-3',
    'depends': [
        'point_of_sale',
        'mail',
    ],
    'data': [
        'data/ir_cron.xml',
        'data/server_actions.xml',
        'views/pos_config_view.xml',
        'views/res_config_settings_view.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
