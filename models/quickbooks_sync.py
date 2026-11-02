
import logging
from datetime import datetime, timedelta, timezone

import requests
import odoo.tools

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

TOKEN_URL = 'https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer'


class QuickBooksSyncLog(models.Model):
    _name = 'quickbooks.sync.log'
    _description = 'QuickBooks Synchronization Log'
    _order = 'create_date desc'

    name = fields.Char(required=True)
    model_name = fields.Char()
    res_id = fields.Integer()
    status = fields.Selection(
        [
            ('queued', 'Queued'),
            ('done', 'Done'),
            ('error', 'Error'),
        ],
        default='queued',
        required=True,
    )
    qb_id = fields.Char(string='QuickBooks ID')
    message = fields.Text()
    attempt_count = fields.Integer(default=0)
    company_id = fields.Many2one(
        'res.company',
        default=lambda s: s.env.company,
        required=True,
    )

    def action_retry(self):
        self.write({
            'status': 'queued',
            'message': False,
        })

    def action_process_queue(self):
        self.env['quickbooks.client'].cron_process_queue(limit=50)
        return True


class QuickBooksClient(models.AbstractModel):
    _name = 'quickbooks.client'
    _description = 'QuickBooks Online API Client'

    def _p(self, key, default=False):
        return self.env['ir.config_parameter'].sudo().get_param(
            'quickbooks_connector.' + key,
            default,
        )

    def _base(self):
        if self._p('environment', 'sandbox') == 'production':
            return 'https://quickbooks.api.intuit.com'
        return 'https://sandbox-quickbooks.api.intuit.com'

    def _save_tokens(self, data):
        params = self.env['ir.config_parameter'].sudo()

        params.set_param(
            'quickbooks_connector.access_token',
            data.get('access_token', ''),
        )
        params.set_param(
            'quickbooks_connector.refresh_token',
            data.get('refresh_token', self._p('refresh_token', '')),
        )
        params.set_param(
            'quickbooks_connector.token_expiry',
            (
                datetime.now(timezone.utc)
                + timedelta(seconds=int(data.get('expires_in', 3600)))
            ).isoformat(),
        )

    def _refresh(self):
        if not all([
            self._p('client_id'),
            self._p('client_secret'),
            self._p('refresh_token'),
        ]):
            raise UserError(_(
                'QuickBooks refresh credentials are missing; '
                'reconnect the app.'
            ))

        response = requests.post(
            TOKEN_URL,
            auth=(
                self._p('client_id'),
                self._p('client_secret'),
            ),
            headers={
                'Accept': 'application/json',
                'Content-Type': 'application/x-www-form-urlencoded',
            },
            data={
                'grant_type': 'refresh_token',
                'refresh_token': self._p('refresh_token'),
            },
            timeout=30,
        )

        if not response.ok:
            err_text = response.text or ''
            if 'invalid_grant' in err_text:
                raise UserError(_(
                    'Your QuickBooks Online session has expired or the authorization was revoked (invalid_grant).\n\n'
                    'Please reconnect QuickBooks by going to:\n'
                    'Settings > Invoicing > QuickBooks Online Connection and clicking "Connect to QuickBooks".'
                ))
            raise UserError(_(
                'QuickBooks token refresh failed (%s): %s'
            ) % (response.status_code, err_text[:500]))

        self._save_tokens(response.json())

    def _token(self):
        token = self._p('access_token')
        expiry = self._p('token_expiry')

        if not token:
            raise UserError(_(
                'QuickBooks is not connected. '
                'Configure credentials and authorize first.'
            ))

        try:
            exp = (
                datetime.fromisoformat(expiry)
                if expiry
                else datetime.min.replace(tzinfo=timezone.utc)
            )
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            exp = datetime.min.replace(tzinfo=timezone.utc)

        if datetime.now(timezone.utc) >= exp - timedelta(seconds=90):
            self._refresh()
            token = self._p('access_token')

        return token

    def request(self, method, path, payload=None, params=None):
        realm = self._p('realm_id')

        if not realm:
            raise UserError(_(
                'QuickBooks Company ID is missing.'
            ))

        url = '%s/v3/company/%s/%s' % (
            self._base(),
            realm,
            path.lstrip('/'),
        )

        response = requests.request(
            method,
            url,
            headers={
                'Authorization': 'Bearer ' + self._token(),
                'Accept': 'application/json',
                'Content-Type': 'application/json',
            },
            json=payload,
            params=params,
            timeout=45,
        )

        if not response.ok:
            raise UserError(_(
                'QuickBooks API error (%s): %s'
            ) % (response.status_code, response.text[:1200]))

        return response.json() if response.content else {}

    def query(self, query):
        return self.request(
            'GET',
            'query',
            params={'query': query},
        )

    @api.model
    def preview_invoice(self, invoice_id):
        """
        Fetch a QuickBooks invoice for preview only.

        This method does not create or modify Odoo invoices.
        """
        # Restrict the preview to accounting managers or system admins.
        if not (
            self.env.user.has_group('account.group_account_manager')
            or self.env.user.has_group('base.group_system')
        ):
            raise UserError(_(
                'You are not allowed to preview QuickBooks invoices.'
            ))

        invoice_id = str(invoice_id or '').strip()

        if not invoice_id.isdigit():
            raise UserError(_(
                'Enter a valid QuickBooks invoice ID.'
            ))

        data = self.request(
            'GET',
            'invoice/' + invoice_id,
        )
        invoice = data.get('Invoice')

        if not invoice:
            raise UserError(_(
                'QuickBooks returned no invoice for ID %s.'
            ) % invoice_id)

        existing = self.env['account.move'].sudo().search([
            ('quickbooks_id', '=', invoice_id),
        ], limit=1)

        lines = []

        for line in invoice.get('Line', []):
            if line.get('DetailType') != 'SalesItemLineDetail':
                continue

            detail = line.get('SalesItemLineDetail', {})
            item_ref = detail.get('ItemRef', {})
            tax_ref = detail.get('TaxCodeRef', {})

            lines.append({
                'description': line.get('Description', ''),
                'amount': line.get('Amount', 0.0),
                'item_id': item_ref.get('value'),
                'item_name': item_ref.get('name'),
                'quantity': detail.get('Qty', 0.0),
                'unit_price': detail.get('UnitPrice', 0.0),
                'tax_code_id': tax_ref.get('value'),
            })

        tax_detail = invoice.get('TxnTaxDetail', {})

        # Use QuickBooks SubTotal when provided. It may include line
        # types that are not ordinary sales-item lines.
        subtotal = invoice.get('SubTotal')
        if subtotal is None or float(subtotal or 0.0) == 0.0:
            subtotal = sum(
                float(line.get('amount') or 0.0)
                for line in lines
            )
        else:
            subtotal = float(subtotal)

        return {
            'id': invoice.get('Id'),
            'doc_number': invoice.get('DocNumber'),
            'customer_id': invoice.get('CustomerRef', {}).get('value'),
            'customer_name': invoice.get('CustomerRef', {}).get('name'),
            'date': invoice.get('TxnDate'),
            'due_date': invoice.get('DueDate'),
            'subtotal': subtotal,
            'tax': tax_detail.get('TotalTax', 0.0),
            'total': invoice.get('TotalAmt', 0.0),
            'balance': invoice.get('Balance', 0.0),
            'lines': lines,
            'already_imported': bool(existing),
            'existing_odoo_invoice': (
            existing.display_name if existing else False
            ),
        }

    @api.model
    def get_company_preferences(self):
        """
        Retrieves company info and currency preferences from QuickBooks Online.
        """
        c_info = self.query("select * from CompanyInfo")
        companies = c_info.get('QueryResponse', {}).get('CompanyInfo', [])
        c_data = companies[0] if companies else {}

        p_info = self.query("select * from Preferences")
        prefs = p_info.get('QueryResponse', {}).get('Preferences', [])
        p_data = prefs[0] if prefs else {}
        curr_prefs = p_data.get('CurrencyPrefs', {})

        return {
            'company_name': c_data.get('CompanyName', ''),
            'country': c_data.get('Country', ''),
            'home_currency': curr_prefs.get('HomeCurrency', {}).get('value', 'USD'),
            'multicurrency_enabled': bool(curr_prefs.get('MultiCurrencyEnabled', False)),
        }

    @api.model
    def check_currency_support(self, currency_name):
        """
        Enforces strict safety guard:
        Refuses currency if target QBO company has a different home currency and multicurrency is disabled.
        """
        if not currency_name:
            raise UserError(_('Invoice has no currency defined.'))

        prefs = self.get_company_preferences()
        home_currency = prefs.get('home_currency', 'USD')
        multicurrency = prefs.get('multicurrency_enabled', False)
        company_name = prefs.get('company_name', 'QuickBooks Company')

        if currency_name != home_currency and not multicurrency:
            raise UserError(_(
                'Safety Guard: Cannot process %(currency)s invoice against target QuickBooks company "%(company)s".\n'
                'Target home currency is %(home_curr)s and multi-currency is disabled in QuickBooks.\n'
                'Never export %(currency)s transactions to a %(home_curr)s-only QuickBooks environment.'
            ) % {
                'currency': currency_name,
                'company': company_name,
                'home_curr': home_currency,
            })
        return True

    @api.model
    def find_invoice_by_doc_number(self, doc_number):
        """
        Queries QuickBooks for an invoice matching DocNumber.
        Returns the invoice dict if exactly one match is found, None if zero.
        Raises UserError if multiple matches exist.
        """
        if not doc_number:
            return None
        doc_clean = str(doc_number).strip().replace('\\', '\\\\').replace("'", "\\'")
        res = self.query("select Id, DocNumber, TotalAmt, CustomerRef, Balance from Invoice where DocNumber = '%s'" % doc_clean)
        invoices = res.get('QueryResponse', {}).get('Invoice', [])
        if len(invoices) == 1:
            return invoices[0]
        elif len(invoices) > 1:
            raise UserError(_(
                'Multiple QuickBooks invoices exist with DocNumber "%(doc)s" (IDs: %(ids)s). '
                'Manual reconciliation required.'
            ) % {
                'doc': doc_number,
                'ids': ', '.join(str(inv.get('Id')) for inv in invoices),
            })
        return None

    @api.model
    def find_credit_memo_by_doc_number(self, doc_number):
        """
        Queries QuickBooks for a CreditMemo matching DocNumber.
        Returns the credit memo dict if exactly one match is found, None if zero.
        Raises UserError if multiple matches exist.
        """
        if not doc_number:
            return None
        doc_clean = str(doc_number).strip().replace('\\', '\\\\').replace("'", "\\'")
        res = self.query("select Id, DocNumber, TotalAmt, CustomerRef, Balance from CreditMemo where DocNumber = '%s'" % doc_clean)
        memos = res.get('QueryResponse', {}).get('CreditMemo', [])
        if len(memos) == 1:
            return memos[0]
        elif len(memos) > 1:
            raise UserError(_(
                'Multiple QuickBooks credit memos exist with DocNumber "%(doc)s" (IDs: %(ids)s). '
                'Manual reconciliation required.'
            ) % {
                'doc': doc_number,
                'ids': ', '.join(str(m.get('Id')) for m in memos),
            })
        return None

    @api.model
    def find_payment_by_ref_num(self, ref_num):
        """
        Queries QuickBooks for a Payment matching PaymentRefNum.
        Returns the payment dict if exactly one match is found, None if zero.
        Raises UserError if multiple matches exist.
        """
        if not ref_num:
            return None
        ref_clean = str(ref_num).strip().replace('\\', '\\\\').replace("'", "\\'")
        res = self.query("select Id, PaymentRefNum, TotalAmt, CustomerRef from Payment where PaymentRefNum = '%s'" % ref_clean)
        payments = res.get('QueryResponse', {}).get('Payment', [])
        if len(payments) == 1:
            return payments[0]
        elif len(payments) > 1:
            raise UserError(_(
                'Multiple QuickBooks payments exist with Reference "%(ref)s" (IDs: %(ids)s). '
                'Manual reconciliation required.'
            ) % {
                'ref': ref_num,
                'ids': ', '.join(str(p.get('Id')) for p in payments),
            })
        return None

    @api.model
    def cron_process_queue(self, limit=20):
        logs = self.env['quickbooks.sync.log'].search(
            [('status', '=', 'queued')],
            limit=limit,
        )

        for log in logs:
            target_record = False
            try:
                log.attempt_count += 1
                qb_id = False

                if log.model_name == 'account.move':
                    target_record = self.env['account.move'].browse(log.res_id).exists()
                    if not target_record:
                        log.write({
                            'status': 'error',
                            'message': 'Invoice/Credit Note no longer exists.',
                        })
                        continue
                    qb_id = target_record._quickbooks_export_invoice()

                elif log.model_name == 'account.payment':
                    target_record = self.env['account.payment'].browse(log.res_id).exists()
                    if not target_record:
                        log.write({
                            'status': 'error',
                            'message': 'Payment no longer exists.',
                        })
                        continue
                    qb_id = target_record._quickbooks_export_payment()

                else:
                    log.write({
                        'status': 'error',
                        'message': 'Unsupported model: %s' % log.model_name,
                    })
                    continue

                log.write({
                    'status': 'done',
                    'qb_id': qb_id,
                    'message': 'Exported successfully.',
                })
                if not odoo.tools.config['test_enable']:
                    self.env.cr.commit()

            except Exception as error:
                _logger.exception(
                    'QuickBooks export failed for queue record %s',
                    log.id,
                )

                log.write({
                    'status': 'error',
                    'message': str(error),
                })

                if target_record:
                    target_record.quickbooks_sync_status = 'error'
                if not odoo.tools.config['test_enable']:
                    self.env.cr.commit()

