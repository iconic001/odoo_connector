from odoo import fields, models
class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'
    qb_client_id = fields.Char(config_parameter='quickbooks_connector.client_id')
    qb_client_secret = fields.Char(config_parameter='quickbooks_connector.client_secret', groups='base.group_system')
    qb_environment = fields.Selection([('sandbox','Sandbox'),('production','Production')], default='sandbox', config_parameter='quickbooks_connector.environment')
    qb_realm_id = fields.Char(string='QuickBooks Company ID', config_parameter='quickbooks_connector.realm_id', readonly=True)
    qb_default_income_account_id = fields.Char(string='Default QBO Sales Account ID', config_parameter='quickbooks_connector.default_income_account_id', help='QuickBooks Account ID of type Income used when creating new items in QBO.')
    qb_auto_sync_payments = fields.Boolean(string='Auto-Queue Reconciled Payments', config_parameter='quickbooks_connector.auto_sync_payments', help='Automatically queue payments when their reconciled invoice is exported to QuickBooks.')
    qb_redirect_uri = fields.Char(compute='_compute_qb_redirect_uri', string='OAuth Redirect URI')
    def _compute_qb_redirect_uri(self):
        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url','').rstrip('/')
        for rec in self:
            rec.qb_redirect_uri = base + '/quickbooks/oauth/callback'
