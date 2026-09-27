package com.acme.dao;

import com.acme.model.User;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;
import org.hibernate.Session;
import org.hibernate.SessionFactory;
public class UserDao extends HibernateSupport implements UserDaoApi {

    public UserDao(SessionFactory sessionFactory) {
        super(sessionFactory);
    }

    @Override
    public User findById(Long id) {
        return currentSession().get(User.class, id);
    }

    @Override
    public void save(User u) {
        save(u, false);
    }

    public void save(User u, boolean flush) {
        Session session = currentSession();
        session.save(u);
        if (flush) {
            session.flush();
        }
    }

    @Override
    public List<User> findAll() {
        return currentSession().createQuery("from User", User.class).list();
    }

    @Override
    public List<User> findAllSortedByName() {
        List<User> users = new ArrayList<>(findAll());
        Collections.sort(users, new Comparator<User>() {
            @Override
            public int compare(User a, User b) {
                return a.getName().compareTo(b.getName());
            }
        });
        return users;
    }
}
