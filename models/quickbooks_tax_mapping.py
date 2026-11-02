# -*- coding: utf-8 -*-

import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class QuickBooksTaxMapping(models.Model):
    _name = 'quickbooks.tax.mapping'
    _description = 'QuickBooks Tax Code Mapping'
    _order = 'company_id, tax_amount desc, id'

    name = fields.Char(compute='_compute_name', store=True, string='Mapping Name')
    company_id = fields.Many2one(
        'res.company',
        default=lambda self: self.env.company,
        required=True,
        string='Company',
    )
    tax_id = fields.Many2one(
        'account.tax',
        string='Odoo Sales Tax',
        required=True,
        domain="[('type_tax_use', '=', 'sale'), ('company_id', '=', company_id)]",
    )
    tax_amount = fields.Float(
        string='Tax Rate (%)',
        related='tax_id.amount',
        store=True,
        readonly=True,
    )
    qbo_tax_code_id = fields.Char(
        string='QuickBooks Tax Code ID',
        required=True,
        help="QuickBooks TaxCode ID or reference (e.g., 'TAX', 'NON', '2', or specific Global/Kenyan VAT TaxCode ID).",
    )
    qbo_tax_code_name = fields.Char(
        string='QuickBooks Tax Code Name / Description',
        help="Display name in QuickBooks Online (e.g., 'California', 'VAT 16%', 'Zero Rated').",
    )
    tax_type = fields.Selection([
        ('taxable', 'Taxable Standard / Reduced Rate'),
        ('zero_rate', 'Zero-Rated (0%)'),
        ('exempt', 'Exempt (0%)'),
        ('out_of_scope', 'Out of Scope / Non-Taxable'),
    ], default='taxable', required=True, string='Tax Type')

    active = fields.Boolean(default=True)
    notes = fields.Text(string='Accounting Notes')

    _sql_constraints = [
        ('tax_company_uniq', 'unique(tax_id, company_id)',
         'A QuickBooks tax mapping already exists for this Odoo tax in the specified company!'),
    ]

    @api.depends('tax_id.name', 'tax_id.amount', 'qbo_tax_code_name', 'qbo_tax_code_id')
    def _compute_name(self):
        for rec in self:
            tax_name = rec.tax_id.name or 'Unknown Tax'
            rate_str = f"{rec.tax_id.amount}%" if rec.tax_id else ""
            qbo_ref = rec.qbo_tax_code_name or rec.qbo_tax_code_id or "Unset"
            rec.name = f"{tax_name} ({rate_str}) -> QBO: {qbo_ref}"

    @api.constrains('tax_id', 'tax_type')
    def _check_tax_type_consistency(self):
        for rec in self:
            if rec.tax_type in ('zero_rate', 'exempt') and rec.tax_amount != 0.0:
                raise ValidationError(_(
                    'Tax mapping "%s" is classified as %s, but has a non-zero rate (%.2f%%). '
                    'Zero-rated or Exempt mappings must apply to 0%% taxes only.'
                ) % (rec.tax_id.name, rec.tax_type, rec.tax_amount))

    @api.model
    def get_line_tax_code_ref(self, line):
        """
        Resolves the QuickBooks TaxCodeRef for an invoice line.
        Fails closed with a clear UserError if an unmapped tax is encountered.
        """
        taxes = line.tax_ids
        if not taxes:
            # Check if there is an out_of_scope or non-taxable default mapping for this company
            non_tax = self.search([
                ('company_id', '=', line.company_id.id),
                ('tax_type', 'in', ('out_of_scope', 'exempt')),
                ('active', '=', True)
            ], limit=1)
            if non_tax:
                return {'value': str(non_tax.qbo_tax_code_id)}
            # Standard US Sandbox fallback for non-taxable lines
            return {'value': 'NON'}

        if len(taxes) > 1:
            raise UserError(_(
                'Line "%s" has multiple taxes (%s). Compound tax mapping is not configured. '
                'Apply a single mapped tax or map a group tax.'
            ) % (line.name or line.product_id.display_name, ', '.join(taxes.mapped('name'))))

        tax = taxes[0]
        mapping = self.search([
            ('tax_id', '=', tax.id),
            ('company_id', '=', line.company_id.id),
            ('active', '=', True),
        ], limit=1)

        if not mapping:
            raise UserError(_(
                'Tax "%s" (%.2f%%) on line "%s" has no active QuickBooks tax mapping.\n'
                'Configure the tax mapping under Accounting > QuickBooks > Tax Mappings before exporting.'
            ) % (tax.name, tax.amount, line.name or line.product_id.display_name))

        return {'value': str(mapping.qbo_tax_code_id)}
