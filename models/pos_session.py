# -*- coding: utf-8 -*-
# Copyright 2026 SOPROMER
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import logging
import re
import traceback
from datetime import datetime

import pytz
from markupsafe import Markup

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

        Called every 5 minutes by ir.cron `sopromer_pos_auto_close.ir_cron_auto_close`.
        Wraps each session in its own try/except so one failure does not stop
        the whole batch.

        Accumulates results in `closed_results` (one dict per closed session)
        and sends 1 CONSOLIDATED email at the end of the run. Per-session
        chatter post stays for individual audit.
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

        # Lecture heure + minute globales. Si vide/non-définie → None (skip).
        # Permet d'installer le module sans déclencher de fermeture tant que
        # l'admin n'a pas défini d'heure (ni global, ni override par PdV).
        global_hour = self._read_int_icp(IrConfig, 'sopromer_pos_auto_close.hour_global')
        global_minute = self._read_int_icp(IrConfig, 'sopromer_pos_auto_close.minute_global')
        # Default minute = 0 si non défini, pour compat avec installs anciens.
        if global_minute is None:
            global_minute = 0

        opened_sessions = self.search([('state', '=', 'opened')])
        if not opened_sessions:
            _logger.info("[pos_auto_close] No opened session, nothing to do.")
            return

        _logger.info(
            "[pos_auto_close] Scanning %d opened session(s), global=%s",
            len(opened_sessions),
            "%02d:%02d" % (global_hour, global_minute) if global_hour is not None else "(empty)",
        )

        closed_results = []
        for session in opened_sessions:
            try:
                result = self._auto_close_dispatch(session, global_hour, global_minute)
                if result:
                    closed_results.append(result)
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

        # 1 consolidated email for the whole run.
        if closed_results:
            try:
                self._send_consolidated_auto_close_email(closed_results)
            except Exception as exc:  # noqa: BLE001
                _logger.error(
                    "[pos_auto_close] Consolidated email failed: %s\n%s",
                    exc, traceback.format_exc(),
                )

    @api.model
    def _fmt_money(self, amount):
        """Format montant avec espace milliers, virgule decimale, 2 chiffres.
        Ex: 857300 -> '857 300,00', 361767.04000000004 -> '361 767,04'.
        """
        try:
            v = round(float(amount or 0.0), 2)
        except (TypeError, ValueError):
            return str(amount)
        s = "{:,.2f}".format(v)  # ex '857,300.00'
        # FR : remplace , par espace et . par ,
        return s.replace(',', ' ').replace('.', ',').replace(' ', ' ')

    @api.model
    def _read_int_icp(self, IrConfig, key):
        """Lit ICP `key` et retourne int ou None si vide/invalide."""
        raw = IrConfig.get_param(key, default='')
        if raw in (False, None, ''):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            _logger.warning(
                "[pos_auto_close] Invalid ICP %s='%s', treated as empty.", key, raw,
            )
            return None

    @api.model
    def _auto_close_dispatch(self, session, global_hour, global_minute=0):
        """Decide whether `session` must be closed now and act accordingly.

        Returns:
          dict payload of closed session (suitable for consolidated email)
          OR None if skipped/not matching window.

        Skips silently when:
          - the PdV opted out (auto_close_enabled = False)
          - the current time is not within target window (5-min window)
          - the session is in opening_control
          - the session has draft orders pending
        """
        config = session.config_id
        if not config.auto_close_enabled:
            _logger.info(
                "[pos_auto_close] Session %s: PdV %s opted out, skip.",
                session.name, config.name,
            )
            return None

        # Priorité override PdV > heure globale.
        # Si heure globale ET override PdV vides → skip silencieux.
        override_h = config.auto_close_hour_override
        if override_h not in (False, None, ''):
            target_hour = int(override_h)
        elif global_hour is not None:
            target_hour = int(global_hour)
        else:
            _logger.info(
                "[pos_auto_close] Session %s: no hour configured (override + global empty), skip.",
                session.name,
            )
            return None

        # Minute : override PdV > globale > 0.
        override_m = config.auto_close_minute_override
        if override_m not in (False, None, ''):
            target_minute = int(override_m)
        elif global_minute is not None:
            target_minute = int(global_minute)
        else:
            target_minute = 0

        company_tz = session.company_id.partner_id.tz or 'Indian/Antananarivo'
        try:
            tz = pytz.timezone(company_tz)
        except pytz.UnknownTimeZoneError:
            tz = pytz.timezone('Indian/Antananarivo')
            _logger.warning(
                "[pos_auto_close] Unknown tz %s, fallback Indian/Antananarivo.",
                company_tz,
            )
        now_dt = datetime.now(tz)
        current_total = now_dt.hour * 60 + now_dt.minute
        target_total = target_hour * 60 + target_minute

        # Fenêtre 5 min (cron tourne toutes les 5 min). Idempotence garantie
        # par le check state='opened' (session déjà closed = skip à la prochaine).
        if not (target_total <= current_total < target_total + 5):
            return None

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
            return None

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
            return None

        # OK to close
        return session._auto_close_session()

    # ---------------------------------------------------------------------
    # Per-session close
    # ---------------------------------------------------------------------
    def _auto_close_session(self):
        """Close `self` automatically and return payload dict for the
        consolidated email.

        Logic:
          1. Move to closing_control (triggers Odoo's cash balance compute)
          2. Set balance_end_real := computed expected balance
          3. Finalize close
          4. Post detailed message in chatter (audit individuel : KEPT)
          5. Return dict {session, balance_start, expected_cash, now_str,
                          cash_moves_html} → consume par consolidated email.

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
            return None

        if self.state == 'closed':
            _logger.info(
                "[pos_auto_close] Session %s closed by closing_control directly.",
                self.name,
            )
        else:
            # 3. Si encore en closing_control, finaliser le close.
            self.action_pos_session_close()

        # 4. Detailed audit message in chatter (per-session = KEPT).
        cash_moves_lines = []
        for cm in self.statement_line_ids:
            amount = round(cm.amount or 0.0, 2)
            sign = '+' if amount >= 0 else '-'
            kind = _("CASH IN") if amount > 0 else (
                _("CASH OUT") if amount < 0 else _("CASH")
            )
            ref = cm.payment_ref or _('(sans libelle)')
            move_ref = (cm.move_id.name if cm.move_id else '') or _('(non poste)')
            cash_moves_lines.append(
                "<li>[%s] <b>%s</b> &mdash; %s : %s%s</li>" % (
                    kind,
                    move_ref,
                    ref,
                    sign,
                    self._fmt_money(abs(amount)),
                )
            )
        # Wrap en Markup pour que t-out (Odoo 18) ne ré-échappe pas le HTML
        # dans le template QWeb du mail consolidé. Sans Markup, le rendu
        # affiche les balises brutes (<ul><li>...) au lieu de la liste.
        cash_moves_html = (
            Markup("<ul>%s</ul>") % Markup("").join(
                Markup(line) for line in cash_moves_lines
            )
            if cash_moves_lines
            else Markup(_("<i>aucun mouvement de caisse</i>"))
        )

        body = _(
            "<b>Fermeture automatique a %(hour)s</b><br/>"
            "Solde initial : <b>%(start)s</b><br/>"
            "Solde theorique (calcule) : <b>%(expected)s</b><br/>"
            "Solde de cloture reel (auto-rempli) : <b>%(end)s</b><br/>"
            "<br/>"
            "<u>Mouvements de caisse :</u>%(moves)s",
            hour=now_str,
            start=self._fmt_money(balance_start),
            expected=self._fmt_money(expected_cash),
            end=self._fmt_money(expected_cash),
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

        # Payload consume by consolidated email. NB: pas d'envoi mail ici,
        # tout est centralise dans _send_consolidated_auto_close_email.
        return {
            'session': self,
            'balance_start': balance_start,
            'expected_cash': expected_cash,
            'now_str': now_str,
            'cash_moves_html': cash_moves_html,
        }

    # ---------------------------------------------------------------------
    # Manual force-close (bypass hour check)
    # ---------------------------------------------------------------------
    def action_force_auto_close(self):
        """Force fermeture auto sans verifier l'heure cible.

        Reuse `_auto_close_session()` (calcul balance + close).
        Garde les protections: opening_control, draft orders, opted-out PdV.
        Multi-record safe : itere et catche erreurs par session.
        Accumule resultats et envoie 1 SEUL email consolide a la fin.
        """
        closed = []
        skipped = []
        closed_results = []
        for session in self:
            config = session.config_id
            if not config.auto_close_enabled:
                skipped.append((session.name, "PdV %s opted out" % config.name))
                continue
            if session.state == 'opening_control':
                skipped.append((session.name, "opening_control"))
                continue
            if session.state != 'opened':
                skipped.append((session.name, "state=%s" % session.state))
                continue
            draft = session.order_ids.filtered(lambda o: o.state == 'draft')
            if draft:
                skipped.append((session.name, "%d draft order(s)" % len(draft)))
                continue
            try:
                result = session._auto_close_session()
                if result:
                    closed_results.append(result)
                closed.append(session.name)
            except Exception as exc:  # noqa: BLE001
                _logger.error(
                    "[pos_auto_close] Force close %s failed: %s\n%s",
                    session.name, exc, traceback.format_exc(),
                )
                skipped.append((session.name, "error: %s" % exc))

        # 1 mail consolide pour le batch force-close.
        if closed_results:
            try:
                self._send_consolidated_auto_close_email(closed_results)
            except Exception as exc:  # noqa: BLE001
                _logger.error(
                    "[pos_auto_close] Force close consolidated email failed: %s\n%s",
                    exc, traceback.format_exc(),
                )

        _logger.info(
            "[pos_auto_close] Force close batch: closed=%s skipped=%s",
            closed, skipped,
        )
        msg_lines = []
        if closed:
            msg_lines.append(_("Fermees : %s") % ", ".join(closed))
        if skipped:
            msg_lines.append(_("Ignorees : %s") % "; ".join(
                "%s (%s)" % (n, r) for n, r in skipped
            ))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Fermeture forcee"),
                'message': "\n".join(msg_lines) or _("Aucune session a traiter."),
                'type': 'success' if closed else 'warning',
                'sticky': bool(skipped),
            }
        }

    @api.model
    def _force_auto_close_all(self):
        """Server-action entry point: ferme toutes les sessions opened, bypass heure."""
        sessions = self.search([('state', '=', 'opened')])
        if not sessions:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _("Fermeture forcee"),
                    'message': _("Aucune session ouverte."),
                    'type': 'warning',
                    'sticky': False,
                },
            }
        return sessions.action_force_auto_close()

    # ---------------------------------------------------------------------
    # Email notification (consolide)
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

    @api.model
    def _send_consolidated_auto_close_email(self, closed_results):
        """Send ONE consolidated email for all closed sessions in this run.

        Args:
          closed_results: list[dict] returned by _auto_close_session, each
            containing keys session, balance_start, expected_cash, now_str,
            cash_moves_html.

        Recipients: union des destinataires de chaque session (override PdV
        sinon global ICP), dedupliques en preservant l'ordre.

        Skip silencieux si liste vide ou aucun destinataire resolu.

        Le template est attache a pos.session ; on rend avec la 1ere session
        en res_id pour fournir un record d'attache (chatter / followers du
        template), mais le corps utilise UNIQUEMENT le contexte injecte.
        """
        if not closed_results:
            return

        # Union recipients (dedupe preserve order).
        recipients = []
        seen = set()
        for r in closed_results:
            session = r['session']
            for addr in session._resolve_auto_close_email_recipients():
                key = addr.lower()
                if key not in seen:
                    seen.add(key)
                    recipients.append(addr)

        if not recipients:
            _logger.info(
                "[pos_auto_close] Consolidated email: %d session(s) closed but no recipient configured, skip.",
                len(closed_results),
            )
            return

        # Email from = SMTP user du serveur sortant actif (sinon fallback
        # company.email). Évite l'écart from_filter si company.email pointe
        # un domaine non autorisé par le serveur SMTP.
        mail_server = self.env['ir.mail_server'].sudo().search(
            [], order='sequence, id', limit=1,
        )
        first_session = closed_results[0]['session']
        company = first_session.company_id or self.env.company
        email_from = (
            (mail_server.smtp_user if mail_server else None)
            or company.email
            or self.env.user.email
            or 'noreply@sopromer.mg'
        )
        email_to = ', '.join(recipients)

        company_tz = company.partner_id.tz or 'Indian/Antananarivo'
        try:
            tz = pytz.timezone(company_tz)
        except pytz.UnknownTimeZoneError:
            tz = pytz.timezone('Indian/Antananarivo')
        date_str = datetime.now(tz).strftime('%d/%m/%Y')

        # Build ctx payload: list de dicts serialisables pour QWeb.
        sessions_payload = [
            {
                'name': r['session'].name,
                'pdv': r['session'].config_id.name,
                'now_str': r['now_str'],
                'balance_start_fmt': self._fmt_money(r['balance_start']),
                'expected_fmt': self._fmt_money(r['expected_cash']),
                'cash_moves_html': r['cash_moves_html'],
            }
            for r in closed_results
        ]
        ctx = {
            'sessions': sessions_payload,
            'count': len(closed_results),
            'date_str': date_str,
            'email_from': email_from,
            'email_to': email_to,
        }

        try:
            template = self.env.ref(
                'sopromer_pos_auto_close.mail_template_consolidated'
            )
        except ValueError:
            _logger.error(
                "[pos_auto_close] Template mail_template_consolidated introuvable, "
                "skip email consolide."
            )
            return

        # send_mail avec res_id = 1ere session (template attache pos.session).
        # email_values override email_to/email_from au cas ou le template
        # produit du vide (defense en profondeur).
        template.with_context(**ctx).send_mail(
            first_session.id,
            force_send=False,
            email_values={
                'email_to': email_to,
                'email_from': email_from,
                'auto_delete': True,
            },
        )
        _logger.info(
            "[pos_auto_close] Consolidated email queued: %d session(s) -> %s",
            len(closed_results), email_to,
        )
