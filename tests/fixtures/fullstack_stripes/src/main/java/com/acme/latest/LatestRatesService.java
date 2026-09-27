package com.acme.latest;

import com.acme.model.Money;
import java.util.HashMap;
import java.util.Map;
/** Production code: the package name only looks like a test directory. */
public class LatestRatesService {

    private final Map<String, Long> ratesPerMille = new HashMap<>();

    public void publish(String currency, long perMille) {
        ratesPerMille.put(currency, perMille);
    }

    public Money convert(Money amount, String target) {
        long rate = ratesPerMille.getOrDefault(target, 1000L);
        return new Money(amount.cents() * rate / 1000, target);
    }
}
