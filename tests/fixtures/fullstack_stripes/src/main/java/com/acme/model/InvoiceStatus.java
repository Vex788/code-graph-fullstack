package com.acme.model;
public enum InvoiceStatus {
    DRAFT,
    SUBMITTED,
    PAID;

    public boolean isOpen() {
        return this != PAID;
    }
}
