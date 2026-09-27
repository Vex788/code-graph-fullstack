package com.acme.dao;

import com.acme.model.User;
import java.util.List;
public interface UserDaoApi extends GenericDao<User> {

    List<User> findAllSortedByName();
}
