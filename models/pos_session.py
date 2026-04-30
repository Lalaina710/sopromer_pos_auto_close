# -*- coding: utf-8 -*-
# Copyright 2026 SOPROMER
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import logging
import re
import traceback
from datetime import datetime

import pytz

from odoo import _, api, models

_logger = logging.getLogger(__name__)

# Match comma or semicolon separators in email recipient lists.
_EMAIL_SPLIT_RE = re.compile(r'[;,]')


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

        # 1. Pre-fill balance_end_real = balance_end (expected cash).
        # cash_register_balance_end est un computed natif :
        #   balance_start + sum(stmt_lines.amount) + total_cash_payment
        # En settant balance_end_real = balance_end AVANT closing_control,
        # cash_register_difference reste 0 → pas de perte/gain comptable.
        # Sans ça, le solde de fond reste "perdu" car balance_end_real=0.
        expected_cash = self.cash_register_balance_end
        self.write({
            'cash_register_balance_end_real': expected_cash,
        })
        # Flush + invalider cache pour que action_pos_session_closing_control
        # voit la nouvelle valeur lors de son compute interne.
        self.flush_recordset(['cash_register_balance_end_real'])
        self.invalidate_recordset(['cash_register_balance_end', 'cash_register_difference'])

        # 2. Move to closing_control (Odoo 18 peut finaliser direct vers
        # 'closed' si balance_end_real déjà set et pas de diff).
        if self.state == 'opened':
            self.action_pos_session_closing_control()

        # 3. Cleanup phantom "Écart d'espèces" stmt_line si égale à -balance_start
        # (artefact Odoo 18 sur sessions sans transactions où la diff
        # apparaît malgré balance_end_real correct). Détection stricte :
        # amount opposé exact à balance_start ET label contient 'Écart'.
        self = self.exists()
        if self:
            phantom_ecart = self.sudo().statement_line_ids.filtered(
                lambda sl: (
                    sl.payment_ref
                    and ('Écart' in sl.payment_ref or 'Cash difference' in sl.payment_ref)
                    and abs(sl.amount + balance_start) < 0.01
                )
            )
            if phantom_ecart:
                _logger.warning(
                    "[pos_auto_close] Session %s: phantom écart stmt_line "
                    "%s (%s) detected, unlinking.",
                    self.name, phantom_ecart.ids, phantom_ecart.mapped('amount'),
                )
                phantom_ecart.sudo().unlink()

        # Refresh after state change.
        self = self.exists()
        if not self:
            return

        close_result = None

        if self.state == 'closed':
            _logger.info(
                "[pos_auto_close] Session %s closed by closing_control directly.",
                self.name,
            )
        else:
            # 3. Si encore en closing_control, finaliser le close.
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

        # 5. Best-effort email notification (never block close on failure).
        try:
            self._send_auto_close_email(
                body_html=body,
                expected_cash=expected_cash,
                balance_start=balance_start,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.error(
                "[pos_auto_close] Session %s: email notification failed: %s\n%s",
                self.name, exc, traceback.format_exc(),
            )

        # Pass through any wizard/action returned by core close.
        return close_result

    # ---------------------------------------------------------------------
    # Email notification
    # ---------------------------------------------------------------------
    def _resolve_auto_close_email_recipients(self):
        """Return cleaned email recipient list for `self`.

        Resolution order:
          1. PdV override (`config.auto_close_email_to_override`)
          2. Global ICP `sopromer_pos_auto_close.email_to`

        Splits on `,` or `;`, strips whitespace, drops empty entries.
        """
        self.ensure_one()
        override = self.config_id.auto_close_email_to_override or ''
        if override.strip():
            raw = override
        else:
            raw = self.env['ir.config_parameter'].sudo().get_param(
                'sopromer_pos_auto_close.email_to', default=''
            ) or ''
        if not raw.strip():
            return []
        return [
            addr.strip()
            for addr in _EMAIL_SPLIT_RE.split(raw)
            if addr and addr.strip()
        ]

    def _send_auto_close_email(self, body_html, expected_cash, balance_start):
        """Send email notification after a successful auto-close.

        Silently skips when no recipient is configured. Caller wraps in
        try/except so any SMTP/DNS error is swallowed and never blocks the
        cron loop.
        """
        self.ensure_one()
        recipients = self._resolve_auto_close_email_recipients()
        if not recipients:
            _logger.info(
                "[pos_auto_close] Session %s: no email recipient, skip mail.",
                self.name,
            )
            return

        # Email from = SMTP user du serveur sortant actif (sinon fallback
        # company.email). Évite l'écart from_filter si company.email pointe
        # un domaine non autorisé par le serveur SMTP.
        mail_server = self.env['ir.mail_server'].sudo().search(
            [], order='sequence, id', limit=1,
        )
        company = self.company_id or self.env.company
        email_from = (
            (mail_server.smtp_user if mail_server else None)
            or company.email
            or self.env.user.email
            or 'noreply@sopromer.mg'
        )
        email_to = ', '.join(recipients)

        subject = _(
            "[SOPROMER] Session POS auto-fermee - %(session)s (%(pdv)s)",
            session=self.name,
            pdv=self.config_id.name,
        )

        intro = _(
            "<p>Notification automatique : la session POS "
            "<b>%(session)s</b> du PdV <b>%(pdv)s</b> a ete fermee "
            "automatiquement.</p>",
            session=self.name,
            pdv=self.config_id.name,
        )
        full_body = intro + body_html

        mail_vals = {
            'subject': subject,
            'body_html': full_body,
            'email_from': email_from,
            'email_to': email_to,
            'auto_delete': True,
        }
        mail = self.env['mail.mail'].sudo().create(mail_vals)
        mail.send()  # async send via mail.mail standard pipeline
        _logger.info(
            "[pos_auto_close] Session %s: email queued to %s (mail.id=%s).",
            self.name, email_to, mail.id,
        )
