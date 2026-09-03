# -*- coding: utf-8 -*-
# Copyright 2026 SOPROMER
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import re

from odoo import fields
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

# Extrait le tag et le libelle d'un <li> du bloc "Mouvements de caisse" :
#   <li>[VENTE DU JOUR] <b>CSH10/26-27/0242</b> &mdash; POS/05818 : +364 077,78</li>
_LI_RE = re.compile(r'\[([^\]]+)\]\s*<b>(.*?)</b>\s*(?:&mdash;|—)\s*(.*?)\s*:\s')


@tagged('post_install', '-at_install')
class TestAutoCloseCashMoves(TransactionCase):
    """Couvre l'heuristique de discrimination des lignes de releve introduite
    en 18.0.1.4.5 : seule la ligne de reglement des ventes especes du jour doit
    porter `[VENTE DU JOUR]`, les apports/retraits et les ecarts de cloture
    gardent `[CASH IN]` / `[CASH OUT]`.

    On ne simule pas une vente POS complete (fixture trop lourde et hors sujet):
    le critere lu par le code est le `account_type` de la contrepartie de
    l'ecriture de la ligne de releve. Cette contrepartie est resolue a la
    creation sur `journal.suspense_account_id`, ce qui permet de reproduire
    exactement les trois signatures comptables reelles :
      - vente du jour  -> `asset_receivable` (pose par _get_combine/_split_...)
      - cash in / out  -> `asset_current`    (compte d'attente du journal)
      - ecart cloture  -> `expense`          (compte de perte)
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.config = cls.env['pos.config'].create({'name': 'TEST-AUTO-CLOSE'})
        cls.config.open_ui()
        cls.session = cls.config.current_session_id
        # Fond de caisse non nul : garantit que l'ecart de test ne tombe pas
        # dans le nettoyage de l'ecart fantome (`abs(amount + balance_start)`).
        if cls.session.state == 'opening_control':
            cls.session.set_opening_control(500.0, '')

        cls.cash_journal = cls.session.payment_method_ids.filtered(
            lambda pm: pm.is_cash_count
        )[:1].journal_id
        assert cls.cash_journal, (
            "Le PdV de test n'a pas de mode de paiement especes : la fixture "
            "ne peut pas creer de lignes de releve."
        )
        cls.partner = cls.env['res.partner'].create({'name': 'TEST AutoClose'})

        # Contrepartie "vente" : compte client. Les deux autres reutilisent le
        # compte d'attente natif du journal (`asset_current`) et un compte de
        # charge cree pour l'occasion.
        cls.acc_receivable = cls.env['account.account'].create({
            'name': 'TEST AutoClose Client',
            'code': 'TACR41',
            'account_type': 'asset_receivable',
            'reconcile': True,
        })
        cls.acc_current = cls.cash_journal.suspense_account_id
        cls.acc_expense = cls.env['account.account'].create({
            'name': 'TEST AutoClose Perte especes',
            'code': 'TACE65',
            'account_type': 'expense',
            'reconcile': True,
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _make_statement_line(self, account, amount, ref):
        """Cree une ligne de releve de session dont la contrepartie tombe sur
        `account`.

        La contrepartie d'une `account.bank.statement.line` est le compte
        d'attente du journal, resolu au moment du create (l'ecriture est
        ensuite postee, donc non modifiable). On permute donc le compte
        d'attente autour du create, puis on le restaure.
        """
        journal = self.cash_journal
        original = journal.suspense_account_id
        journal.suspense_account_id = account
        try:
            return self.env['account.bank.statement.line'].create({
                'pos_session_id': self.session.id,
                'journal_id': journal.id,
                'date': fields.Date.context_today(self.session),
                'payment_ref': ref,
                'amount': amount,
                'partner_id': self.partner.id,
            })
        finally:
            journal.suspense_account_id = original

    def _auto_close_body(self):
        """Ferme la session et retourne le corps du message d'audit."""
        self.session._auto_close_session()
        messages = self.session.message_ids.filtered(
            lambda m: 'Fermeture automatique' in (m.body or '')
        )
        self.assertTrue(
            messages, "Le message d'audit de fermeture automatique est absent "
                      "du chatter de la session.",
        )
        return messages[0].body

    def _tags_by_ref(self, body):
        """Retourne {libelle_ligne: tag} depuis le bloc mouvements de caisse."""
        return {m.group(3): m.group(1) for m in _LI_RE.finditer(body)}

    # ------------------------------------------------------------------
    # Discrimination des lignes
    # ------------------------------------------------------------------
    def test_only_cash_sale_line_is_tagged_vente_du_jour(self):
        """Seule la ligne a contrepartie client porte `[VENTE DU JOUR]`.

        La fixture est construite pour etre *contraignante* : chaque leurre
        plausible d'implementation naive doit mistaguer au moins une ligne.

        ========================= ================== ======= ==============
        Libelle                   Contrepartie       Montant Attendu
        ========================= ================== ======= ==============
        POS/09913                 asset_current       +1500  CASH IN
        VENTE COMPTOIR - avance   asset_current        +300  CASH IN
        POS/09912                 asset_receivable    +1000  VENTE DU JOUR
        Ecart de caisse - cloture expense               -25  CASH OUT
        ========================= ================== ======= ==============

        - matching de libelle : la vente (`POS/09912`) ne contient pas
          « vente » et n'est pas le nom de session, tandis qu'un apport porte
          `POS/09913` — libelle *de meme forme* que la vente. Toute regle de
          libelle (`'VENTE' in ref`, `startswith('POS/')`, `^POS/\\d+$`,
          `'/' in ref`, `ref == session.name`) mistague au moins une ligne
        - montant : la vente (1000) n'est ni le plus gros positif (1500) ni le
          plus petit (300) — aucune regle « max », « min » ou « > seuil »
          ne l'isole
        - signe : trois positives sur quatre, une regle de signe ne distingue
          pas la vente des deux autres positives
        - rang de creation : la vente est creee en 3e, ni 1re ni derniere

        Seul le `account_type` de la contrepartie discrimine — c'est-a-dire
        exactement le mecanisme introduit en 1.4.5. Verifie en rejouant la
        fixture contre 15 implementations naives : toutes echouent.
        """
        # Apport de caisse portant un libelle de MEME FORME que la vente :
        # c'est lui qui interdit toute heuristique de libelle, y compris un
        # simple prefixe `POS/`.
        self._make_statement_line(self.acc_current, 1500.0, 'POS/09913')
        # Piege inverse : libelle qui crie « vente » mais contrepartie compte
        # d'attente. Doit rester un mouvement de caisse.
        self._make_statement_line(self.acc_current, 300.0, 'VENTE COMPTOIR - avance')
        # La vente : reconnaissable uniquement a sa contrepartie client.
        self._make_statement_line(self.acc_receivable, 1000.0, 'POS/09912')
        # Libelle accentue volontaire : verifie au passage que le nettoyage de
        # l'ecart fantome n'avale pas un ecart de cloture legitime (il ne cible
        # que les montants opposes exacts au fond de caisse : ici
        # `abs(-25 + 500) = 475`). Pas d'apostrophe, elle serait echappee en
        # `&#39;` (cf. test d'echappement).
        self._make_statement_line(self.acc_expense, -25.0, 'Écart de caisse - clôture')

        tags = self._tags_by_ref(self._auto_close_body())

        # Les 4 lignes doivent etre rendues : ni avalee par le nettoyage de
        # l'ecart fantome, ni ratee par le parsing (sinon les assertions
        # ci-dessous deviendraient vacantes).
        self.assertEqual(len(tags), 4, "4 lignes attendues, obtenu : %s" % tags)

        self.assertEqual(tags.get('POS/09912'), 'VENTE DU JOUR')
        self.assertEqual(tags.get('POS/09913'), 'CASH IN')
        self.assertEqual(tags.get('VENTE COMPTOIR - avance'), 'CASH IN')
        self.assertEqual(tags.get('Écart de caisse - clôture'), 'CASH OUT')
        # Une seule ligne taguee vente : le repli conservateur ne doit jamais
        # promouvoir un mouvement de caisse en vente.
        self.assertEqual(
            [ref for ref, tag in tags.items() if tag == 'VENTE DU JOUR'],
            ['POS/09912'],
        )

    # ------------------------------------------------------------------
    # Echappement du texte libre
    # ------------------------------------------------------------------
    def test_payment_ref_html_is_escaped_but_template_stays_html(self):
        """`payment_ref` est saisi librement par le caissier : il doit etre
        echappe, sans casser le HTML du gabarit (`&mdash;`, `<li>`, `<b>`).
        """
        self._make_statement_line(
            self.acc_current, 75.0, "Retrait <b>Naka</b> & vola d'avance",
        )

        body = self._auto_close_body()

        # Le libelle hostile ne doit pas etre injecte comme balise...
        self.assertNotIn('<b>Naka</b>', body)
        # ...et il doit ressortir ECHAPPE, pas simplement strippe. Sans cette
        # assertion, un `html_sanitize` qui retire la balise ferait passer le
        # test alors que notre echappement serait absent : c'est ici qu'on
        # epingle la cause reelle.
        self.assertIn('&lt;b&gt;Naka&lt;/b&gt;', body)
        # L'apostrophe est le cas courant en prod (libelles POS francais) :
        # elle sort en entite HTML valide, jamais en double echappement.
        self.assertNotIn('&amp;#39;', body)
        # Le gabarit reste du HTML : la liste est structuree et le tiret
        # cadratin est rendu comme tel, pas affiche en texte litteral.
        self.assertIn('<li>', body)
        self.assertNotIn('&amp;mdash;', body)
        self.assertTrue(
            '&mdash;' in body or '—' in body,
            "Le tiret cadratin doit rester du HTML rendu, pas du texte brut.",
        )
