package com.acme.dao;

import java.util.List;
public interface GenericDao<T> {

    T findById(Long id);

    void save(T entity);

    List<T> findAll();
}
