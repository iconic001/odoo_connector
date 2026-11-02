# -*- coding: utf-8 -*-

import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class QuickBooksMappingWizard(models.TransientModel):
    _name = 'quickbooks.mapping.wizard'
    _description = 'QuickBooks Master Data Mapping & Onboarding Wizard'

    mapping_type = fields.Selection([
        ('customer', 'Customers & Contacts (res.partner)'),
        ('product', 'Products & Services (product.template)'),
    ], default='customer', required=True, string='Master Data Type')

    product_scope = fields.Selection([
        ('invoiced', 'Products on Invoices Only (Recommended for Migration)'),
        ('all', 'All Saleable Products (Full Catalog)'),
    ], default='invoiced', string='Product Scope')

    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, required=True)

    total_odoo_records = fields.Integer(string='Total Records Scanned', readonly=True)
    mapped_count = fields.Integer(string='Already Mapped', readonly=True)
    matched_count = fields.Integer(string='Matches Found in QBO', readonly=True)
    unmatched_count = fields.Integer(string='Unmatched (Require Creation or Manual Match)', readonly=True)

    line_ids = fields.One2many('quickbooks.mapping.wizard.line', 'wizard_id', string='Mapping Candidates')
    result_message = fields.Char(string='Last Operation Result', readonly=True)

    def action_load_and_match(self):
        self.ensure_one()
        client = self.env['quickbooks.client']
        self.line_ids.unlink()

        lines_to_create = []
        total = 0
        mapped = 0
        matched = 0
        unmatched = 0

        if self.mapping_type == 'customer':
            # 1. Fetch active QBO Customers
            qbo_customers = {}        # normalized_name -> (id, DisplayName)
            qbo_cust_by_email = {}    # normalized_email -> (id, DisplayName)
            qbo_all_ids = set()

            try:
                res = client.query("select Id, DisplayName, CompanyName, PrimaryEmailAddr, Active from Customer maxresults 1000")
                for c in res.get('QueryResponse', {}).get('Customer', []):
                    cid = str(c.get('Id'))
                    dname = c.get('DisplayName', '').strip()
                    cname = c.get('CompanyName', '').strip()
                    qbo_all_ids.add(cid)

                    for name_val in [dname, cname]:
                        if name_val:
                            qbo_customers[name_val.lower()] = (cid, name_val)

                    email_addr = (c.get('PrimaryEmailAddr') or {}).get('Address', '').strip().lower()
                    if email_addr and '@' in email_addr:
                        qbo_cust_by_email[email_addr] = (cid, dname or cname)
            except Exception as e:
                raise UserError(_('Failed to fetch customers from QuickBooks: %s') % e)

            # 2. Select relevant Odoo Partners (customers with invoices or customer rank)
            invoiced_partners = self.env['account.move'].search([
                ('move_type', '=', 'out_invoice'),
                ('company_id', '=', self.company_id.id),
            ]).mapped('partner_id.commercial_partner_id')

            ranked_customers = self.env['res.partner'].search([
                ('customer_rank', '>', 0),
                '|', ('company_id', '=', False), ('company_id', '=', self.company_id.id),
            ])
            target_partners = (invoiced_partners | ranked_customers).sorted(lambda p: p.name or '')

            total = len(target_partners)

            for partner in target_partners:
                p_name = partner.name or 'Unnamed Partner #%s' % partner.id
                p_norm = p_name.strip().lower()
                p_email = (partner.email or '').strip().lower()

                match_type = 'none'
                cand_id = False
                cand_name = False
                action = 'skip'
                selected = False
                notes = ''

                if partner.quickbooks_id:
                    if partner.quickbooks_id in qbo_all_ids:
                        match_type = 'already_mapped'
                        cand_id = partner.quickbooks_id
                        cand_name = p_name
                        action = 'skip'
                        mapped += 1
                    else:
                        match_type = 'invalid_id'
                        cand_id = partner.quickbooks_id
                        action = 'map'
                        selected = False
                        unmatched += 1
                        notes = _('Current QBO ID %s not found in active QBO customers.') % partner.quickbooks_id
                else:
                    # Try exact name match
                    if p_norm in qbo_customers:
                        cid, q_name = qbo_customers[p_norm]
                        match_type = 'exact_name'
                        cand_id = cid
                        cand_name = q_name
                        action = 'map'
                        selected = True
                        matched += 1
                        notes = _('Exact name match found in QuickBooks: "%s"') % q_name
                    # Try email match
                    elif p_email and '@' in p_email and p_email in qbo_cust_by_email:
                        cid, q_name = qbo_cust_by_email[p_email]
                        match_type = 'email'
                        cand_id = cid
                        cand_name = q_name
                        action = 'map'
                        selected = True
                        matched += 1
                        notes = _('Email match found in QuickBooks for "%s"') % q_name
                    else:
                        match_type = 'none'
                        action = 'create_in_qbo'
                        selected = False
                        unmatched += 1
                        notes = _('No match found in QBO. Check box to create customer in QBO.')

                lines_to_create.append((0, 0, {
                    'partner_id': partner.id,
                    'name': p_name,
                    'email': partner.email,
                    'current_qb_id': partner.quickbooks_id,
                    'candidate_qb_id': cand_id,
                    'candidate_qb_name': cand_name,
                    'match_type': match_type,
                    'action_to_take': action,
                    'selected': selected,
                    'message': notes,
                }))

        elif self.mapping_type == 'product':
            # 1. Fetch active QBO Items
            qbo_items = {}        # normalized_name -> (id, Name)
            qbo_by_sku = {}       # normalized_sku -> (id, Name)
            qbo_all_ids = set()

            try:
                res = client.query("select Id, Name, Type, Sku, Active from Item maxresults 1000")
                for i in res.get('QueryResponse', {}).get('Item', []):
                    iid = str(i.get('Id'))
                    iname = i.get('Name', '').strip()
                    sku = (i.get('Sku') or '').strip().lower()
                    qbo_all_ids.add(iid)

                    if iname:
                        qbo_items[iname.lower()] = (iid, iname)
                    if sku:
                        qbo_by_sku[sku] = (iid, iname)
            except Exception as e:
                raise UserError(_('Failed to fetch items from QuickBooks: %s') % e)

            # 2. Select relevant Odoo products
            invoice_lines = self.env['account.move.line'].search([
                ('move_id.move_type', '=', 'out_invoice'),
                ('company_id', '=', self.company_id.id),
                ('product_id', '!=', False),
            ])
            invoiced_templates = invoice_lines.mapped('product_id.product_tmpl_id')

            if self.product_scope == 'invoiced':
                target_templates = invoiced_templates.sorted(lambda t: t.name or '')
            else:
                saleable_templates = self.env['product.template'].search([
                    ('sale_ok', '=', True),
                    '|', ('company_id', '=', False), ('company_id', '=', self.company_id.id),
                ])
                target_templates = (invoiced_templates | saleable_templates).sorted(lambda t: t.name or '')

            total = len(target_templates)

            for tmpl in target_templates:
                t_name = tmpl.name or 'Unnamed Product #%s' % tmpl.id
                t_norm = t_name.strip().lower()
                t_code = (tmpl.default_code or '').strip().lower()

                match_type = 'none'
                cand_id = False
                cand_name = False
                action = 'skip'
                selected = False
                notes = ''

                if tmpl.quickbooks_id:
                    if tmpl.quickbooks_id in qbo_all_ids:
                        match_type = 'already_mapped'
                        cand_id = tmpl.quickbooks_id
                        cand_name = t_name
                        action = 'skip'
                        mapped += 1
                    else:
                        match_type = 'invalid_id'
                        cand_id = tmpl.quickbooks_id
                        action = 'map'
                        selected = False
                        unmatched += 1
                        notes = _('Current QBO Item ID %s not found in active QBO items.') % tmpl.quickbooks_id
                else:
                    # Try exact name match
                    if t_norm in qbo_items:
                        iid, q_name = qbo_items[t_norm]
                        match_type = 'exact_name'
                        cand_id = iid
                        cand_name = q_name
                        action = 'map'
                        selected = True
                        matched += 1
                        notes = _('Exact item name match in QuickBooks: "%s"') % q_name
                    # Try SKU / Reference match
                    elif t_code and t_code in qbo_by_sku:
                        iid, q_name = qbo_by_sku[t_code]
                        match_type = 'sku'
                        cand_id = iid
                        cand_name = q_name
                        action = 'map'
                        selected = True
                        matched += 1
                        notes = _('Internal reference/SKU match in QuickBooks: "%s"') % q_name
                    else:
                        match_type = 'none'
                        action = 'create_in_qbo'
                        selected = False
                        unmatched += 1
                        notes = _('No match found in QBO. Check box to create item in QBO.')

                lines_to_create.append((0, 0, {
                    'product_tmpl_id': tmpl.id,
                    'name': t_name,
                    'ref': tmpl.default_code,
                    'current_qb_id': tmpl.quickbooks_id,
                    'candidate_qb_id': cand_id,
                    'candidate_qb_name': cand_name,
                    'match_type': match_type,
                    'action_to_take': action,
                    'selected': selected,
                    'message': notes,
                }))

        self.write({
            'total_odoo_records': total,
            'mapped_count': mapped,
            'matched_count': matched,
            'unmatched_count': unmatched,
            'line_ids': lines_to_create,
        })

        return {
            'type': 'ir.actions.act_window',
            'name': _('Master Data Mapping Wizard'),
            'res_model': self._name,
            'view_mode': 'form',
            'views': [(False, 'form')],
            'res_id': self.id,
            'target': 'current',
        }

    def action_apply_mappings(self):
        self.ensure_one()
        if not (self.env.user.has_group('account.group_account_manager') or self.env.user.has_group('base.group_system')):
            raise UserError(_('You must be an Accounting Manager to apply QuickBooks mappings.'))

        client = self.env['quickbooks.client']
        selected_lines = self.line_ids.filtered(lambda l: l.selected)
        if not selected_lines:
            raise UserError(_('No lines are selected for application. Check the "Apply" box on candidates.'))

        success_count = 0
        error_count = 0

        # Resolve default sales income account for product creation ONCE before loop
        income_account_id = False
        if self.mapping_type == 'product':
            income_account_id = self.env['ir.config_parameter'].sudo().get_param(
                'quickbooks_connector.default_income_account_id', False
            )
            if not income_account_id:
                try:
                    acc_res = client.query("select Id, Name from Account where AccountType = 'Income' and Active = true maxresults 1")
                    accounts = acc_res.get('QueryResponse', {}).get('Account', [])
                    if accounts:
                        income_account_id = accounts[0].get('Id')
                except Exception as e:
                    _logger.warning("Could not auto-fetch QBO income account: %s", e)

        for line in selected_lines:
            try:
                if line.action_to_take == 'map':
                    if not line.candidate_qb_id:
                        raise UserError(_('No candidate QBO ID specified for %s.') % line.name)

                    if self.mapping_type == 'customer' and line.partner_id:
                        line.partner_id.write({'quickbooks_id': str(line.candidate_qb_id)})
                    elif self.mapping_type == 'product' and line.product_tmpl_id:
                        line.product_tmpl_id.write({'quickbooks_id': str(line.candidate_qb_id)})

                    line.write({
                        'status': 'success',
                        'message': _('Successfully mapped to QBO ID %s') % line.candidate_qb_id,
                        'selected': False,
                    })
                    success_count += 1

                elif line.action_to_take == 'create_in_qbo':
                    if self.mapping_type == 'customer' and line.partner_id:
                        payload = {
                            'DisplayName': line.name[:100],
                        }
                        if line.email and '@' in line.email:
                            payload['PrimaryEmailAddr'] = {'Address': line.email}

                        res = client.request('POST', 'customer', payload=payload)
                        cust = res.get('Customer', {})
                        new_id = cust.get('Id')
                        if not new_id:
                            raise UserError(_('QuickBooks did not return a customer ID: %s') % str(res)[:300])

                        line.partner_id.write({'quickbooks_id': str(new_id)})
                        line.write({
                            'candidate_qb_id': str(new_id),
                            'candidate_qb_name': cust.get('DisplayName', line.name),
                            'match_type': 'already_mapped',
                            'status': 'success',
                            'message': _('Created customer in QBO (ID %s)') % new_id,
                            'selected': False,
                        })
                        success_count += 1

                    elif self.mapping_type == 'product' and line.product_tmpl_id:
                        if not income_account_id:
                            raise UserError(_('No active Income Account found in QuickBooks. Configure a Default QBO Sales Account ID in Settings.'))

                        payload = {
                            'Name': line.name[:100],
                            'Type': 'Service' if line.product_tmpl_id.type == 'service' else 'NonInventory',
                            'IncomeAccountRef': {'value': str(income_account_id)},
                        }
                        if line.ref:
                            payload['Sku'] = line.ref[:31]

                        res = client.request('POST', 'item', payload=payload)
                        item = res.get('Item', {})
                        new_id = item.get('Id')
                        if not new_id:
                            raise UserError(_('QuickBooks did not return an item ID: %s') % str(res)[:300])

                        line.product_tmpl_id.write({'quickbooks_id': str(new_id)})
                        line.write({
                            'candidate_qb_id': str(new_id),
                            'candidate_qb_name': item.get('Name', line.name),
                            'match_type': 'already_mapped',
                            'status': 'success',
                            'message': _('Created item in QBO (ID %s)') % new_id,
                            'selected': False,
                        })
                        success_count += 1

                # Commit every 5 items so progress is saved incrementally
                if (success_count + error_count) % 5 == 0:
                    self.env.cr.commit()

            except Exception as e:
                _logger.exception('Failed to apply mapping for %s: %s', line.name, e)
                line.write({
                    'status': 'error',
                    'message': str(e),
                })
                error_count += 1

        # Write result summary into wizard field — no action returned (avoids _preprocessAction crash)
        msg_parts = ['✓ %d linked successfully.' % success_count]
        if error_count:
            msg_parts.append('✗ %d errors — see Details column.' % error_count)
        self.result_message = ' '.join(msg_parts)
        return False

    def action_select_all_matched(self):
        self.ensure_one()
        for line in self.line_ids:
            if line.match_type in ('exact_name', 'email', 'sku'):
                line.selected = True
                line.action_to_take = 'map'
        return False

    def action_select_all_unmatched(self):
        """Select all lines with no QBO match — ready for bulk 'Create As New in QBO'."""
        self.ensure_one()
        for line in self.line_ids:
            if line.match_type == 'none':
                line.selected = True
                line.action_to_take = 'create_in_qbo'
        return False

    def action_deselect_all(self):
        self.ensure_one()
        self.line_ids.write({'selected': False})
        return False


class QuickBooksMappingWizardLine(models.TransientModel):
    _name = 'quickbooks.mapping.wizard.line'
    _description = 'QuickBooks Master Data Mapping Candidate Line'
    _order = 'selected desc, match_type desc, name asc'

    wizard_id = fields.Many2one('quickbooks.mapping.wizard', ondelete='cascade', required=True)
    partner_id = fields.Many2one('res.partner', string='Customer')
    product_tmpl_id = fields.Many2one('product.template', string='Product')

    name = fields.Char(string='Name', required=True)
    ref = fields.Char(string='Reference / SKU')
    email = fields.Char(string='Email')

    current_qb_id = fields.Char(string='Current QBO ID')
    candidate_qb_id = fields.Char(string='Proposed QBO ID')
    candidate_qb_name = fields.Char(string='Proposed QBO Name')

    match_type = fields.Selection([
        ('already_mapped', 'Already Mapped'),
        ('exact_name', 'Exact Name Match'),
        ('email', 'Email Match'),
        ('sku', 'SKU / Ref Match'),
        ('invalid_id', 'Invalid QBO ID'),
        ('none', 'No QBO Match'),
    ], string='Match Type', required=True, default='none')

    action_to_take = fields.Selection([
        ('map', 'Link Existing QBO Record'),
        ('create_in_qbo', 'Create As New in QBO'),
        ('skip', 'Skip'),
    ], string='Action', default='map', required=True)

    selected = fields.Boolean(string='Apply', default=False)
    status = fields.Selection([
        ('pending', 'Pending'),
        ('success', 'Applied'),
        ('error', 'Error'),
    ], default='pending', string='Status')
    message = fields.Text(string='Details')

