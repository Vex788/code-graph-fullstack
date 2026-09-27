package com.acme.report;

import static com.acme.util.Strings.abbreviate;
import com.acme.model.Invoice;
import com.acme.service.InvoiceService;
import java.util.List;
public class InvoiceReport {

    private final InvoiceService invoiceService;

    public InvoiceReport(InvoiceService invoiceService) {
        this.invoiceService = invoiceService;
    }

    public String render(String vendorCode) {
        StringBuilder out = new StringBuilder();
        List<Invoice> invoices = invoiceService.listForVendor(vendorCode);
        for (Invoice invoice : invoices) {
            out.append(abbreviate(invoice.getVendorCode(), 12)).append('\n');
        }
        return out.toString();
    }
}
