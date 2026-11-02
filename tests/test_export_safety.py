# -*- coding: utf-8 -*-

from unittest.mock import patch, MagicMock
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import UserError


@tagged('post_install', '-at_install')
class TestQuickBooksExportSafety(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.partner = self.env['res.partner'].create({
            'name': 'Test Safety Partner',
            'quickbooks_id': '999',
        })
        self.product_tmpl = self.env['product.template'].create({
            'name': 'Test Safety Product',
            'type': 'service',
            'quickbooks_id': '888',
        })
        self.product = self.product_tmpl.product_variant_id

        # Create a basic invoice
        self.invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'currency_id': self.env.ref('base.USD').id,
            'invoice_date': '2026-10-30',
            'invoice_line_ids': [
                (0, 0, {
                    'product_id': self.product.id,
                    'quantity': 1.0,
                    'price_unit': 100.0,
                    'tax_ids': [(6, 0, [])],
                })
            ]
        })

    def test_queue_export_rejects_draft(self):
        self.assertEqual(self.invoice.state, 'draft')
        with self.assertRaises(UserError) as cm:
            self.invoice.action_quickbooks_queue_export()
        self.assertIn('Only posted customer invoices and credit notes can be exported', str(cm.exception))

    def test_queue_export_rejects_existing_qbo_id(self):
        self.invoice.action_post()
        self.invoice.quickbooks_id = '12345'
        with self.assertRaises(UserError) as cm:
            self.invoice.action_quickbooks_queue_export()
        self.assertIn('already has a QuickBooks ID', str(cm.exception))

    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.check_currency_support')
    def test_queue_export_rejects_duplicate_queue(self, mock_guard):
        mock_guard.return_value = True
        self.invoice.action_post()
        self.invoice.action_quickbooks_queue_export()

        with self.assertRaises(UserError) as cm:
            self.invoice.action_quickbooks_queue_export()
        self.assertIn('An export queue record already exists', str(cm.exception))

    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.check_currency_support')
    def test_export_rejects_unmapped_product(self, mock_guard):
        mock_guard.return_value = True
        self.product_tmpl.quickbooks_id = False
        self.invoice.action_post()

        with self.assertRaises(UserError) as cm:
            self.invoice._quickbooks_export_invoice()
        self.assertIn('Map product/service', str(cm.exception))

    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.check_currency_support')
    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.find_invoice_by_doc_number')
    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.request')
    def test_idempotency_precheck_reconciles_existing_qbo_invoice(self, mock_request, mock_find, mock_guard):
        mock_guard.return_value = True
        self.invoice.action_post()

        # Simulate that QBO already has an invoice with matching DocNumber and Customer
        mock_find.return_value = {
            'Id': '777111',
            'DocNumber': (self.invoice.name or '')[:21],
            'CustomerRef': {'value': '999'},
            'TotalAmt': 100.0,
        }

        qb_id = self.invoice._quickbooks_export_invoice()

        self.assertEqual(qb_id, '777111')
        self.assertEqual(self.invoice.quickbooks_id, '777111')
        self.assertEqual(self.invoice.quickbooks_sync_status, 'synced')
        # Crucial check: verify that no new POST was issued!
        mock_request.assert_not_called()

    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.check_currency_support')
    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.find_invoice_by_doc_number')
    def test_idempotency_precheck_aborts_on_customer_conflict(self, mock_find, mock_guard):
        mock_guard.return_value = True
        self.invoice.action_post()

        # Simulate matching DocNumber but DIFFERENT Customer in QBO
        mock_find.return_value = {
            'Id': '777222',
            'DocNumber': (self.invoice.name or '')[:21],
            'CustomerRef': {'value': 'CONFLICT_CUSTOMER'},
            'TotalAmt': 100.0,
        }

        with self.assertRaises(UserError) as cm:
            self.invoice._quickbooks_export_invoice()
        self.assertIn('Manual reconciliation required', str(cm.exception))

    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.check_currency_support')
    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.get_company_preferences')
    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.find_invoice_by_doc_number')
    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.request')
    def test_ambiguous_post_network_recovery(self, mock_request, mock_find, mock_get_prefs, mock_guard):
        mock_guard.return_value = True
        mock_get_prefs.return_value = {'multicurrency_enabled': False, 'home_currency': 'USD', 'company_name': 'Test Co'}
        self.invoice.action_post()

        # Step 1: Pre-check returns None (not found before POST)
        # Step 2: POST fails with timeout
        # Step 3: Ambiguous recovery query finds the newly created invoice in QBO
        recovered_inv = {
            'Id': '777333',
            'DocNumber': (self.invoice.name or '')[:21],
            'CustomerRef': {'value': '999'},
            'TotalAmt': 100.0,
        }
        mock_find.side_effect = [None, recovered_inv]
        mock_request.side_effect = Exception('HTTPSConnectionPool: Read timed out.')

        qb_id = self.invoice._quickbooks_export_invoice()

        self.assertEqual(qb_id, '777333')
        self.assertEqual(self.invoice.quickbooks_id, '777333')
        self.assertEqual(self.invoice.quickbooks_sync_status, 'synced')

