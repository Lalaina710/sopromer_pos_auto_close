# -*- coding: utf-8 -*-
# Copyright 2026 SOPROMER
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import logging
import traceback
from datetime import datetime

import pytz

from odoo import _, api, models

_logger = logging.getLogger(__name__)


class PosSession(models.Model):
    _inherit = 'pos.session'

    # ---------------------------------------------------------------------
    # Cron entry point
    # ---------------------------------------------------------------------
    @api.model
    def _cron_auto_close_sessions(self):
        """Iterate opened POS sessions and auto-close those whose target
        hour matches the current hour in the company timezone.

        Called every hour by ir.cron `sopromer_pos_auto_close.ir_cron_auto_close`.
        Wraps each session in its own try/except so one failure does not stop
        the whole batch.
        """
        IrConfig = self.env['ir.config_parameter'].sudo()
        enabled_global = IrConfig.get_param(
            'sopromer_pos_auto_close.enabled', default='True'
        )
        if str(enabled_global).lower() not in ('true', '1'):
            _logger.info(
                "[pos_auto_close] Disabled globally, cron skipped."
            )
            return

        try:
            global_hour = int(
                IrConfig.get_param(
                    'sopromer_pos_auto_close.hour_global', default=19
                )
            )
        except (TypeError, ValueError):
            global_hour = 19
            _logger.warning(
                "[pos_auto_close] Invalid hour_global, fallback to 19h."
            )

        opened_sessions = self.search([('state', '=', 'opened')])
        if not opened_sessions:
            _logger.info("[pos_auto_close] No opened session, nothing to do.")
            return

        _logger.info(
            "[pos_auto_close] Scanning %d opened session(s), global_hour=%dh",
            len(opened_sessions), global_hour,
        )

        for session in opened_sessions:
            try:
                self._auto_close_dispatch(session, global_hour)
            except Exception as exc:  # noqa: BLE001
                # Defensive: never let one session crash the whole cron.
                _logger.error(
                    "[pos_auto_close] Session %s: unexpected error: %s\n%s",
                    session.name, exc, traceback.format_exc(),
                )
                try:
                    session.message_post(
                        body=_(
                            "Erreur fermeture automatique :<br/><pre>%s</pre>",
                            traceback.format_exc(),
                        ),
                        message_type='comment',
                        subtype_xmlid='mail.mt_note',
                    )
                except Exception:  # noqa: BLE001
                    # Chatter post should never break the loop either.
                    pass

    @api.model
    def _auto_close_dispatch(self, session, global_hour):
        """Decide whether `session` must be closed now and act accordingly.

        Skips silently when:
          - the PdV opted out (auto_close_enabled = False)
          - the current hour is not the target hour for this PdV
          - the session is in opening_control
          - the session has draft orders pending
        """
        config = session.config_id
        if not config.auto_close_enabled:
            _logger.info(
                "[pos_auto_close] Session %s: PdV %s opted out, skip.",
                session.name, config.name,
            )
            return

        target_hour = (
            config.auto_close_hour_override
            if config.auto_close_hour_override not in (False, None)
            else global_hour
        )

        company_tz = session.company_id.partner_id.tz or 'Indian/Antananarivo'
        try:
            tz = pytz.timezone(company_tz)
        except pytz.UnknownTimeZoneError:
            tz = pytz.timezone('Indian/Antananarivo')
            _logger.warning(
                "[pos_auto_close] Unknown tz %s, fallback Indian/Antananarivo.",
                company_tz,
            )
        current_hour = datetime.now(tz).hour

        if current_hour != int(target_hour):
            return

        # Edge case 1: opening_control (cashier never validated opening)
        if session.state == 'opening_control':
            _logger.warning(
                "[pos_auto_close] Session %s stuck in opening_control, skip.",
                session.name,
            )
            session.message_post(
                body=_(
                    "Fermeture automatique annulee : la session est encore en "
                    "controle d'ouverture (le caissier n'a pas valide le fond "
                    "de caisse)."
                ),
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )
            return

        # Edge case 2: draft orders still pending
        draft_orders = session.order_ids.filtered(lambda o: o.state == 'draft')
        if draft_orders:
            _logger.warning(
                "[pos_auto_close] Session %s has %d draft order(s), skip + activity.",
                session.name, len(draft_orders),
            )
            session.activity_schedule(
                'mail.mail_activity_data_todo',
                user_id=session.user_id.id or self.env.user.id,
                summary=_("Session POS non fermee : commandes en draft"),
                note=_(
                    "La fermeture automatique a ete annulee car la session "
                    "%(session)s contient %(count)d commande(s) en brouillon. "
                    "Validez ou supprimez ces commandes avant de fermer la "
                    "session.",
                    session=session.name,
                    count=len(draft_orders),
                ),
            )
            return

        # OK to close
        session._auto_close_session()

    # ---------------------------------------------------------------------
    # Per-session close
    # ---------------------------------------------------------------------
    def _auto_close_session(self):
        """Close `self` automatically.

        Logic:
          1. Move to closing_control (triggers Odoo's cash balance compute)
          2. Set balance_end_real := computed expected balance
          3. Finalize close
          4. Post detailed message in chatter

        Must be called on a single session (ensure_one). Caller wraps in
        try/except.
        """
        self.ensure_one()
        balance_start = self.cash_register_balance_start
        company_tz = self.company_id.partner_id.tz or 'Indian/Antananarivo'
        try:
            tz = pytz.timezone(company_tz)
        except pytz.UnknownTimeZoneError:
            tz = pytz.timezone('Indian/Antananarivo')
        now_str = datetime.now(tz).strftime('%H:%M')

        _logger.info(
            "[pos_auto_close] Closing session %s (PdV %s) at %s ...",
            self.name, self.config_id.name, now_str,
        )

        # 1. Move to closing_control (Odoo computes cash_register_balance_end
        #    = balance_start + total_entry from cash moves & payments).
        if self.state == 'opened':
            self.action_pos_session_closing_control()

        # Refresh after state change.
        self = self.exists()
        if not self:
            return

        expected_cash = self.cash_register_balance_end

        # 2. Auto-fill balance_end_real with the expected balance, since
        #    cashiers won't physically count cash at this hour.
        self.write({
            'cash_register_balance_end_real': expected_cash,
        })

        # 3. Finalize closing.
        close_result = self.action_pos_session_close()

        # 4. Detailed audit message in chatter.
        cash_moves_lines = []
        for cm in self.statement_line_ids:
            sign = '+' if cm.amount >= 0 else ''
            cash_moves_lines.append(
                "<li>%s : %s%s</li>" % (
                    cm.payment_ref or _('(sans libelle)'),
                    sign,
                    cm.amount,
                )
            )
        cash_moves_html = (
            "<ul>%s</ul>" % "".join(cash_moves_lines)
            if cash_moves_lines else _("<i>aucun mouvement de caisse</i>")
        )

        body = _(
            "<b>Fermeture automatique a %(hour)s</b><br/>"
            "Solde initial : <b>%(start)s</b><br/>"
            "Solde theorique (calcule) : <b>%(expected)s</b><br/>"
            "Solde de cloture reel (auto-rempli) : <b>%(end)s</b><br/>"
            "<br/>"
            "<u>Mouvements de caisse :</u>%(moves)s",
            hour=now_str,
            start=balance_start,
            expected=expected_cash,
            end=expected_cash,
            moves=cash_moves_html,
        )
        self.message_post(
            body=body,
            message_type='comment',
            subtype_xmlid='mail.mt_note',
        )

        _logger.info(
            "[pos_auto_close] Session %s closed (start=%s expected=%s).",
            self.name, balance_start, expected_cash,
        )

        # Pass through any wizard/action returned by core close.
        return close_result
