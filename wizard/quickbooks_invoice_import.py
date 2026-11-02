from odoo import models, _
from odoo.exceptions import UserError


class QuickBooksInvoicePreviewImport(models.TransientModel):
    _inherit = 'quickbooks.invoice.preview'

    def _require_import_access(self):
        if not (
            self.env.user.has_group('account.group_account_manager')
            or self.env.user.has_group('base.group_system')
        ):
            raise UserError(_('You are not allowed to import invoices.'))

        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('quickbooks_connector.environment', 'sandbox') != 'sandbox':
            raise UserError(_(
                'This importer is restricted to QuickBooks Sandbox.'
            ))

    def _unique_record(self, model, domain, label):
        records = model.search(domain, limit=2)
        if len(records) > 1:
            raise UserError(_(
                'More than one Odoo record matches %s. Resolve the mapping '
                'manually before importing.'
            ) % label)
        return records

    def _get_or_create_customer(self, qb_id, company):
        Partner = self.env['res.partner'].with_company(company)
        partner = self._unique_record(
            Partner, [('quickbooks_id', '=', str(qb_id))],
            'QuickBooks customer ID %s' % qb_id,
        )
        if partner:
            return partner

        data = self.env['quickbooks.client'].request(
            'GET', 'customer/' + str(qb_id)
        ).get('Customer')
        if not data or not data.get('Active', True):
            raise UserError(_('The QuickBooks customer is missing or inactive.'))

        name = data.get('DisplayName') or data.get('CompanyName')
        if not name:
            raise UserError(_('The QuickBooks customer has no name.'))

        # Link only a unique exact-name match; never fuzzy-match customers.
        matches = self._unique_record(
            Partner, [('name', '=', name)],
            'customer named %s' % name,
        )
        if matches:
            if matches.quickbooks_id and matches.quickbooks_id != str(qb_id):
                raise UserError(_('The matching customer has a different QuickBooks ID.'))
            matches.write({'quickbooks_id': str(qb_id)})
            return matches

        addr = data.get('BillAddr') or {}
        country = self.env['res.country'].search([('code', '=', 'US')], limit=1)
        state = self.env['res.country.state']
        if country and addr.get('CountrySubDivisionCode'):
            state = state.search([
                ('country_id', '=', country.id),
                ('code', '=', addr['CountrySubDivisionCode']),
            ], limit=1)

        receivable = company.partner_id.with_company(
            company
        ).property_account_receivable_id

        values = {
            'name': name,
            'company_type': 'company' if data.get('CompanyName') else 'person',
            'customer_rank': 1,
            'quickbooks_id': str(qb_id),
            'email': (data.get('PrimaryEmailAddr') or {}).get('Address'),
            'phone': (data.get('PrimaryPhone') or {}).get('FreeFormNumber'),
            'street': addr.get('Line1'),
            'city': addr.get('City'),
            'zip': addr.get('PostalCode'),
            'country_id': country.id if country else False,
            'state_id': state.id if state else False,
            'property_account_receivable_id': receivable.id if receivable else False,
        }
        return Partner.create({
            key: value for key, value in values.items()
            if value is not None
        })

    def _get_or_create_product(self, qb_id, company):
        Product = self.env['product.template'].with_company(company)
        product = self._unique_record(
            Product, [('quickbooks_id', '=', str(qb_id))],
            'QuickBooks item ID %s' % qb_id,
        )
        if product:
            return product.product_variant_id

        data = self.env['quickbooks.client'].request(
            'GET', 'item/' + str(qb_id)
        ).get('Item')
        if not data or not data.get('Active', True):
            raise UserError(_('A QuickBooks item is missing or inactive.'))

        name = data.get('Name')
        if not name:
            raise UserError(_('A QuickBooks item has no name.'))

        matches = self._unique_record(
            Product, [('name', '=', name)],
            'product named %s' % name,
        )
        if matches:
            if matches.quickbooks_id and matches.quickbooks_id != str(qb_id):
                raise UserError(_('The matching product has a different QuickBooks ID.'))
            matches.write({'quickbooks_id': str(qb_id)})
            return matches.product_variant_id

        item_type = data.get('Type')
        if item_type == 'Inventory':
            odoo_type = 'product'
            category_name = 'Saleable'
        elif item_type == 'Service':
            odoo_type = 'service'
            category_name = 'All'
        elif item_type == 'NonInventory':
            odoo_type = 'consu'
            category_name = 'Saleable'
        else:
            raise UserError(_(
                'Unsupported QuickBooks item type for %s: %s'
            ) % (name, item_type))

        category = self.env['product.category'].search(
            [('name', '=', category_name)], limit=1
        )
        if not category:
            raise UserError(_(
                'Create or select an appropriate product category for %s first.'
            ) % name)

        template = Product.create({
            'name': name,
            'description_sale': data.get('Description') or name,
            'type': odoo_type,
            'categ_id': category.id,
            'sale_ok': True,
            'purchase_ok': item_type != 'Service',
            'quickbooks_id': str(qb_id),
        })
        return template.product_variant_id

    def action_import_as_draft(self):
        self.ensure_one()
        self._require_import_access()

        qb_id = (self.invoice_id or '').strip()
        if not qb_id.isdigit():
            raise UserError(_('Enter a valid QuickBooks invoice ID.'))

        Move = self.env['account.move'].sudo()
        existing = Move.search([('quickbooks_id', '=', qb_id)], limit=1)
        if existing:
            raise UserError(_(
                'This QuickBooks invoice has already been imported as %s.'
            ) % existing.display_name)

        company = self.env.company
        client = self.env['quickbooks.client']
        source = client.request('GET', 'invoice/' + qb_id).get('Invoice')
        if not source:
            raise UserError(_('QuickBooks returned no invoice.'))

        if source.get('CurrencyRef', {}).get('value') != 'USD':
            raise UserError(_(
                'This initial Sandbox importer supports USD invoices only.'
            ))

        currency = self.env['res.currency'].search([
            ('name', '=', 'USD'), ('active', '=', True)
        ], limit=1)
        if not currency:
            raise UserError(_('Activate USD before importing this invoice.'))

        customer_ref = source.get('CustomerRef') or {}
        if not customer_ref.get('value'):
            raise UserError(_('The invoice has no customer reference.'))

        source_lines = [
            line for line in source.get('Line', [])
            if line.get('DetailType') == 'SalesItemLineDetail'
        ]
        if not source_lines:
            raise UserError(_('The QuickBooks invoice has no product/service lines.'))

        subtotal = sum(float(line.get('Amount') or 0) for line in source_lines)
        source_subtotal = float(source.get('SubTotal') if source.get('SubTotal') is not None else subtotal)
        source_tax = float((source.get('TxnTaxDetail') or {}).get('TotalTax') or 0)
        source_total = float(source.get('TotalAmt') or 0)

        if currency.compare_amounts(subtotal, source_subtotal) != 0:
            raise UserError(_('Invoice line amounts do not match the QuickBooks subtotal.'))
        if currency.compare_amounts(subtotal + source_tax, source_total) != 0:
            raise UserError(_('QuickBooks subtotal plus tax does not match its total.'))

        tax_detail = source.get('TxnTaxDetail') or {}
        tax_lines = tax_detail.get('TaxLine') or []
        if isinstance(tax_lines, dict):
            tax_lines = [tax_lines]

        tax_rates = {
            float((line.get('TaxLineDetail') or {}).get('TaxPercent'))
            for line in tax_lines
            if (line.get('TaxLineDetail') or {}).get('TaxPercent') is not None
        }

        tax = self.env['account.tax']
        if source_tax:
            if len(tax_rates) != 1:
                raise UserError(_(
                    'The invoice has missing or mixed tax rates. Configure '
                    'an explicit tax mapping before importing it.'
                ))
            rate = next(iter(tax_rates))
            tax = self.env['account.tax'].search([
                ('type_tax_use', '=', 'sale'),
                ('amount_type', '=', 'percent'),
                ('amount', '=', rate),
                ('company_id', '=', company.id),
                ('active', '=', True),
            ], limit=2)
            if len(tax) != 1:
                raise UserError(_(
                    'Expected exactly one active company sales tax at %s%%; '
                    'found %s. Resolve tax mapping before importing.'
                ) % (rate, len(tax)))

        # Validate line-level tax treatment before creating or linking records.
        expected_tax_base = 0.0
        for line in source_lines:
            detail = line.get('SalesItemLineDetail') or {}
            code = (detail.get('TaxCodeRef') or {}).get('value')
            amount = float(line.get('Amount') or 0)

            if code == 'TAX':
                expected_tax_base += amount
            elif code in ('NON', 'NON_TAXABLE'):
                continue
            else:
                raise UserError(_(
                    'Unsupported or missing QuickBooks tax code %s. '
                    'Configure its mapping before importing.'
                ) % (code or '(missing)'))

        if not source_tax and expected_tax_base:
            raise UserError(_(
                'Import blocked before creating or mapping records: '
                'the invoice contains TAX lines but QuickBooks reports '
                'zero invoice tax. Review the source tax details.'
            ))
        expected_line_tax = currency.round(
            expected_tax_base * (next(iter(tax_rates)) / 100.0)
        ) if source_tax and len(tax_rates) == 1 else 0.0
        if currency.compare_amounts(expected_line_tax, source_tax) != 0:
            raise UserError(_(
                'Import blocked before creating or mapping records: '
                'QuickBooks reports tax of %.2f USD, but the supported '
                'line-level tax rules calculate %.2f USD. Review the '
                'QuickBooks tax setup or configure an explicit mapping.'
            ) % (source_tax, expected_line_tax))

        partner = self._get_or_create_customer(customer_ref['value'], company)
        journal = self.env['account.journal'].search([
            ('type', '=', 'sale'),
            ('company_id', '=', company.id),
        ], limit=1)
        if not journal:
            raise UserError(_('No sales journal exists for the active company.'))

        invoice_lines = []
        for line in source_lines:
            detail = line.get('SalesItemLineDetail') or {}
            item_ref = detail.get('ItemRef') or {}
            item_id = item_ref.get('value')
            if not item_id:
                raise UserError(_('An invoice line has no QuickBooks item ID.'))

            quantity = float(detail.get('Qty') or 0)
            unit_price = float(detail.get('UnitPrice') or 0)
            amount = float(line.get('Amount') or 0)
            if quantity <= 0:
                raise UserError(_('Invoice line quantity must be positive.'))
            if currency.compare_amounts(quantity * unit_price, amount) != 0:
                raise UserError(_(
                    'Quantity x unit price does not match the source amount '
                    'for item %s. Discounts or special line calculations '
                    'must be mapped explicitly.'
                ) % item_id)

            code = (detail.get('TaxCodeRef') or {}).get('value')
            if code == 'TAX':
                if not source_tax:
                    raise UserError(_(
                        'A taxable line has a zero invoice tax total. '
                        'Check the source tax details before importing.'
                    ))
                line_taxes = [(6, 0, tax.ids)]
            elif code in ('NON', 'NON_TAXABLE'):
                line_taxes = [(6, 0, [])]
            else:
                raise UserError(_(
                    'Unsupported or missing QuickBooks tax code %s. '
                    'Configure its mapping before importing.'
                ) % (code or '(missing)'))

            product = self._get_or_create_product(item_id, company)
            invoice_lines.append((0, 0, {
                'product_id': product.id,
                'name': line.get('Description') or product.display_name,
                'quantity': quantity,
                'price_unit': unit_price,
                'tax_ids': line_taxes,
            }))

        vals = {
            'move_type': 'out_invoice',
            'company_id': company.id,
            'journal_id': journal.id,
            'partner_id': partner.id,
            'currency_id': currency.id,
            'invoice_date': source.get('TxnDate'),
            'invoice_date_due': source.get('DueDate') or source.get('TxnDate'),
            'ref': 'QuickBooks Sandbox %s' % (source.get('DocNumber') or qb_id),
            'invoice_origin': 'QuickBooks Sandbox invoice %s' % qb_id,
            'invoice_line_ids': invoice_lines,
            'quickbooks_id': qb_id,
            'quickbooks_sync_status': 'synced',
        }

        move = self.env['account.move'].with_company(company).create(vals)

        # Compare in USD (invoice currency), not company currency (KES).
        checks = [
            ('subtotal', move.amount_untaxed, source_subtotal),
            ('tax', move.amount_tax, source_tax),
            ('total', move.amount_total, source_total),
        ]
        for label, actual, expected in checks:
            if not currency.is_zero(actual - expected):
                raise UserError(_(
                    'Import rolled back: Odoo %s %.2f USD differs from '
                    'QuickBooks %.2f USD.'
                ) % (label, actual, expected))

        if move.state != 'draft':
            raise UserError(_('Import aborted because the invoice is not in Draft state.'))

        return {
            'type': 'ir.actions.act_window',
            'name': _('Imported Draft Invoice'),
            'res_model': 'account.move',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'res_id': move.id,
            'target': 'current',
        }



