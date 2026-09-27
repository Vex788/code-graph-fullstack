package com.acme.model;

import static org.junit.Assert.assertEquals;
import org.junit.Test;
public class MoneyTest {

    @Test
    public void plusAddsCents() {
        Money a = new Money(150, "USD");
        Money b = new Money(275, "USD");
        assertEquals(425, a.plus(b).cents());
    }

    @Test
    public void formatPadsCents() {
        assertEquals("1.05 USD", new Money(105, "USD").format());
    }
}
