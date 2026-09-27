package com.acme.util;

import java.time.LocalDate;
import java.time.format.DateTimeFormatter;
public final class Dates {

    private static final DateTimeFormatter ISO = DateTimeFormatter.ISO_LOCAL_DATE;

    private Dates() {
    }

    public static String iso(LocalDate date) {
        return date == null ? "" : ISO.format(date);
    }
}
