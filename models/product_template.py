from odoo import fields, models
class ProductTemplate(models.Model):
    _inherit = 'product.template'
    quickbooks_id = fields.Char(string='QuickBooks Item ID', copy=False, index=True)
