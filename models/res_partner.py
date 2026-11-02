from odoo import fields, models
class ResPartner(models.Model):
    _inherit = 'res.partner'
    quickbooks_id = fields.Char(string='QuickBooks Customer ID', copy=False, index=True)
