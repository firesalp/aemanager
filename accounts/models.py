# coding=utf-8
from decimal import Decimal
from django.utils.formats import localize
import datetime
from custom_canvas import NumberedCanvas
from reportlab.platypus import Paragraph, Frame, Spacer, BaseDocTemplate, PageTemplate
from reportlab.lib.styles import ParagraphStyle
from reportlab.rl_config import defaultPageSize
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import Table, TableStyle, Image
from reportlab.lib import colors
from django.db import models, connection
from core.models import OwnedObject
from django.utils.translation import ugettext_lazy as _, ugettext
from contact.models import Contact, CONTACT_TYPE_COMPANY
from django.core.urlresolvers import reverse
from project.models import Row, Proposal, update_row_amount, \
    ROW_CATEGORY_SERVICE, ROW_CATEGORY, PROPOSAL_STATE_ACCEPTED, ProposalRow
from django.db.models.aggregates import Sum, Min, Max
from django.db.models.signals import post_save, pre_save, post_delete
from django.conf import settings
from django.core.validators import MaxValueValidator
from django.db.models.expressions import F

PAYMENT_TYPE_CASH = 1
PAYMENT_TYPE_BANK_CARD = 2
PAYMENT_TYPE_TRANSFER = 3
PAYMENT_TYPE_CHECK = 4
PAYMENT_TYPE = ((PAYMENT_TYPE_CASH, _('Cash')),
                (PAYMENT_TYPE_BANK_CARD, _('Bank card')),
                (PAYMENT_TYPE_TRANSFER, _('Transfer')),
                (PAYMENT_TYPE_CHECK, _('Check')))

class Expense(OwnedObject):
    date = models.DateField(verbose_name=_('Date'), help_text=_('format: mm/dd/yyyy'), db_index=True)
    reference = models.CharField(max_length=50, blank=True, null=True, verbose_name=_('Reference'))
    supplier = models.CharField(max_length=70, blank=True, null=True, verbose_name=_('Supplier'))
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_('Amount'))
    payment_type = models.IntegerField(choices=PAYMENT_TYPE, verbose_name=_('Payment type'))
    description = models.CharField(max_length=100, blank=True, null=True, verbose_name=_('Description'))

class InvoiceAmountError(Exception):
    pass

class InvoiceIdNotUniqueError(Exception):
    pass

class InvalidInvoiceIdError(Exception):
    pass

MAX_INVOICE_ID = 999999999

INVOICE_STATE_EDITED = 1
INVOICE_STATE_SENT = 2
INVOICE_STATE_PAID = 3
INVOICE_STATE = ((INVOICE_STATE_EDITED, _('Edited')),
              (INVOICE_STATE_SENT, _('Sent')),
              (INVOICE_STATE_PAID, _('Paid')))

class InvoiceManager(models.Manager):
    def get_next_invoice_id(self, owner):
        return (Invoice.objects.filter(owner=owner).aggregate(invoice_id=Max('invoice_id'))['invoice_id'] or 0) + 1

    def get_paid_sales(self, owner, reference_date=None):
        if not reference_date:
            reference_date = datetime.date.today()
        amount_sum = self.filter(state=INVOICE_STATE_PAID,
                                 owner=owner,
                                 paid_date__lte=reference_date,
                                 paid_date__year=reference_date.year).aggregate(sales=Sum('amount'))
        return amount_sum['sales'] or 0

    def get_paid_service_sales(self, owner, year=None):
        if not year:
            year = datetime.date.today().year
        amount_sum = InvoiceRow.objects.filter(invoice__state=INVOICE_STATE_PAID,
                                               owner=owner,
                                               category=ROW_CATEGORY_SERVICE,
                                               invoice__paid_date__year=year).aggregate(sales=Sum('amount'))
        return amount_sum['sales'] or 0

    def get_waiting_payments(self, owner):
        amount_sum = self.filter(state=INVOICE_STATE_SENT,
                                 owner=owner).aggregate(sales=Sum('amount'))
        return amount_sum['sales'] or 0

    def get_waiting_service_payments(self, owner):
        amount_sum = InvoiceRow.objects.filter(invoice__state=INVOICE_STATE_SENT,
                                               owner=owner,
                                               category=ROW_CATEGORY_SERVICE).aggregate(sales=Sum('amount'))
        return amount_sum['sales'] or 0

    def get_late_invoices(self, owner):
        late_invoices = self.filter(state=INVOICE_STATE_SENT,
                                    payment_date__lt=datetime.date.today(),
                                    owner=owner)
        return late_invoices

    def get_late_invoices_for_notification(self):
        late_invoices = self.filter(state=INVOICE_STATE_SENT,
                                    payment_date__lt=datetime.date.today(),
                                    owner__notification__notify_late_invoices=True)
        return late_invoices

    def get_invoices_to_send(self, owner):
        invoices_to_send = self.filter(state=INVOICE_STATE_EDITED,
                                       edition_date__lte=datetime.date.today(),
                                       owner=owner)
        return invoices_to_send

    def get_invoices_to_send_for_notification(self):
        invoices_to_send = self.filter(state=INVOICE_STATE_EDITED,
                                       edition_date__lte=datetime.date.today(),
                                       owner__notification__notify_invoices_to_send=True)
        return invoices_to_send

    def get_paid_sales_for_period(self, owner, begin_date, end_date):
        if not begin_date or not end_date:
            return 0
        amount_sum = self.filter(state=INVOICE_STATE_PAID,
                                 owner=owner,
                                 paid_date__gte=begin_date,
                                 paid_date__lte=end_date).aggregate(sales=Sum('amount'))
        return amount_sum['sales'] or 0

    def get_waiting_sales_for_period(self, owner, end_date, begin_date=None):
        if not end_date:
            return 0
        amount_sum = self.filter(state__lte=INVOICE_STATE_SENT,
                                 owner=owner,
                                 payment_date__lte=end_date)
        if begin_date:
            amount_sum = amount_sum.filter(payment_date__gte=begin_date)
        amount_sum = amount_sum.aggregate(waiting=Sum('amount'))
        return amount_sum['waiting'] or 0

    def get_first_invoice_paid_date(self, owner):
        return self.filter(owner=owner).aggregate(min_date=Min('paid_date'))['min_date']

    def get_paid_invoices(self, owner, begin_date=None):
        if not begin_date:
            return self.filter(state=INVOICE_STATE_PAID,
                               owner=owner,
                               paid_date__year=datetime.date.today().year).order_by('paid_date')
        else:
            return self.filter(state=INVOICE_STATE_PAID,
                               owner=owner,
                               paid_date__lte=datetime.date.today(),
                               paid_date__gte=begin_date).order_by('paid_date')

    def get_waiting_invoices(self, owner):
        return self.filter(state__lte=INVOICE_STATE_SENT,
                           owner=owner).order_by('payment_date')

    def get_to_be_invoiced(self, owner):
        accepted_proposal_amount_sum = Proposal.objects.filter(state=PROPOSAL_STATE_ACCEPTED,
                                                   owner=owner).extra(where=['project_proposal.ownedobject_ptr_id NOT IN (SELECT proposal_id FROM accounts_invoicerow irow JOIN accounts_invoice i ON irow.invoice_id = i.ownedobject_ptr_id WHERE i.state IN (%s,%s) AND irow.balance_payments = %s)'],
                                                                                 params=[INVOICE_STATE_SENT, INVOICE_STATE_PAID, True]).aggregate(amount=Sum('amount'))
        # exclude amount found in sent or paid invoices referencing accepted proposal, aka computing already invoiced from not sold proposal
        invoicerows_to_exclude = InvoiceRow.objects.extra(where=['accounts_invoicerow.proposal_id NOT IN (SELECT proposal_id FROM accounts_invoicerow irow JOIN accounts_invoice i ON irow.invoice_id = i.ownedobject_ptr_id WHERE i.state IN (%s,%s) AND irow.balance_payments = %s)'],
                                                          params=[INVOICE_STATE_SENT, INVOICE_STATE_PAID, True]).exclude(invoice__state=INVOICE_STATE_EDITED).filter(owner=owner).aggregate(amount=Sum('amount'))

        # adding invoice rows of edited invoices which don't have proposal linked
        invoicerows_whithout_proposals = InvoiceRow.objects.filter(owner=owner,
                                                                   proposal=None,
                                                                   invoice__state=INVOICE_STATE_EDITED).aggregate(amount=Sum('amount'))
        return (accepted_proposal_amount_sum['amount'] or 0) - (invoicerows_to_exclude['amount'] or 0) + (invoicerows_whithout_proposals['amount'] or 0)

    def get_service_to_be_invoiced(self, owner):
        accepted_proposal_amount_sum = ProposalRow.objects.filter(proposal__state=PROPOSAL_STATE_ACCEPTED,
                                                                  category=ROW_CATEGORY_SERVICE,
                                                                  owner=owner).extra(where=['project_proposal.ownedobject_ptr_id NOT IN (SELECT proposal_id FROM accounts_invoicerow irow JOIN accounts_invoice i ON irow.invoice_id = i.ownedobject_ptr_id WHERE i.state IN (%s,%s) AND irow.balance_payments = %s)'],
                                                                                 params=[INVOICE_STATE_SENT, INVOICE_STATE_PAID, True]).aggregate(amount=Sum('amount'))
        invoicerows_to_exclude = InvoiceRow.objects.filter(proposal__state=PROPOSAL_STATE_ACCEPTED,
                                                           category=ROW_CATEGORY_SERVICE,
                                                           owner=owner).extra(where=['accounts_invoicerow.proposal_id NOT IN (SELECT proposal_id FROM accounts_invoicerow irow JOIN accounts_invoice i ON irow.invoice_id = i.ownedobject_ptr_id WHERE i.state IN (%s,%s) AND irow.balance_payments = %s)'],
                                                                             params=[INVOICE_STATE_SENT, INVOICE_STATE_PAID, True]).exclude(invoice__state=INVOICE_STATE_EDITED).filter(owner=owner).aggregate(amount=Sum('amount'))
        return (accepted_proposal_amount_sum['amount'] or 0) - (invoicerows_to_exclude['amount'] or 0)

class Invoice(OwnedObject):
    customer = models.ForeignKey(Contact, blank=True, null=True, verbose_name=_('Customer'))
    invoice_id = models.IntegerField(verbose_name=_("Invoice id"))
    state = models.IntegerField(choices=INVOICE_STATE, default=INVOICE_STATE_EDITED, verbose_name=_("State"), db_index=True)
    amount = models.DecimalField(blank=True, max_digits=12, decimal_places=2, default=0, verbose_name=_("Amount"))
    edition_date = models.DateField(verbose_name=_("Edition date"), help_text=_('format: mm/dd/yyyy'), db_index=True)
    payment_date = models.DateField(verbose_name=_("Payment date"), help_text=_('format: mm/dd/yyyy'), db_index=True)
    payment_type = models.IntegerField(choices=PAYMENT_TYPE, blank=True, null=True, verbose_name=_('Payment type'))
    paid_date = models.DateField(blank=True, null=True, verbose_name=_("Paid date"), help_text=_('format: mm/dd/yyyy'), db_index=True)
    execution_begin_date = models.DateField(blank=True, null=True, verbose_name=_("Execution begin date"), help_text=_('format: mm/dd/yyyy'))
    execution_end_date = models.DateField(blank=True, null=True, verbose_name=_("Execution end date"), help_text=_('format: mm/dd/yyyy'))
    penalty_date = models.DateField(blank=True, null=True, verbose_name=_("Penalty date"), help_text=_('format: mm/dd/yyyy'))
    penalty_rate = models.DecimalField(blank=True, null=True, max_digits=4, decimal_places=2, verbose_name=_("Penalty rate"))
    discount_conditions = models.CharField(max_length=100, blank=True, null=True, verbose_name=_("Discount conditions"))

    objects = InvoiceManager()

    class Meta:
        ordering = ['invoice_id']

    def __unicode__(self):
        return "<a href=\"%s\">%s</a>" % (reverse('invoice_detail', kwargs={'id' : self.id}), ugettext("invoice #%d") % (self.invoice_id))

    def isInvoiceIdValid(self):
        validator = MaxValueValidator(MAX_INVOICE_ID)
        try:
            validator(self.invoice_id)
        except:
            return False
        return True

    def isInvoiceIdUnique(self, owner):
        invoices = Invoice.objects.filter(owner=owner,
                                          invoice_id=self.invoice_id)
        if self.id:
            invoices = invoices.exclude(id=self.id)

        if len(invoices):
            return False

        return True

    def getNature(self):
        natures = self.invoice_rows.values_list('category', flat=True).order_by('category').distinct()
        result = []
        natures_dict = dict(ROW_CATEGORY)
        for nature in natures:
            result.append(unicode(natures_dict[nature]))

        return " & ".join(result)

    def save(self, force_insert=False, force_update=False, using=None, user=None):
        if not self.isInvoiceIdValid():
            raise InvalidInvoiceIdError(ugettext('Invoice id must be less than or equal to %d') % (MAX_INVOICE_ID))
        if not self.isInvoiceIdUnique(user):
            raise InvoiceIdNotUniqueError(ugettext("Invoice id must be unique"))
        super(Invoice, self).save(force_insert, force_update, using, user)

    def check_amounts(self):
        proposals = Proposal.objects.filter(invoice_rows__invoice=self).distinct()
        for proposal in proposals:
            remaining_amount = proposal.get_remaining_to_invoice(exclude_invoice=self)
            rows_amount = InvoiceRow.objects.filter(invoice=self,
                                                    proposal=proposal).aggregate(amount=Sum('amount'))['amount'] or 0
            if float(remaining_amount) < float(rows_amount):
                raise InvoiceRowAmountError(ugettext("Amounts invoiced can't be greater than proposals remaining amounts"))

        return True

    def get_vat(self):
        cursor = connection.cursor()
        cursor.execute('SELECT SUM(accounts_invoicerow.amount * accounts_invoicerow.vat_rate / 100) AS "vat" FROM "accounts_invoicerow" WHERE "accounts_invoicerow"."invoice_id" = %s', [self.id])
        row = cursor.fetchone()
        vat = row[0]
        vat = vat.quantize(Decimal(1)) if vat == vat.to_integral() else vat.normalize()
        return vat

    def amount_including_tax(self):
        return self.amount + self.get_vat()

    def to_pdf(self, user, response):
        def invoice_footer(canvas, doc):
            canvas.saveState()
            canvas.setFont('Times-Roman', 10)
            PAGE_WIDTH = defaultPageSize[0]
            footer_text = "%s %s - %s, %s %s" % (user.first_name,
                                                 user.last_name,
                                                 user.get_profile().address.street.replace("\n", ", ").replace("\r", ""),
                                                 user.get_profile().address.zipcode,
                                                 user.get_profile().address.city)
            if user.get_profile().address.country:
                footer_text = footer_text + u", %s" % (user.get_profile().address.country)

            canvas.drawCentredString(PAGE_WIDTH / 2.0, 0.5 * inch, footer_text)
            extra_info = u"SIRET : %s" % (user.get_profile().company_id)
            if user.get_profile().vat_number:
                extra_info = u"%s - N° TVA : %s" % (extra_info, user.get_profile().vat_number)
            canvas.drawCentredString(PAGE_WIDTH / 2.0, 0.35 * inch, extra_info)
            canvas.restoreState()

        filename = ugettext('invoice_%(invoice_id)d.pdf') % {'invoice_id': self.invoice_id}
        response['Content-Disposition'] = 'attachment; filename=%s' % (filename)

        doc = BaseDocTemplate(response, title=ugettext('Invoice #%(invoice_id)d') % {'invoice_id': self.invoice_id}, leftMargin=0.5 * inch, rightMargin=0.5 * inch)
        frameT = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height + 0.5 * inch, id='normal')
        doc.addPageTemplates([PageTemplate(id='all', frames=frameT, onPage=invoice_footer), ])

        styleH = ParagraphStyle({})
        styleH.fontSize = 14
        styleH.leading = 16
        styleH.borderPadding = (5,) * 4

        styleCustomer = ParagraphStyle({})
        styleCustomer.fontSize = 12
        styleCustomer.leading = 14
        styleCustomer.borderPadding = (5,) * 4

        styleTotal = ParagraphStyle({})
        styleTotal.fontSize = 14
        styleTotal.leading = 16
        styleTotal.borderColor = colors.black
        styleTotal.borderWidth = 0.5
        styleTotal.borderPadding = (5,) * 4

        styleH2 = ParagraphStyle({})
        styleH2.fontSize = 14
        styleH2.leading = 16


        styleTitle = ParagraphStyle({})
        styleTitle.fontSize = 14
        styleTitle.fontName = "Times-Bold"

        styleN = ParagraphStyle({})
        styleN.fontSize = 12
        styleN.leading = 14

        styleNSmall = ParagraphStyle({})
        styleNSmall.fontSize = 8
        styleNSmall.leading = 14

        styleF = ParagraphStyle({})
        styleF.fontSize = 10
        styleF.alignment = TA_CENTER

        styleLabel = ParagraphStyle({})

        story = []

        data = []
        user_header_content = """
        %s %s<br/>
        %s<br/>
        %s %s<br/>
        %s<br/>
        Siret : %s<br/>
        """ % (user.first_name,
               user.last_name,
               user.get_profile().address.street.replace("\n", "<br/>"),
               user.get_profile().address.zipcode,
               user.get_profile().address.city,
               user.get_profile().address.country or '',
               user.get_profile().company_id)

        customer_header_content = """
        <br/><br/><br/><br/>
        %s<br/>
        %s<br/>
        %s %s<br/>
        %s<br/>
        """

        if user.get_profile().logo_file:
            user_header = Image("%s%s" % (settings.FILE_UPLOAD_DIR, user.get_profile().logo_file))
        else:
            user_header = Paragraph(user_header_content, styleH)

        customer_header = customer_header_content % (self.customer.name,
                                                     self.customer.address.street.replace("\n", "<br/>"),
                                                     self.customer.address.zipcode,
                                                     self.customer.address.city,
                                                     self.customer.address.country or '')

        customer_header = Paragraph(customer_header, styleCustomer)

        data.append([user_header,
                    '',
                    customer_header])

        t1 = Table(data, [3.5 * inch, 0.7 * inch, 3.1 * inch], [1.9 * inch])
        table_style = [('VALIGN', (0, 0), (-1, -1), 'TOP'), ]
        if user.get_profile().logo_file:
            table_style.append(('TOPPADDING', (0, 0), (0, 0), 0))
            table_style.append(('LEFTPADDING', (0, 0), (0, 0), 0))

        t1.setStyle(TableStyle(table_style))

        story.append(t1)

        spacer1 = Spacer(doc.width, 0.25 * inch)
        story.append(spacer1)

        data = []
        msg = u"Dispensé d'immatriculation au registre du commerce et des sociétés (RCS) et au répertoire des métiers (RM)"
        data.append([Paragraph(msg, styleN),
                    '',
                    Paragraph("<br/>" + _("Date : %s") % (localize(self.edition_date)), styleH2)])

        t2 = Table(data, [3.5 * inch, 0.3 * inch, 3.5 * inch], [0.7 * inch])
        t2.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ]))

        story.append(t2)

        spacer2 = Spacer(doc.width, 0.25 * inch)
        story.append(spacer2)

        story.append(Paragraph(_("INVOICE #%d") % (self.invoice_id), styleTitle))

        spacer3 = Spacer(doc.width, 0.1 * inch)
        story.append(spacer3)

        # invoice row list
        rows = self.invoice_rows.all()
        extra_rows = 0
        data = [[ugettext('Label'), ugettext('Quantity'), ugettext('Unit price'), ugettext('Total excl tax')]]
        if user.get_profile().vat_number:
            data[0].append(ugettext('VAT'))
            label_width = 4.0 * inch
        else:
            label_width = 4.5 * inch
        for row in rows:
            label = row.label
            if row.proposal and row.proposal.reference:
                label = u"%s - [%s]" % (label, row.proposal.reference)
            para = Paragraph(label, styleLabel)
            para.width = label_width
            splitted_para = para.breakLines(label_width)
            label = " ".join(splitted_para.lines[0][1])
            quantity = row.quantity
            quantity = quantity.quantize(Decimal(1)) if quantity == quantity.to_integral() else quantity.normalize()
            unit_price = row.unit_price
            unit_price = unit_price.quantize(Decimal(1)) if unit_price == unit_price.to_integral() else unit_price.normalize()
            total = row.quantity * row.unit_price
            total = total.quantize(Decimal(1)) if total == total.to_integral() else total.normalize()

            data_row = [label, localize(quantity), "%s %s" % (localize(unit_price), "€".decode('utf-8')), "%s %s" % (localize(total), "€".decode('utf-8'))]
            if user.get_profile().vat_number:
                data_row.append("%s%%" % (localize(row.vat_rate)))
            data.append(data_row)
            for extra_row in splitted_para.lines[1:]:
                label = " ".join(extra_row[1])
                if user.get_profile().vat_number:
                    data.append([label, '', '', '', ''])
                else:
                    data.append([label, '', '', ''])
                extra_rows = extra_rows + 1

        row_count = len(rows) + extra_rows
        if row_count <= 16:
            max_row_count = 16
        else:
            first_page_count = 21
            normal_page_count = 33
            last_page_count = 27
            max_row_count = first_page_count + ((row_count - first_page_count) // normal_page_count * normal_page_count) + last_page_count
            if row_count - first_page_count - ((row_count - first_page_count) // normal_page_count * normal_page_count) > last_page_count:
                max_row_count = max_row_count + normal_page_count

        for i in range(max_row_count - row_count):
            if user.get_profile().vat_number:
                data.append(['', '', '', '', ''])
            else:
                data.append(['', '', '', ''])

        if user.get_profile().vat_number:
            row_table = Table(data, [4.2 * inch, 0.8 * inch, 0.9 * inch, 0.8 * inch, 0.5 * inch], (max_row_count + 1) * [0.3 * inch])
        else:
            row_table = Table(data, [4.7 * inch, 0.8 * inch, 0.9 * inch, 0.8 * inch], (max_row_count + 1) * [0.3 * inch])

        row_style = [('ALIGN', (0, 0), (-1, 0), 'CENTER'),
                     ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
                     ('FONT', (0, 0), (-1, 0), 'Times-Bold'),
                     ('BOX', (0, 0), (-1, 0), 0.25, colors.black),
                     ('INNERGRID', (0, 0), (-1, 0), 0.25, colors.black),
                     ('BOX', (0, 1), (0, -1), 0.25, colors.black),
                     ('BOX', (1, 1), (1, -1), 0.25, colors.black),
                     ('BOX', (2, 1), (2, -1), 0.25, colors.black),
                     ('BOX', (3, 1), (3, -1), 0.25, colors.black)]
        if user.get_profile().vat_number:
            row_style.append(('BOX', (4, 1), (4, -1), 0.25, colors.black))

        row_table.setStyle(TableStyle(row_style))

        story.append(row_table)

        spacer4 = Spacer(doc.width, 0.35 * inch)
        story.append(spacer4)
        invoice_amount = self.amount
        invoice_amount = invoice_amount.quantize(Decimal(1)) if invoice_amount == invoice_amount.to_integral() else invoice_amount.normalize()
        left_block = [Paragraph(_("Payment date : %s") % (localize(self.payment_date)), styleN),
                      Paragraph(_("Penalty begins on : %s") % (localize(self.penalty_date) or ''), styleN),
                      Paragraph(_("Penalty rate : %s") % (localize(self.penalty_rate) or ''), styleN),
                      Paragraph(_("Discount conditions : %s") % (self.discount_conditions or ''), styleN)]
        if self.owner.get_profile().iban_bban:
            left_block.append(Spacer(doc.width, 0.2 * inch))
            left_block.append(Paragraph(_("IBAN/BBAN : %s") % (self.owner.get_profile().iban_bban), styleNSmall))
            if self.owner.get_profile().bic:
                left_block.append(Paragraph(_("BIC/SWIFT : %s") % (self.owner.get_profile().bic), styleNSmall))

        if user.get_profile().vat_number:
            right_block = [Paragraph(_("Total excl tax : %(amount)s %(currency)s") % {'amount': localize(invoice_amount), 'currency' : "€".decode('utf-8')}, styleN)]
            vat_amounts = {}
            for row in rows:
                vat_rate = row.vat_rate
                vat_amount = row.amount * vat_rate / 100
                if vat_rate in vat_amounts:
                    vat_amounts[vat_rate] = vat_amounts[vat_rate] + vat_amount
                else:
                    vat_amounts[vat_rate] = vat_amount
            for vat_rate, vat_amount in vat_amounts.items():
                vat_amount = round(vat_amount, 2)
                #vat_amount = vat_amount.quantize(Decimal(1)) if vat_amount == vat_amount.to_integral() else vat_amount.normalize()
                right_block.append(Paragraph(_("VAT %(vat_rate)s%% : %(vat_amount)s %(currency)s") % {'vat_rate': localize(vat_rate),
                                                                                                      'vat_amount': localize(vat_amount),
                                                                                                      'currency' : "€".decode('utf-8')},
                                             styleN))

            incl_tax_amount = invoice_amount + sum(vat_amounts.values())
            #incl_tax_amount = incl_tax_amount.quantize(Decimal(1)) if incl_tax_amount == incl_tax_amount.to_integral() else incl_tax_amount.normalize()
            incl_tax_amount = round(incl_tax_amount, 2)
            right_block.append(Spacer(1, 0.25 * inch))
            right_block.append(Paragraph(_("TOTAL incl tax : %(amount)s %(currency)s") % {'amount': localize(incl_tax_amount), 'currency' : "€".decode('utf-8')}, styleTotal))
        else:
            right_block = [Paragraph(_("TOTAL excl tax : %(amount)s %(currency)s") % {'amount': localize(invoice_amount), 'currency' : "€".decode('utf-8')}, styleTotal),
                           Spacer(1, 0.25 * inch),
                           Paragraph(u"TVA non applicable, art. 293 B du CGI", styleN)]

        data = [[left_block,
                '',
                right_block], ]

        if self.execution_begin_date and self.execution_end_date:
            data[0][0].insert(1, Paragraph(_("Execution dates : %(begin_date)s to %(end_date)s") % {'begin_date': localize(self.execution_begin_date), 'end_date' : localize(self.execution_end_date)}, styleN))

        footer_table = Table(data, [4.5 * inch, 0.3 * inch, 2.5 * inch], [1 * inch])
        footer_table.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ]))

        story.append(footer_table)

        doc.build(story, canvasmaker=NumberedCanvas)

        return response

class InvoiceRowAmountError(Exception):
    pass

class InvoiceRow(Row):
    invoice = models.ForeignKey(Invoice, related_name="invoice_rows")
    proposal = models.ForeignKey(Proposal, related_name="invoice_rows", verbose_name=_('Proposal'), null=True, blank=True)
    balance_payments = models.BooleanField(verbose_name=_('Balance payments for the proposal'), help_text=_('"Balancing payments for the proposal" means there will be no future invoices for the selected proposal. Thus the amount remaining to invoice for this proposal will fall to zero and its state will be set to "balanced" when all invoices are paid.'))

    class Meta:
        ordering = ['id']

    def save(self, force_insert=False, force_update=False, using=None, user=None):
        super(InvoiceRow, self).save(force_insert, force_update, using, user)

def update_invoice_amount(sender, instance, created=None, **kwargs):
    row = instance
    invoice = row.invoice
    invoice.amount = invoice.invoice_rows.all().aggregate(sum=Sum('amount'))['sum'] or 0
    invoice.save(user=invoice.owner)

pre_save.connect(update_row_amount, sender=InvoiceRow)
post_save.connect(update_invoice_amount, sender=InvoiceRow)
post_delete.connect(update_invoice_amount, sender=InvoiceRow)
