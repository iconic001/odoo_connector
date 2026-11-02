# -*- coding: utf-8 -*-

from unittest.mock import patch
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import UserError


@tagged('post_install', '-at_install')
class TestQuickBooksMappingWizard(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.partner_match = self.env['res.partner'].create({
            'name': 'Alpha Supplies Kenya',
            'email': 'alpha@example.com',
            'customer_rank': 1,
        })
        self.partner_unmatched = self.env['res.partner'].create({
            'name': 'Omega Logistics Ltd',
            'email': 'omega@example.com',
            'customer_rank': 1,
        })

        self.product_match = self.env['product.template'].create({
            'name': 'Standard Maintenance Kit',
            'default_code': 'MNT-KIT-01',
            'sale_ok': True,
        })

        self.wizard = self.env['quickbooks.mapping.wizard'].create({
            'mapping_type': 'customer',
            'company_id': self.company.id,
        })

    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.query')
    def test_customer_matching_and_batch_apply(self, mock_query):
        # Simulate QBO response with Alpha Supplies Kenya
        mock_query.return_value = {
            'QueryResponse': {
                'Customer': [
                    {
                        'Id': 'QBO_CUST_ALPHA',
                        'DisplayName': 'Alpha Supplies Kenya',
                        'Active': True,
                    }
                ]
            }
        }

        self.wizard.action_load_and_match()

        alpha_line = self.wizard.line_ids.filtered(lambda l: l.partner_id == self.partner_match)
        self.assertTrue(alpha_line)
        self.assertEqual(alpha_line.match_type, 'exact_name')
        self.assertEqual(alpha_line.candidate_qb_id, 'QBO_CUST_ALPHA')
        self.assertTrue(alpha_line.selected)

        omega_line = self.wizard.line_ids.filtered(lambda l: l.partner_id == self.partner_unmatched)
        self.assertTrue(omega_line)
        self.assertEqual(omega_line.match_type, 'none')
        self.assertFalse(omega_line.selected)

        # Apply mapping
        self.wizard.action_apply_mappings()

        # Verify Odoo partner now has quickbooks_id set
        self.assertEqual(self.partner_match.quickbooks_id, 'QBO_CUST_ALPHA')
        self.assertFalse(self.partner_unmatched.quickbooks_id)

    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.query')
    def test_product_matching_by_sku(self, mock_query):
        self.wizard.mapping_type = 'product'
        self.wizard.product_scope = 'all'
        mock_query.return_value = {
            'QueryResponse': {
                'Item': [
                    {
                        'Id': 'QBO_ITEM_MNT',
                        'Name': 'Maintenance Kit (QBO Name)',
                        'Sku': 'MNT-KIT-01',
                        'Active': True,
                    }
                ]
            }
        }

        self.wizard.action_load_and_match()

        prod_line = self.wizard.line_ids.filtered(lambda l: l.product_tmpl_id == self.product_match)
        self.assertTrue(prod_line)
        self.assertEqual(prod_line.match_type, 'sku')
        self.assertEqual(prod_line.candidate_qb_id, 'QBO_ITEM_MNT')
        self.assertTrue(prod_line.selected)

        self.wizard.action_apply_mappings()
        self.assertEqual(self.product_match.quickbooks_id, 'QBO_ITEM_MNT')

