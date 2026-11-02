# -*- coding: utf-8 -*-

from unittest.mock import patch
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import UserError


@tagged('post_install', '-at_install')
class TestQuickBooksCurrencyGuard(TransactionCase):

    def setUp(self):
        super().setUp()
        self.client = self.env['quickbooks.client']

    def test_missing_currency_raises(self):
        with self.assertRaises(UserError) as cm:
            self.client.check_currency_support(False)
        self.assertIn('Invoice has no currency defined', str(cm.exception))

    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.get_company_preferences')
    def test_kes_rejected_on_usd_single_currency(self, mock_prefs):
        mock_prefs.return_value = {
            'company_name': 'Sandbox Company US 2bf8',
            'country': 'US',
            'home_currency': 'USD',
            'multicurrency_enabled': False,
        }
        with self.assertRaises(UserError) as cm:
            self.client.check_currency_support('KES')
        self.assertIn('Safety Guard: Cannot process KES invoice', str(cm.exception))
        self.assertIn('Target home currency is USD and multi-currency is disabled', str(cm.exception))

    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.get_company_preferences')
    def test_usd_allowed_on_usd_single_currency(self, mock_prefs):
        mock_prefs.return_value = {
            'company_name': 'Sandbox Company US 2bf8',
            'country': 'US',
            'home_currency': 'USD',
            'multicurrency_enabled': False,
        }
        result = self.client.check_currency_support('USD')
        self.assertTrue(result)

    @patch('odoo.addons.quickbooks_connector.models.quickbooks_sync.QuickBooksClient.get_company_preferences')
    def test_kes_allowed_when_multicurrency_enabled(self, mock_prefs):
        mock_prefs.return_value = {
            'company_name': 'Sandbox Company US 2bf8',
            'country': 'US',
            'home_currency': 'USD',
            'multicurrency_enabled': True,
        }
        result = self.client.check_currency_support('KES')
        self.assertTrue(result)

