package com.acme.audit;

import org.hibernate.Session;
import org.hibernate.SessionFactory;
/** Writes audit rows straight through the Hibernate session. */
public class AuditLogWriter {

    private final SessionFactory sessionFactory;

    public AuditLogWriter(SessionFactory sessionFactory) {
        this.sessionFactory = sessionFactory;
    }

    public void write(AuditEntry entry) {
        Session session = sessionFactory.getCurrentSession();
        session.save(entry);
    }
}
