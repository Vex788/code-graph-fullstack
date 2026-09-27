package com.acme.service;

import com.acme.dao.OrderDao;
import com.acme.model.Money;
import com.acme.model.Order;
import org.springframework.stereotype.Service;
@Service("orderService")
public class OrderService {

    private final OrderDao orderDao;

    public OrderService(OrderDao orderDao) {
        this.orderDao = orderDao;
    }

    public Order find(Long id) {
        return orderDao.load(id);
    }

    public Money place(Order order) {
        orderDao.persist(order);
        return order.total("USD");
    }
}
