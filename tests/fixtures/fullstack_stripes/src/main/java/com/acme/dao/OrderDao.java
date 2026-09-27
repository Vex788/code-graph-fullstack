package com.acme.dao;

import com.acme.model.Order;
import java.util.List;
import org.hibernate.Session;
import org.hibernate.SessionFactory;
public class OrderDao extends HibernateSupport {

    public OrderDao(SessionFactory sessionFactory) {
        super(sessionFactory);
    }

    public Order load(Long id) {
        return currentSession().get(Order.class, id);
    }

    public void persist(Order order) {
        Session session = currentSession();
        session.save(order);
    }

    public List<Order> findOpen() {
        return currentSession().createQuery("from Order o where o.closed = false", Order.class)
            .list();
    }
}
