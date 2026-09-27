package com.acme.service;

import com.acme.dao.UserDao;
import com.acme.model.User;
import java.util.List;
import org.springframework.stereotype.Service;
@Service("userService")
public class UserService {

    private final UserDao userDao;

    public UserService(UserDao userDao) {
        this.userDao = userDao;
    }

    public void register(User user) {
        user.setActive(true);
        userDao.save(user);
    }

    public void registerAndFlush(User user) {
        userDao.save(user, true);
    }

    public List<User> listUsers() {
        return userDao.findAllSortedByName();
    }
}
