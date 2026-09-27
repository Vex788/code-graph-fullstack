package com.acme.model;

import java.util.ArrayList;
import java.util.List;
/** Mapped by Order.hbm.xml, not by annotations. */
public class Order {

    private Long id;
    private User customer;
    private List<OrderLine> lines = new ArrayList<>();

    public Long getId() {
        return id;
    }

    public User getCustomer() {
        return customer;
    }

    public void setCustomer(User customer) {
        this.customer = customer;
    }

    public List<OrderLine> getLines() {
        return lines;
    }

    public Money total(String currency) {
        long cents = 0;
        for (OrderLine line : lines) {
            cents += line.amountCents();
        }
        return new Money(cents, currency);
    }
}
