package com.acme.service;

import com.acme.model.Invoice;
import com.acme.model.Money;
import java.util.List;
public interface InvoiceService {

    Invoice findInvoice(Long id);

    void saveInvoice(Invoice invoice, Money total);

    List<Invoice> listForVendor(String vendorCode);
}
