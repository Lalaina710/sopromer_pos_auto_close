# -*- coding: utf-8 -*-
# Copyright 2026 SOPROMER
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
from odoo import fields, models


HOUR_SELECTION = [(h, "%02d:00" % h) for h in range(24)]


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
        string="Heure de fermeture automatique (globale)",
        config_parameter='sopromer_pos_auto_close.hour_global',
        default=19,
        help=(
            "Heure a laquelle le cron declenche la fermeture des sessions "
            "POS ouvertes (fuseau de la societe). Peut etre surcharge par "
            "PdV. Format 24h."
        ),
    )
