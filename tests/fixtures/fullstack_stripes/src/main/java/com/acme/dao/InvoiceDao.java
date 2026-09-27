package com.acme.dao;

import com.acme.model.Invoice;
import java.util.List;
import org.hibernate.SessionFactory;
public class InvoiceDao extends HibernateSupport {

    public InvoiceDao(SessionFactory sessionFactory) {
        super(sessionFactory);
    }

    public Invoice find(Long id) {
        return currentSession().get(Invoice.class, id);
    }

    public void store(Invoice invoice) {
        currentSession().saveOrUpdate(invoice);
    }

    public List<Invoice> findByVendor(String vendorCode) {
        return currentSession()
            .createQuery("from Invoice i where i.vendorCode = :code", Invoice.class)
            .setParameter("code", vendorCode)
            .list();
    }
}
