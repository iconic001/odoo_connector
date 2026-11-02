
from odoo import fields, models, _
from odoo.exceptions import UserError


class QuickBooksInvoicePreview(models.TransientModel):
    _name = 'quickbooks.invoice.preview'
    _description = 'QuickBooks Invoice Preview'

    invoice_id = fields.Char(
        string='QuickBooks Invoice ID',
        required=True,
        default='130',
    )

    qb_doc_number = fields.Char(readonly=True)
    qb_customer = fields.Char(readonly=True)
    qb_date = fields.Date(readonly=True)
    qb_due_date = fields.Date(readonly=True)
    qb_subtotal = fields.Float(readonly=True, digits=(16, 2))
    qb_tax = fields.Float(readonly=True, digits=(16, 2))
    qb_total = fields.Float(readonly=True, digits=(16, 2))
    qb_balance = fields.Float(readonly=True, digits=(16, 2))
    qb_already_imported = fields.Boolean(readonly=True)
    qb_existing_invoice = fields.Char(readonly=True)
    preview_message = fields.Text(readonly=True)

    def action_preview(self):
        self.ensure_one()

        result = self.env['quickbooks.client'].preview_invoice(
            self.invoice_id
        )

        self.write({
            'qb_doc_number': result.get('doc_number') or '',
            'qb_customer': result.get('customer_name') or '',
            'qb_date': result.get('date') or False,
            'qb_due_date': result.get('due_date') or False,
            'qb_subtotal': float(result.get('subtotal') or 0),
            'qb_tax': float(result.get('tax') or 0),
            'qb_total': float(result.get('total') or 0),
            'qb_balance': float(result.get('balance') or 0),
            'qb_already_imported': result.get('already_imported', False),
            'qb_existing_invoice': (
                result.get('existing_odoo_invoice') or ''
            ),
            'preview_message': (
                'Preview loaded successfully. No invoice was created '
                'or modified.'
            ),
        })

        return {
            'type': 'ir.actions.act_window',
            'name': _('QuickBooks Invoice Preview'),
            'res_model': self._name,
            'view_mode': 'form',
            'res_id': self.id,
            'target': 'new',
        }
