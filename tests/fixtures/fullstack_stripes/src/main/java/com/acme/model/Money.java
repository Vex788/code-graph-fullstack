package com.acme.model;

import java.util.Objects;
public record Money(long cents, String currency) {

    public Money {
        Objects.requireNonNull(currency, "currency");
    }

    public Money plus(Money other) {
        if (!currency.equals(other.currency())) {
            throw new IllegalArgumentException("currency mismatch");
        }
        return new Money(cents + other.cents(), currency);
    }

    public String format() {
        return String.format("%d.%02d %s", cents / 100, cents % 100, currency);
    }
}
