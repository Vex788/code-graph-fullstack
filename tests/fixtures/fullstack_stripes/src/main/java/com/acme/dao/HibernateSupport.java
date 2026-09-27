package com.acme.dao;

import org.hibernate.Session;
import org.hibernate.SessionFactory;
public abstract class HibernateSupport {

    private final SessionFactory sessionFactory;

    protected HibernateSupport(SessionFactory sessionFactory) {
        this.sessionFactory = sessionFactory;
    }

    protected Session currentSession() {
        return sessionFactory.getCurrentSession();
    }
}
