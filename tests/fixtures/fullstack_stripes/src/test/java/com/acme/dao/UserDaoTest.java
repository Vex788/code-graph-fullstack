package com.acme.dao;

import static org.junit.Assert.assertEquals;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import com.acme.model.User;
import org.hibernate.Session;
import org.hibernate.SessionFactory;
import org.junit.Before;
import org.junit.Test;
public class UserDaoTest {

    private Session session;
    private UserDao dao;

    @Before
    public void setUp() {
        SessionFactory factory = mock(SessionFactory.class);
        session = mock(Session.class);
        when(factory.getCurrentSession()).thenReturn(session);
        dao = new UserDao(factory);
    }

    @Test
    public void saveDelegatesToSession() {
        User user = new User();
        dao.save(user);
        verify(session).save(user);
    }

    @Test
    public void saveWithFlushFlushes() {
        User user = new User();
        dao.save(user, true);
        verify(session).flush();
        assertEquals(null, user.getId());
    }
}
