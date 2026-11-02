# -*- coding: utf-8 -*-

from unittest.mock import MagicMock
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import UserError, ValidationError


@tagged('post_install', '-at_install')
class TestQuickBooksTaxMapping(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.TaxMapping = self.env['quickbooks.tax.mapping']
        self.AccountTax = self.env['account.tax']

        self.tax_16 = self.AccountTax.create({
            'name': 'VAT 16% Test',
            'amount_type': 'percent',
            'amount': 16.0,
            'type_tax_use': 'sale',
            'company_id': self.company.id,
        })
        self.tax_0 = self.AccountTax.create({
            'name': 'Zero Rated 0% Test',
            'amount_type': 'percent',
            'amount': 0.0,
            'type_tax_use': 'sale',
            'company_id': self.company.id,
        })

    def test_zero_rate_constraint_fails_on_nonzero_tax(self):
        with self.assertRaises(ValidationError):
            self.TaxMapping.create({
                'tax_id': self.tax_16.id,
                'qbo_tax_code_id': 'NON',
                'tax_type': 'zero_rate',
                'company_id': self.company.id,
            })

    def test_explicit_tax_mapping_resolved(self):
        mapping = self.TaxMapping.create({
            'tax_id': self.tax_16.id,
            'qbo_tax_code_id': 'TAX_16_KE',
            'qbo_tax_code_name': 'Kenyan VAT 16%',
            'tax_type': 'taxable',
            'company_id': self.company.id,
        })
        mock_line = MagicMock()
        mock_line.tax_ids = self.tax_16
        mock_line.company_id = self.company
        mock_line.name = 'Test Line'

        ref = self.TaxMapping.get_line_tax_code_ref(mock_line)
        self.assertEqual(ref, {'value': 'TAX_16_KE'})

    def test_unmapped_tax_raises_user_error(self):
        mock_line = MagicMock()
        mock_line.tax_ids = self.tax_16
        mock_line.company_id = self.company
        mock_line.name = 'Unmapped Line'
        mock_line.product_id.display_name = 'Unmapped Product'

        with self.assertRaises(UserError) as cm:
            self.TaxMapping.get_line_tax_code_ref(mock_line)
        self.assertIn('has no active QuickBooks tax mapping', str(cm.exception))

    def test_multiple_compound_taxes_raises_user_error(self):
        mock_line = MagicMock()
        mock_line.tax_ids = self.tax_16 | self.tax_0
        mock_line.company_id = self.company
        mock_line.name = 'Compound Line'
        mock_line.product_id.display_name = 'Compound Product'

        with self.assertRaises(UserError) as cm:
            self.TaxMapping.get_line_tax_code_ref(mock_line)
        self.assertIn('Compound tax mapping is not configured', str(cm.exception))

    def test_nontaxable_line_fallback_to_non(self):
        mock_line = MagicMock()
        mock_line.tax_ids = self.AccountTax.browse([])
        mock_line.company_id = self.company
        mock_line.name = 'No Tax Line'

        ref = self.TaxMapping.get_line_tax_code_ref(mock_line)
        self.assertEqual(ref, {'value': 'NON'})

