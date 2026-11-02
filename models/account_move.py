# -*- coding: utf-8 -*-

import logging
import odoo.tools
from odoo import fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = 'account.move'

    quickbooks_id = fields.Char(string='QuickBooks Invoice ID', copy=False, readonly=True, index=True)
    quickbooks_sync_status = fields.Selection([
        ('not_synced', 'Not Synced'),
        ('queued', 'Queued'),
        ('synced', 'Synced'),
        ('error', 'Error'),
    ], default='not_synced', copy=False, readonly=True)

    def action_quickbooks_queue_export(self):
        client = self.env['quickbooks.client']
        for m in self:
            if m.move_type not in ('out_invoice', 'out_refund') or m.state != 'posted':
                raise UserError(_('Only posted customer invoices and credit notes can be exported.'))
            if m.quickbooks_id:
                raise UserError(_('This record already has a QuickBooks ID.'))

            # Phase 2 Target Company & Currency Guard
            client.check_currency_support(m.currency_id.name)

            existing = self.env['quickbooks.sync.log'].search([
                ('model_name', '=', 'account.move'),
                ('res_id', '=', m.id),
                ('status', 'in', ['queued', 'done']),
            ], limit=1)
            if existing:
                raise UserError(_('An export queue record already exists; check QuickBooks Sync Queue.'))

            label = 'Credit Note' if m.move_type == 'out_refund' else 'Invoice'
            self.env['quickbooks.sync.log'].create({
                'name': '%s %s' % (label, m.name),
                'model_name': 'account.move',
                'res_id': m.id,
            })
            m.quickbooks_sync_status = 'queued'

        # Immediately process queue so interactive clicks export on the spot
        if not odoo.tools.config['test_enable']:
            self.env['quickbooks.client'].cron_process_queue(limit=len(self))
        return True

    def _quickbooks_export_invoice(self):
        self.ensure_one()
        if self.quickbooks_id:
            return self.quickbooks_id

        client = self.env['quickbooks.client']

        # Phase 2 Target Company & Currency Guard
        client.check_currency_support(self.currency_id.name)

        partner = self.partner_id.commercial_partner_id
        customer_id = partner.quickbooks_id
        if not customer_id:
            name = partner.name.replace('\\', '\\\\').replace("'", "\\'")
            result = client.query("select Id, DisplayName from Customer where DisplayName = '%s'" % name)
            matches = result.get('QueryResponse', {}).get('Customer', [])
            if len(matches) != 1:
                raise UserError(_('Map customer %s to one QuickBooks Customer ID first.') % partner.display_name)
            customer_id = matches[0]['Id']

        TaxMapping = self.env['quickbooks.tax.mapping']
        lines = []

        # Filter valid product lines in Odoo 17
        valid_lines = self.invoice_line_ids.filtered(
            lambda l: l.display_type not in ('line_section', 'line_note') and (l.quantity or l.price_subtotal)
        )

        for line in valid_lines:
            item_id = line.product_id.product_tmpl_id.quickbooks_id
            if not item_id:
                raise UserError(_('Map product/service %s to a QuickBooks Item ID first.') % line.product_id.display_name)

            # Phase 3 Explicit Tax Mapping
            tax_ref = TaxMapping.get_line_tax_code_ref(line)

            line_detail = {
                'ItemRef': {'value': str(item_id)},
                'Qty': line.quantity,
                'UnitPrice': line.price_unit,
            }
            if tax_ref:
                line_detail['TaxCodeRef'] = tax_ref

            lines.append({
                'Amount': round(line.price_subtotal, 2),
                'DetailType': 'SalesItemLineDetail',
                'Description': (line.name or line.product_id.display_name or '')[:4000],
                'SalesItemLineDetail': line_detail,
            })

        if not lines:
            raise UserError(_('No product/service lines found.'))

        is_credit_note = (self.move_type == 'out_refund')
        endpoint = 'creditmemo' if is_credit_note else 'invoice'
        entity_key = 'CreditMemo' if is_credit_note else 'Invoice'
        record_label = 'Credit Note' if is_credit_note else 'Invoice'

        doc_number = (self.name or '')[:21]

        # Phase 4 Idempotency Pre-Check:
        # Check if a record with this DocNumber already exists in QBO before sending POST.
        if is_credit_note:
            existing_inv = client.find_credit_memo_by_doc_number(doc_number)
        else:
            existing_inv = client.find_invoice_by_doc_number(doc_number)

        if existing_inv:
            qbo_cust_id = str(existing_inv.get('CustomerRef', {}).get('value'))
            if qbo_cust_id == str(customer_id):
                _logger.info(
                    'Idempotency match: %s %s already exists in QuickBooks (ID %s). Reconciling without duplicate.',
                    record_label, self.name, existing_inv.get('Id')
                )
                self.write({
                    'quickbooks_id': str(existing_inv['Id']),
                    'quickbooks_sync_status': 'synced',
                })
                return str(existing_inv['Id'])
            else:
                raise UserError(_(
                    'A QuickBooks %(type)s with DocNumber "%(doc)s" already exists (ID %(id)s), '
                    'but belongs to customer %(q_cust)s instead of expected customer %(exp_cust)s. '
                    'Manual reconciliation required.'
                ) % {
                    'type': record_label,
                    'doc': doc_number,
                    'id': existing_inv.get('Id'),
                    'q_cust': qbo_cust_id,
                    'exp_cust': customer_id,
                })

        payload = {
            'CustomerRef': {'value': str(customer_id)},
            'TxnDate': str(self.invoice_date or self.date),
            'DocNumber': doc_number,
            'PrivateNote': 'Created from Odoo %s %s' % (record_label.lower(), self.name),
            'Line': lines,
        }

        # Multi-currency support
        prefs = client.get_company_preferences()
        if prefs.get('multicurrency_enabled'):
            payload['CurrencyRef'] = {'value': self.currency_id.name}

        # Native Intuit requestid parameter provides server-side deduplication
        request_id = f"odoo_move_{self.id}"
        try:
            data = client.request('POST', endpoint, payload=payload, params={'requestid': request_id})
        except Exception as post_error:
            # Ambiguous POST recovery: if timeout or network drop occurred after QBO processed the request,
            # query QBO by DocNumber before propagating error.
            _logger.warning(
                'POST /%s raised error (%s) for %s. Checking QBO for ambiguous creation recovery...',
                endpoint, post_error, self.name
            )
            try:
                if is_credit_note:
                    recovered_inv = client.find_credit_memo_by_doc_number(doc_number)
                else:
                    recovered_inv = client.find_invoice_by_doc_number(doc_number)

                if recovered_inv and str(recovered_inv.get('CustomerRef', {}).get('value')) == str(customer_id):
                    _logger.info(
                        'Ambiguous POST recovered successfully: %s %s was created in QBO with ID %s.',
                        record_label, self.name, recovered_inv.get('Id')
                    )
                    self.write({
                        'quickbooks_id': str(recovered_inv['Id']),
                        'quickbooks_sync_status': 'synced',
                    })
                    return str(recovered_inv['Id'])
            except Exception as rec_err:
                _logger.error('Failed to query QBO during ambiguous POST recovery check: %s', rec_err)
            raise

        res_record = data.get(entity_key)
        if not res_record or not res_record.get('Id'):
            raise UserError(_('QuickBooks did not return a %(type)s ID: %(data)s') % {
                'type': record_label,
                'data': str(data)[:500],
            })

        self.write({
            'quickbooks_id': str(res_record['Id']),
            'quickbooks_sync_status': 'synced',
        })

        # Phase 5: Auto-Queue Linked Payments if Enabled
        if not is_credit_note:
            auto_pay = self.env['ir.config_parameter'].sudo().get_param('quickbooks_connector.auto_sync_payments', False)
            if auto_pay:
                reconciled_payments = self._get_reconciled_payments() if hasattr(self, '_get_reconciled_payments') else self.env['account.payment']
                for pay in reconciled_payments:
                    if pay.state == 'posted' and not pay.quickbooks_id and pay.quickbooks_sync_status != 'queued':
                        try:
                            pay.action_quickbooks_queue_export()
                            _logger.info("Auto-queued reconciled payment %s for exported invoice %s", pay.name, self.name)
                        except Exception as pe:
                            _logger.warning("Could not auto-queue payment %s: %s", pay.name, pe)

        return str(res_record['Id'])
