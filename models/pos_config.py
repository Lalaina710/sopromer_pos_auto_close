# -*- coding: utf-8 -*-
# Copyright 2026 SOPROMER
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
from odoo import fields, models


HOUR_SELECTION = [("%02d" % h, "%02d:00" % h) for h in range(24)]


class PosConfig(models.Model):
    _inherit = 'pos.config'

    auto_close_enabled = fields.Boolean(
        string="Fermeture automatique activee",
        default=True,
        help=(
            "Si coche, ce PdV sera ferme automatiquement par le cron a l'heure "
            "definie (heure globale ou override ci-dessous). Decochez pour "
            "exclure ce PdV de la fermeture automatique."
        ),
    )
    auto_close_hour_override = fields.Selection(
        selection=HOUR_SELECTION,
        string="Heure de fermeture (override)",
        help=(
            "Si defini, surcharge l'heure de fermeture globale pour ce PdV. "
            "Laisser vide pour utiliser l'heure globale configuree dans les "
            "parametres Point de Vente."
        ),
    )
    auto_close_email_to_override = fields.Char(
        string="Destinataire(s) email (override)",
        help=(
            "Si defini, surcharge le destinataire global de la notification "
            "email envoyee apres fermeture automatique pour ce PdV. Plusieurs "
            "adresses peuvent etre separees par virgule (,) ou point-virgule "
            "(;). Laisser vide pour utiliser le destinataire global configure "
            "dans les parametres Point de Vente."
        ),
    )
