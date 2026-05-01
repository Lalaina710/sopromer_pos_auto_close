# -*- coding: utf-8 -*-
# Copyright 2026 SOPROMER
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
from odoo import fields, models


HOUR_SELECTION = [("%02d" % h, "%02d" % h) for h in range(24)]
MINUTE_SELECTION = [("%02d" % m, "%02d" % m) for m in range(0, 60, 5)]


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    pos_auto_close_enabled = fields.Boolean(
        string="Fermeture automatique des sessions POS",
        config_parameter='sopromer_pos_auto_close.enabled',
        default=True,
        help=(
            "Active globalement la fermeture automatique des sessions POS "
            "ouvertes. Peut etre surcharge par PdV via le toggle dans la "
            "fiche du Point de Vente."
        ),
    )
    pos_auto_close_hour_global = fields.Selection(
        selection=HOUR_SELECTION,
        string="Heure (HH) globale",
        config_parameter='sopromer_pos_auto_close.hour_global',
        default='19',
        help=(
            "Heure a laquelle le cron declenche la fermeture des sessions "
            "POS ouvertes (fuseau de la societe). Peut etre surcharge par "
            "PdV. Format 24h."
        ),
    )
    pos_auto_close_minute_global = fields.Selection(
        selection=MINUTE_SELECTION,
        string="Minute (MM) globale",
        config_parameter='sopromer_pos_auto_close.minute_global',
        default='00',
        help=(
            "Minute a laquelle le cron declenche la fermeture (granularite "
            "5 min). Peut etre surchargee par PdV."
        ),
    )
    pos_auto_close_email_to = fields.Char(
        string="Destinataire(s) email notification fermeture",
        config_parameter='sopromer_pos_auto_close.email_to',
        help=(
            "Adresse(s) email destinataire(s) de la notification envoyee "
            "apres chaque fermeture automatique de session POS. Plusieurs "
            "adresses peuvent etre separees par virgule (,) ou point-virgule "
            "(;). Laisser vide pour desactiver l'envoi global. Peut etre "
            "surcharge par PdV via le champ override dans la fiche du Point "
            "de Vente."
        ),
    )
