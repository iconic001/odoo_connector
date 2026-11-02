# -*- coding: utf-8 -*-

import logging
import odoo.tools
from odoo import fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    quickbooks_id = fields.Char(string='QuickBooks Payment ID', copy=False, readonly=True, index=True)
    quickbooks_sync_status = fields.Selection([
        ('not_synced', 'Not Synced'),
        ('queued', 'Queued'),
        ('synced', 'Synced'),
        ('error', 'Error'),
    ], default='not_synced', copy=False, readonly=True)

    def action_quickbooks_queue_export(self):
        client = self.env['quickbooks.client']
        for pay in self:
            if pay.payment_type != 'inbound' or pay.state != 'posted':
                raise UserError(_('Only posted inbound customer payments can be exported.'))
            if pay.quickbooks_id:
                raise UserError(_('This payment already has a QuickBooks ID.'))

            client.check_currency_support(pay.currency_id.name)

            existing = self.env['quickbooks.sync.log'].search([
                ('model_name', '=', 'account.payment'),
                ('res_id', '=', pay.id),
                ('status', 'in', ['queued', 'done']),
            ], limit=1)
            if existing:
                raise UserError(_('An export queue record already exists for payment %s.') % pay.name)

            self.env['quickbooks.sync.log'].create({
                'name': 'Payment %s' % pay.name,
                'model_name': 'account.payment',
                'res_id': pay.id,
            })
            pay.quickbooks_sync_status = 'queued'

        # Immediately process queue so interactive clicks export on the spot
        if not odoo.tools.config['test_enable']:
            self.env['quickbooks.client'].cron_process_queue(limit=len(self))
        return True

    def _quickbooks_export_payment(self):
        self.ensure_one()
        if self.quickbooks_id:
            return self.quickbooks_id

        client = self.env['quickbooks.client']
        client.check_currency_support(self.currency_id.name)

        partner = self.partner_id.commercial_partner_id
        customer_id = partner.quickbooks_id
        if not customer_id:
            raise UserError(_('Map customer %s to a QuickBooks Customer ID first.') % partner.display_name)

        doc_number = (self.name or '')[:21]

        # Phase 4 Idempotency Pre-Check: verify if payment with this PaymentRefNum already exists in QBO
        existing_pay = client.find_payment_by_ref_num(doc_number)
        if existing_pay:
            qbo_cust_id = str(existing_pay.get('CustomerRef', {}).get('value'))
            if qbo_cust_id == str(customer_id):
                _logger.info(
                    'Idempotency match: Payment %s already exists in QuickBooks (ID %s). Reconciling without duplicate.',
                    self.name, existing_pay.get('Id')
                )
                self.write({
                    'quickbooks_id': str(existing_pay['Id']),
                    'quickbooks_sync_status': 'synced',
                })
                return str(existing_pay['Id'])
            else:
                raise UserError(_(
                    'A QuickBooks payment with reference "%(doc)s" already exists (ID %(id)s), '
                    'but belongs to customer %(q_cust)s instead of %(exp_cust)s.'
                ) % {
                    'doc': doc_number,
                    'id': existing_pay.get('Id'),
                    'q_cust': qbo_cust_id,
                    'exp_cust': customer_id,
                })

        # Match linked reconciled customer invoices in QBO
        lines = []
        reconciled_moves = self.reconciled_invoice_ids.filtered(lambda m: m.move_type == 'out_invoice')
        for inv in reconciled_moves:
            if inv.quickbooks_id:
                applied_amount = min(self.amount, inv.amount_total)
                lines.append({
                    'Amount': round(applied_amount, 2),
                    'LinkedTxn': [{
                        'TxnId': str(inv.quickbooks_id),
                        'TxnType': 'Invoice',
                    }]
                })

        payload = {
            'CustomerRef': {'value': str(customer_id)},
            'TotalAmt': round(self.amount, 2),
            'TxnDate': str(self.date),
            'PaymentRefNum': doc_number,
            'PrivateNote': 'Created from Odoo payment %s' % self.name,
        }
        if lines:
            payload['Line'] = lines

        prefs = client.get_company_preferences()
        if prefs.get('multicurrency_enabled'):
            payload['CurrencyRef'] = {'value': self.currency_id.name}

        request_id = f"odoo_pay_{self.id}"
        try:
            data = client.request('POST', 'payment', payload=payload, params={'requestid': request_id})
        except Exception as post_error:
            _logger.warning('POST /payment raised error (%s) for %s. Checking recovery...', post_error, self.name)
            try:
                recovered = client.find_payment_by_ref_num(doc_number)
                if recovered and str(recovered.get('CustomerRef', {}).get('value')) == str(customer_id):
                    _logger.info('Ambiguous POST recovered successfully: Payment %s found in QBO with ID %s.', self.name, recovered.get('Id'))
                    self.write({
                        'quickbooks_id': str(recovered['Id']),
                        'quickbooks_sync_status': 'synced',
                    })
                    return str(recovered['Id'])
            except Exception as rec_err:
                _logger.error('Failed recovery query for payment: %s', rec_err)
            raise

        payment_data = data.get('Payment')
        if not payment_data or not payment_data.get('Id'):
            raise UserError(_('QuickBooks did not return a payment ID: %s') % str(data)[:500])

        self.write({
            'quickbooks_id': str(payment_data['Id']),
            'quickbooks_sync_status': 'synced',
        })
        return str(payment_data['Id'])

