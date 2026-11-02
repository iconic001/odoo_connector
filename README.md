# QuickBooks Online Connector for Odoo 17 Community

**Version**: `17.0.2.0.0`  
**Status**: Production-Grade Accounting Integration Engine

A bi-directional accounting synchronization and migration engine connecting Odoo 17 Community with QuickBooks Online. Built with fail-closed accounting safety guards, explicit tax mapping, master data onboarding wizard, full customer payment & credit note support, and network idempotency.

---

## Complete Enterprise Feature Set

1. **OAuth 2.0 Integration & Token Lifecycle**:
   - Secure authorization code flow with automatic refresh token rotation and expiry tracking.
   - Dynamic sandbox / production API environment routing.

2. **Master Data Mapping & Onboarding Wizard (`Accounting > QuickBooks > Master Data Mapping`)**:
   - Live scanning and side-by-side matching of Odoo contacts (`res.partner`) and products (`product.template`) against active QuickBooks Online entities.
   - Automatic candidate matching by exact DisplayName, company name, primary email, or internal reference / SKU.
   - Batch application with human review: link existing QBO records or create missing contacts/items directly in QuickBooks with approved income accounts.

3. **Invoices & Customer Credit Notes (`POST /invoice` & `POST /creditmemo`)**:
   - Support for customer sales invoices (`out_invoice`) and credit notes / refunds (`out_refund`).
   - Dynamic endpoint switching (`Invoice` vs. `CreditMemo`) with consistent payload and tax structures.
   - Preserves original transaction dates, document numbers, and customer references.

4. **Customer Payment Synchronization (`POST /payment`)**:
   - Inbound customer payments (`account.payment`) synced to QuickBooks `Payment` (`ReceivePayment`).
   - Automatically links payments to their corresponding QuickBooks invoice transaction ID (`LinkedTxn`), closing out open AR balances.
   - Optional setting: Auto-queue reconciled payments whenever an invoice is successfully exported.

5. **Migration Readiness Assessment (`Accounting > QuickBooks > Migration Readiness`)**:
   - Strictly read-only assessment scanning historical customer invoices.
   - Classifies invoices into **READY**, **REVIEW REQUIRED**, and **BLOCKED** with financial totals and drill-down blocker breakdowns.
   - Live query of target QuickBooks company name, country, home currency, and multi-currency status.

6. **Multi-Currency & Company Safety Guard**:
   - Enforces strict currency matching before queueing or exporting (`check_currency_support`).
   - Hard blocks exporting foreign currency transactions (e.g., KES) to a target QuickBooks company with single-currency USD enabled.
   - Automatic `CurrencyRef` propagation when multi-currency is enabled in QuickBooks.

7. **Explicit Tax Code Mapping (`Accounting > QuickBooks > Tax Mappings`)**:
   - Explicit company-level mapping between Odoo `account.tax` rates and QuickBooks `TaxCodeRef` IDs.
   - Fails closed on unmapped or compound taxes to prevent accounting discrepancies.
   - Enforces consistency between 0% tax classifications (zero-rated vs. exempt).

8. **Idempotency & Ambiguous POST Recovery**:
   - Pre-check: queries QuickBooks by `DocNumber` or `PaymentRefNum` prior to POSTing to detect prior creation and reconcile without duplication.
   - Server-Side Deduplication: sends native Intuit `requestid` with every POST.
   - Ambiguous Network Recovery: if an HTTP timeout or network drop occurs during a POST, queries QuickBooks by document number to determine if the record was committed before raising errors.

9. **Safety Queue & Automated Processing**:
   - Outbound records queued in `quickbooks.sync.log` with retry counters and detailed error tracking.
   - Handles `account.move` (invoices and credit notes) and `account.payment`.
   - Scheduled cron (`ir.cron`) defined with safe defaults (`active=False` during initial migration setup).

10. **Comprehensive Automated Test Suite**:
    - Unit tests under `tests/` covering currency guards, tax mappings, export validations, payment sync, credit memos, idempotency pre-checks, timeout recovery, and master data matching.

---

## Installation & Setup (Windows)

1. Extract `quickbooks_connector` into your custom addons directory listed in `odoo.conf`.
2. Ensure `requests` is installed in the Odoo Python runtime.
3. Update Apps list and upgrade/install **QuickBooks Online Connector**.
4. In **Settings > Accounting > QuickBooks**:
   - Enter your Intuit Client ID and Client Secret.
   - Set environment to **Sandbox** (or **Production** when ready).
   - Enter your Default QBO Sales Account ID (Income Account for auto-created items).
   - Check **Auto-Queue Reconciled Payments** if you want invoice exports to automatically sync their payments.
   - Register the displayed Redirect URI in your Intuit Developer App settings.
   - Click **Connect to QuickBooks** and complete authorization.

---

## Pre-Flight Checklist for Going Live

Before switching environment from Sandbox to Production:
1. **Target QBO Company**: Connect to your production QuickBooks company and verify home currency (`KES` or multi-currency).
2. **Master Data Wizard**: Run **Accounting > QuickBooks > Master Data Mapping** to map your customer contacts and products in bulk.
3. **Tax Mappings**: Configure active mappings under **Accounting > QuickBooks > Tax Mappings** for standard VAT (16%), Zero-Rated (0%), and Exempt.
4. **Readiness Report**: Run **Accounting > QuickBooks > Migration Readiness** to confirm that invoices show as **READY**.
5. **Background Automation**: Once verified, enable the scheduled cron in **Settings > Technical > Scheduled Actions > QuickBooks: Process Export Queue**.


