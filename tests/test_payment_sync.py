# -*- coding: utf-8 -*-

from unittest.mock import patch
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import UserError


@tagged('post_install', '-at_install')
class TestQuickBooksPaymentSync(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.partner = self.env['res.partner'].create({
            'name': 'Test Payment Customer',
            'quickbooks_id': 'CUST_100',
        })
        self.journal = self.env['account.journal'].search([
            ('type', '=', 'bank'),
            ('company_id', '=', self.company.id),
        ], limit=1)

        self.payment = self.env['account.payment'].create({
            'payment_type': 'inbound',
            'partner_type': 'customer',
            'partner_id': self.partner.id,
            'amount': 250.0,
            'journal_id': self.journal.id,
            'currency_id': self.env.ref('base.USD').id,
            'date': '2026-10-31',
        })

    def test_queue_export_rejects_draft(self):
        self.assertEqual(self.payment.state, 'draft')
        with self.assertRaises(UserError) as cm:
            self.payment.action_quickbooks_queue_export()
        self.assertIn('Only posted inbound customer payments can be exported', str(cm.exception))

    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.check_currency_support')
    def test_queue_export_rejects_outbound(self, mock_guard):
        mock_guard.return_value = True
        self.payment.payment_type = 'outbound'
        self.payment.action_post()
        with self.assertRaises(UserError) as cm:
            self.payment.action_quickbooks_queue_export()
        self.assertIn('Only posted inbound customer payments can be exported', str(cm.exception))

    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.check_currency_support')
    def test_queue_export_success_and_duplicate_prevention(self, mock_guard):
        mock_guard.return_value = True
        self.payment.action_post()
        self.payment.action_quickbooks_queue_export()

        self.assertEqual(self.payment.quickbooks_sync_status, 'queued')
        log = self.env['quickbooks.sync.log'].search([
            ('model_name', '=', 'account.payment'),
            ('res_id', '=', self.payment.id),
        ])
        self.assertTrue(log)

        # Attempting to queue again should be blocked
        with self.assertRaises(UserError) as cm:
            self.payment.action_quickbooks_queue_export()
        self.assertIn('An export queue record already exists', str(cm.exception))

    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.check_currency_support')
    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.find_payment_by_ref_num')
    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.request')
    def test_payment_idempotency_precheck_reconciles(self, mock_request, mock_find, mock_guard):
        mock_guard.return_value = True
        self.payment.action_post()

        # Simulate existing payment in QBO
        mock_find.return_value = {
            'Id': 'PAY_QBO_999',
            'PaymentRefNum': (self.payment.name or '')[:21],
            'CustomerRef': {'value': 'CUST_100'},
            'TotalAmt': 250.0,
        }

        qb_id = self.payment._quickbooks_export_payment()

        self.assertEqual(qb_id, 'PAY_QBO_999')
        self.assertEqual(self.payment.quickbooks_id, 'PAY_QBO_999')
        self.assertEqual(self.payment.quickbooks_sync_status, 'synced')
        # Crucial check: verify that no new POST was issued!
        mock_request.assert_not_called()

    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.check_currency_support')
    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.find_payment_by_ref_num')
    def test_payment_customer_conflict_aborts(self, mock_find, mock_guard):
        mock_guard.return_value = True
        self.payment.action_post()

        # Simulate existing payment under a different customer in QBO
        mock_find.return_value = {
            'Id': 'PAY_QBO_CONFLICT',
            'PaymentRefNum': (self.payment.name or '')[:21],
            'CustomerRef': {'value': 'DIFFERENT_CUST'},
            'TotalAmt': 250.0,
        }

        with self.assertRaises(UserError) as cm:
            self.payment._quickbooks_export_payment()
        self.assertIn('belongs to customer', str(cm.exception))

    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.check_currency_support')
    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.get_company_preferences')
    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.find_payment_by_ref_num')
    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.request')
    def test_payment_ambiguous_post_recovery(self, mock_request, mock_find, mock_get_prefs, mock_guard):
        mock_guard.return_value = True
        mock_get_prefs.return_value = {'multicurrency_enabled': False, 'home_currency': 'USD', 'company_name': 'Test Co'}
        self.payment.action_post()

        recovered_pay = {
            'Id': 'PAY_QBO_RECOVERED',
            'PaymentRefNum': (self.payment.name or '')[:21],
            'CustomerRef': {'value': 'CUST_100'},
            'TotalAmt': 250.0,
        }
        mock_find.side_effect = [None, recovered_pay]
        mock_request.side_effect = Exception('HTTPSConnectionPool: Connection reset by peer.')

        qb_id = self.payment._quickbooks_export_payment()

        self.assertEqual(qb_id, 'PAY_QBO_RECOVERED')
        self.assertEqual(self.payment.quickbooks_id, 'PAY_QBO_RECOVERED')
        self.assertEqual(self.payment.quickbooks_sync_status, 'synced')

