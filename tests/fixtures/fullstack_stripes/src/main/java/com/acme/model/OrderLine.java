package com.acme.model;
public class OrderLine {

    private String sku;
    private int quantity;
    private long unitCents;

    public long amountCents() {
        return unitCents * quantity;
    }

    public String getSku() {
        return sku;
    }
}
