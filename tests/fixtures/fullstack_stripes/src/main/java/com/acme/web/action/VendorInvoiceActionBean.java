package com.acme.web.action;

import com.acme.model.Invoice;
import com.acme.model.Money;
import com.acme.service.InvoiceService;
import net.sourceforge.stripes.action.ActionBean;
import net.sourceforge.stripes.action.ActionBeanContext;
import net.sourceforge.stripes.action.DefaultHandler;
import net.sourceforge.stripes.action.ForwardResolution;
import net.sourceforge.stripes.action.HandlesEvent;
import net.sourceforge.stripes.action.RedirectResolution;
import net.sourceforge.stripes.action.Resolution;
import net.sourceforge.stripes.action.StreamingResolution;
import net.sourceforge.stripes.action.UrlBinding;
import net.sourceforge.stripes.integration.spring.SpringBean;
import net.sourceforge.stripes.validation.Validate;
import net.sourceforge.stripes.validation.ValidateNestedProperties;
@UrlBinding("/vendor/Invoice.action")
public class VendorInvoiceActionBean implements ActionBean {

    private ActionBeanContext context;

    @SpringBean
    private InvoiceService service;

    @ValidateNestedProperties({
        @Validate(field = "amount", required = true, minvalue = 0),
        @Validate(field = "currency", required = true, maxlength = 3),
        @Validate(field = "vendorCode", required = true, maxlength = 32)
    })
    private Invoice invoice;

    private Long invoiceId;

    @Override
    public ActionBeanContext getContext() {
        return context;
    }

    @Override
    public void setContext(ActionBeanContext context) {
        this.context = context;
    }

    public Invoice getInvoice() {
        return invoice;
    }

    public void setInvoice(Invoice invoice) {
        this.invoice = invoice;
    }

    public Long getInvoiceId() {
        return invoiceId;
    }

    public void setInvoiceId(Long invoiceId) {
        this.invoiceId = invoiceId;
    }

    @DefaultHandler
    public Resolution view() {
        if (invoiceId != null) {
            invoice = service.findInvoice(invoiceId);
        }
        return new ForwardResolution("/WEB-INF/jsp/vendor/invoice.jsp");
    }

    @HandlesEvent("save")
    public Resolution save() {
        Money total = new Money(invoice.getAmount(), invoice.getCurrency());
        service.saveInvoice(invoice, total);
        return new RedirectResolution(VendorInvoiceActionBean.class)
            .addParameter("invoiceId", invoice.getId());
    }

    @HandlesEvent("load")
    public Resolution load() {
        Invoice found = service.findInvoice(invoiceId);
        String json = "{\"amount\":" + found.getAmount() + "}";
        return new StreamingResolution("application/json", json);
    }

    @HandlesEvent("list")
    public Resolution list() {
        return new ForwardResolution("/WEB-INF/jsp/vendor/invoice_list.jsp");
    }
}
