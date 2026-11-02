# -*- coding: utf-8 -*-

from unittest.mock import patch
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import UserError


@tagged('post_install', '-at_install')
class TestQuickBooksCreditMemo(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.partner = self.env['res.partner'].create({
            'name': 'Test Credit Memo Partner',
            'quickbooks_id': 'CUST_CM_1',
        })
        self.product_tmpl = self.env['product.template'].create({
            'name': 'Test Refund Item',
            'type': 'service',
            'quickbooks_id': 'ITEM_CM_1',
        })
        self.product = self.product_tmpl.product_variant_id

        self.credit_note = self.env['account.move'].create({
            'move_type': 'out_refund',
            'partner_id': self.partner.id,
            'currency_id': self.env.ref('base.USD').id,
            'invoice_date': '2026-10-31',
            'invoice_line_ids': [
                (0, 0, {
                    'product_id': self.product.id,
                    'quantity': 1.0,
                    'price_unit': 50.0,
                    'tax_ids': [(6, 0, [])],
                })
            ]
        })

    def test_credit_note_draft_queue_rejected(self):
        self.assertEqual(self.credit_note.state, 'draft')
        with self.assertRaises(UserError) as cm:
            self.credit_note.action_quickbooks_queue_export()
        self.assertIn('Only posted customer invoices and credit notes can be exported', str(cm.exception))

    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.check_currency_support')
    def test_credit_note_posted_queue_success(self, mock_guard):
        mock_guard.return_value = True
        self.credit_note.action_post()
        self.credit_note.action_quickbooks_queue_export()

        self.assertEqual(self.credit_note.quickbooks_sync_status, 'queued')
        log = self.env['quickbooks.sync.log'].search([
            ('model_name', '=', 'account.move'),
            ('res_id', '=', self.credit_note.id),
        ])
        self.assertTrue(log)
        self.assertIn('Credit Note', log.name)

    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.check_currency_support')
    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.find_credit_memo_by_doc_number')
    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.request')
    def test_credit_note_export_post_endpoint(self, mock_request, mock_find, mock_guard):
        mock_guard.return_value = True
        self.credit_note.action_post()

        mock_find.return_value = None
        mock_request.return_value = {
            'CreditMemo': {
                'Id': 'CM_QBO_555',
                'DocNumber': (self.credit_note.name or '')[:21],
            }
        }

        qb_id = self.credit_note._quickbooks_export_invoice()

        self.assertEqual(qb_id, 'CM_QBO_555')
        self.assertEqual(self.credit_note.quickbooks_id, 'CM_QBO_555')
        self.assertEqual(self.credit_note.quickbooks_sync_status, 'synced')

        # Verify endpoint called was creditmemo
        post_calls = [c for c in mock_request.call_args_list if c.args and c.args[0] == 'POST']
        self.assertEqual(len(post_calls), 1)
        args, kwargs = post_calls[0]
        self.assertEqual(args[0], 'POST')
        self.assertEqual(args[1], 'creditmemo')

