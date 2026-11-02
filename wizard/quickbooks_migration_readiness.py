# -*- coding: utf-8 -*-

import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class QuickBooksMigrationReadiness(models.TransientModel):
    _name = 'quickbooks.migration.readiness'
    _description = 'QuickBooks Historical Migration Readiness Assessment'

    name = fields.Char(default=lambda self: _('Migration Readiness Report %s') % fields.Datetime.now().strftime('%Y-%m-%d %H:%M'))
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, required=True)
    scope = fields.Selection([
        ('posted', 'Posted Customer Invoices Only (Recommended)'),
        ('all', 'All Customer Invoices (Draft, Posted, Cancelled)'),
    ], default='posted', string='Invoice Scope', required=True)

    # QuickBooks Target Company Metadata (Read-Only live detection)
    qbo_company_name = fields.Char(string='QBO Company Name', readonly=True)
    qbo_country = fields.Char(string='QBO Country', readonly=True)
    qbo_home_currency = fields.Char(string='QBO Home Currency', readonly=True)
    qbo_multicurrency = fields.Boolean(string='QBO Multi-Currency Enabled', readonly=True)

    # Summary Statistics
    total_invoices_count = fields.Integer(string='Total Invoices Evaluated', readonly=True)
    ready_count = fields.Integer(string='Ready Invoices', readonly=True)
    ready_amount = fields.Monetary(string='Ready Total Amount', currency_field='company_currency_id', readonly=True)
    review_count = fields.Integer(string='Review Required Invoices', readonly=True)
    review_amount = fields.Monetary(string='Review Total Amount', currency_field='company_currency_id', readonly=True)
    blocked_count = fields.Integer(string='Blocked Invoices', readonly=True)
    blocked_amount = fields.Monetary(string='Blocked Total Amount', currency_field='company_currency_id', readonly=True)

    # Detailed Blocker Counters
    currency_incompatible_count = fields.Integer(string='Currency Incompatible', readonly=True)
    customer_unmapped_count = fields.Integer(string='Missing Customer Mapping', readonly=True)
    product_unmapped_count = fields.Integer(string='Missing Product Mapping', readonly=True)
    tax_gap_count = fields.Integer(string='Tax Mapping Gap', readonly=True)
    duplicate_risk_count = fields.Integer(string='Existing QBO ID / Queued', readonly=True)
    paid_invoices_count = fields.Integer(string='Paid Invoices (Payment Review)', readonly=True)

    company_currency_id = fields.Many2one('res.currency', related='company_id.currency_id', readonly=True)
    line_ids = fields.One2many('quickbooks.migration.readiness.line', 'wizard_id', string='Invoice Readiness Lines')

    def action_evaluate(self):
        """
        Executes a strictly read-only evaluation of historical customer invoices.
        Does not mutate Odoo accounting entries and does not write to QuickBooks.
        """
        self.ensure_one()
        if not (self.env.user.has_group('account.group_account_manager') or self.env.user.has_group('base.group_system')):
            raise UserError(_('You must be an Accounting Manager to run migration readiness.'))

        client = self.env['quickbooks.client']

        # 1. Read QBO Target Company & Preferences
        qbo_company = ''
        qbo_country = ''
        qbo_home_curr = 'USD'
        qbo_multicurrency = False
        qbo_customers = {}  # {normalized_name: (id, DisplayName)}
        qbo_customer_ids = set()
        qbo_items = {}      # {normalized_name: (id, Name)}
        qbo_item_ids = set()

        try:
            c_info = client.query("select * from CompanyInfo")
            companies = c_info.get('QueryResponse', {}).get('CompanyInfo', [])
            if companies:
                qbo_company = companies[0].get('CompanyName', '')
                qbo_country = companies[0].get('Country', '')

            p_info = client.query("select * from Preferences")
            prefs = p_info.get('QueryResponse', {}).get('Preferences', [])
            if prefs:
                c_pref = prefs[0].get('CurrencyPrefs', {})
                qbo_home_curr = c_pref.get('HomeCurrency', {}).get('value', 'USD')
                qbo_multicurrency = bool(c_pref.get('MultiCurrencyEnabled', False))

            # Fetch active QBO customers for matching candidate detection
            cust_res = client.query("select Id, DisplayName, CompanyName from Customer maxresults 1000")
            for c in cust_res.get('QueryResponse', {}).get('Customer', []):
                cid = str(c.get('Id'))
                qbo_customer_ids.add(cid)
                for name_field in [c.get('DisplayName'), c.get('CompanyName')]:
                    if name_field:
                        qbo_customers[name_field.strip().lower()] = (cid, name_field.strip())

            # Fetch active QBO items
            item_res = client.query("select Id, Name, Type from Item maxresults 1000")
            for i in item_res.get('QueryResponse', {}).get('Item', []):
                iid = str(i.get('Id'))
                qbo_item_ids.add(iid)
                if i.get('Name'):
                    qbo_items[i.get('Name').strip().lower()] = (iid, i.get('Name').strip())

        except Exception as e:
            _logger.warning('Read-only QBO company info query failed: %s', e)
            qbo_company = 'Connection Error / Offline'

        # 2. Select target customer invoices
        domain = [('move_type', '=', 'out_invoice')]
        if self.scope == 'posted':
            domain.append(('state', '=', 'posted'))

        invoices = self.env['account.move'].search(domain, order='invoice_date asc, id asc')

        # Clear previous lines
        self.line_ids.unlink()

        lines_to_create = []
        r_cnt, r_amt = 0, 0.0
        rev_cnt, rev_amt = 0, 0.0
        b_cnt, b_amt = 0.0, 0.0

        curr_incomp = 0
        cust_unmapped = 0
        prod_unmapped = 0
        tax_gap = 0
        dup_risk = 0
        paid_cnt = 0

        # Query existing queue logs for account.move
        sync_logs = self.env['quickbooks.sync.log'].search([
            ('model_name', '=', 'account.move'),
            ('res_id', 'in', invoices.ids)
        ])
        logs_by_move = {log.res_id: log for log in sync_logs}

        for inv in invoices:
            blockers = []
            reviews = []

            # A. Currency Compatibility Check
            inv_curr = inv.currency_id.name or 'KES'
            if inv_curr != qbo_home_curr and not qbo_multicurrency:
                currency_status = 'incompatible'
                curr_incomp += 1
                blockers.append(_('Currency %s is incompatible with QBO home currency %s (multi-currency is disabled in QBO).') % (inv_curr, qbo_home_curr))
            else:
                currency_status = 'compatible'

            # B. Customer Mapping Check
            partner = inv.partner_id.commercial_partner_id
            cust_status = 'mapped'
            if partner.quickbooks_id:
                if qbo_customer_ids and partner.quickbooks_id not in qbo_customer_ids:
                    reviews.append(_('Customer mapped to QBO ID %s, but ID was not found in active QBO customers.') % partner.quickbooks_id)
            else:
                p_name_norm = (partner.name or '').strip().lower()
                candidate = qbo_customers.get(p_name_norm)
                if candidate:
                    cust_status = 'candidate_found'
                    reviews.append(_('Customer unmapped in Odoo, but exact name match found in QBO: "%s" (QBO ID: %s). Requires review/approval.') % (candidate[1], candidate[0]))
                else:
                    cust_status = 'unmapped'
                    cust_unmapped += 1
                    blockers.append(_('Customer "%s" (ID: %s) has no QuickBooks Customer ID and no candidate match in QBO.') % (partner.name, partner.id))

            # C. Product Mapping Check
            # In Odoo 17, non-display lines have display_type not in ('line_section', 'line_note')
            invoice_lines = inv.invoice_line_ids.filtered(lambda l: l.display_type not in ('line_section', 'line_note') and (l.quantity or l.price_subtotal))
            prod_status = 'all_mapped'
            unmapped_products = []

            for line in invoice_lines:
                template = line.product_id.product_tmpl_id
                if not template:
                    unmapped_products.append(_('Unknown line product: %s') % line.name)
                elif not template.quickbooks_id:
                    p_name = template.name or line.name
                    if p_name not in unmapped_products:
                        unmapped_products.append(p_name)

            if unmapped_products:
                prod_status = 'unmapped_lines'
                prod_unmapped += 1
                blockers.append(_('Unmapped products (%d): %s.') % (len(unmapped_products), ', '.join(unmapped_products[:5])))

            # D. Tax Mapping Check
            tax_status = 'mapped'
            unmapped_taxes = []
            taxes_present = set()
            for line in invoice_lines:
                for tax in line.tax_ids:
                    taxes_present.add(tax)

            for tax in taxes_present:
                mapping = self.env['quickbooks.tax.mapping'].search([
                    ('tax_id', '=', tax.id),
                    ('company_id', '=', inv.company_id.id),
                    ('active', '=', True),
                ], limit=1)
                if not mapping:
                    unmapped_taxes.append(f"{tax.name} ({tax.amount}%)")

            if unmapped_taxes:
                tax_status = 'unmapped_taxes'
                tax_gap += 1
                blockers.append(_('Unmapped taxes (%d): [%s]. Configure explicit mapping under Accounting > QuickBooks > Tax Mappings.') % (len(unmapped_taxes), ', '.join(unmapped_taxes)))

            # E. Account Mapping Check
            account_status = 'mapped'
            line_accounts = {l.account_id.code for l in invoice_lines if l.account_id}
            if '400100' in line_accounts or not line_accounts:
                account_status = 'needs_confirmation'
                reviews.append(_('Income account(s) [%s] require approved mapping to target QBO chart of accounts.') % (', '.join(sorted(line_accounts))))

            # F. Duplicate Risk & Existing QuickBooks ID
            duplicate_status = 'none'
            if inv.quickbooks_id:
                duplicate_status = 'has_qbo_id'
                dup_risk += 1
                reviews.append(_('Already has QuickBooks ID %s (Sync status: %s). Duplicate export prevented.') % (inv.quickbooks_id, inv.quickbooks_sync_status))
            elif inv.id in logs_by_move:
                log = logs_by_move[inv.id]
                duplicate_status = 'queued'
                dup_risk += 1
                reviews.append(_('Invoice already present in Sync Queue (Log ID %s, status: %s).') % (log.id, log.status))

            # G. Payment State Considerations
            payment_consideration = 'none'
            is_paid = inv.payment_state in ('paid', 'in_payment') or (inv.amount_residual == 0 and inv.amount_total > 0)
            if is_paid:
                payment_consideration = 'paid'
                paid_cnt += 1
                reviews.append(_('Invoice is PAID (residual = 0.00). Requires payment-migration strategy to avoid creating unpaid AR in QBO.'))

            # State check
            if inv.state == 'cancel':
                blockers.append(_('Invoice is Cancelled in Odoo.'))
            elif inv.state == 'draft':
                reviews.append(_('Invoice is in Draft state; only posted invoices are eligible for queueing.'))

            # Final classification
            if blockers:
                line_status = 'blocked'
                b_cnt += 1
                b_amt += inv.amount_total
            elif reviews:
                line_status = 'review'
                rev_cnt += 1
                rev_amt += inv.amount_total
            else:
                line_status = 'ready'
                r_cnt += 1
                r_amt += inv.amount_total

            all_reasons = []
            if blockers:
                all_reasons.append('BLOCKERS:\n' + '\n'.join(' • ' + b for b in blockers))
            if reviews:
                all_reasons.append('REVIEW ITEMS:\n' + '\n'.join(' • ' + r for r in reviews))

            lines_to_create.append((0, 0, {
                'move_id': inv.id,
                'invoice_name': inv.name or f'Draft #{inv.id}',
                'partner_id': inv.partner_id.id,
                'invoice_date': inv.invoice_date or inv.date,
                'currency_id': inv.currency_id.id,
                'amount_untaxed': inv.amount_untaxed,
                'amount_tax': inv.amount_tax,
                'amount_total': inv.amount_total,
                'amount_residual': inv.amount_residual,
                'payment_state': inv.payment_state or ('paid' if is_paid else 'not_paid'),
                'move_state': inv.state,
                'status': line_status,
                'currency_status': currency_status,
                'customer_status': cust_status,
                'product_status': prod_status,
                'tax_status': tax_status,
                'account_status': account_status,
                'duplicate_status': duplicate_status,
                'payment_consideration': payment_consideration,
                'issues_summary': '\n\n'.join(all_reasons) if all_reasons else _('All readiness criteria passed.'),
            }))

        self.write({
            'qbo_company_name': qbo_company,
            'qbo_country': qbo_country,
            'qbo_home_currency': qbo_home_curr,
            'qbo_multicurrency': qbo_multicurrency,
            'total_invoices_count': len(invoices),
            'ready_count': r_cnt,
            'ready_amount': r_amt,
            'review_count': rev_cnt,
            'review_amount': rev_amt,
            'blocked_count': b_cnt,
            'blocked_amount': b_amt,
            'currency_incompatible_count': curr_incomp,
            'customer_unmapped_count': cust_unmapped,
            'product_unmapped_count': prod_unmapped,
            'tax_gap_count': tax_gap,
            'duplicate_risk_count': dup_risk,
            'paid_invoices_count': paid_cnt,
            'line_ids': lines_to_create,
        })

        return {
            'type': 'ir.actions.act_window',
            'name': _('Migration Readiness Report'),
            'res_model': self._name,
            'view_mode': 'form',
            'views': [(False, 'form')],
            'res_id': self.id,
            'target': 'current',
        }


class QuickBooksMigrationReadinessLine(models.TransientModel):
    _name = 'quickbooks.migration.readiness.line'
    _description = 'QuickBooks Invoice Migration Readiness Line'
    _order = 'status desc, invoice_date asc, id asc'

    wizard_id = fields.Many2one('quickbooks.migration.readiness', ondelete='cascade', required=True)
    move_id = fields.Many2one('account.move', string='Invoice', required=True)
    invoice_name = fields.Char(string='Number')
    partner_id = fields.Many2one('res.partner', string='Customer')
    invoice_date = fields.Date(string='Date')
    currency_id = fields.Many2one('res.currency', string='Invoice Currency')
    amount_untaxed = fields.Monetary(string='Untaxed', currency_field='currency_id')
    amount_tax = fields.Monetary(string='Tax', currency_field='currency_id')
    amount_total = fields.Monetary(string='Total', currency_field='currency_id')
    amount_residual = fields.Monetary(string='Residual', currency_field='currency_id')
    payment_state = fields.Char(string='Payment State')
    move_state = fields.Selection([
        ('draft', 'Draft'),
        ('posted', 'Posted'),
        ('cancel', 'Cancelled'),
    ], string='Invoice State')

    status = fields.Selection([
        ('ready', 'READY'),
        ('review', 'REVIEW REQUIRED'),
        ('blocked', 'BLOCKED'),
    ], string='Readiness Status', required=True, index=True)

    currency_status = fields.Selection([
        ('compatible', 'Compatible'),
        ('incompatible', 'Incompatible Currency'),
    ], string='Currency Compatibility')

    customer_status = fields.Selection([
        ('mapped', 'Mapped'),
        ('candidate_found', 'Candidate in QBO'),
        ('unmapped', 'Unmapped'),
    ], string='Customer Mapping')

    product_status = fields.Selection([
        ('all_mapped', 'All Products Mapped'),
        ('unmapped_lines', 'Unmapped Products'),
    ], string='Product Mapping')

    tax_status = fields.Selection([
        ('mapped', 'Taxes Mapped'),
        ('unmapped_taxes', 'Tax Mapping Gap'),
    ], string='Tax Mapping')

    account_status = fields.Selection([
        ('mapped', 'Account Mapped'),
        ('needs_confirmation', 'Needs Confirmation'),
    ], string='Account Mapping')

    duplicate_status = fields.Selection([
        ('none', 'Clean'),
        ('has_qbo_id', 'Already Synced'),
        ('queued', 'In Sync Queue'),
    ], string='Duplicate Risk')

    payment_consideration = fields.Selection([
        ('none', 'Standard'),
        ('paid', 'Historical Paid Invoice'),
    ], string='Payment Consideration')

    issues_summary = fields.Text(string='Readiness Analysis & Blockers')
