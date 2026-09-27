package com.acme.service;

import static com.acme.util.Strings.isBlank;
import com.acme.dao.InvoiceDao;
import com.acme.model.Invoice;
import com.acme.model.InvoiceStatus;
import com.acme.model.Money;
import java.util.List;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
@Service("invoiceService")
@Transactional
public class InvoiceServiceImpl implements InvoiceService {

    private final InvoiceDao invoiceDao;

    public InvoiceServiceImpl(InvoiceDao invoiceDao) {
        this.invoiceDao = invoiceDao;
    }

    @Override
    public Invoice findInvoice(Long id) {
        return invoiceDao.find(id);
    }

    @Override
    public void saveInvoice(Invoice invoice, Money total) {
        if (isBlank(invoice.getVendorCode())) {
            throw new IllegalArgumentException("vendor code is required");
        }
        invoice.setAmount(total.cents());
        invoice.setCurrency(total.currency());
        invoice.setStatus(InvoiceStatus.SUBMITTED);
        invoiceDao.store(invoice);
    }

    @Override
    public List<Invoice> listForVendor(String vendorCode) {
        return invoiceDao.findByVendor(vendorCode);
    }
}
