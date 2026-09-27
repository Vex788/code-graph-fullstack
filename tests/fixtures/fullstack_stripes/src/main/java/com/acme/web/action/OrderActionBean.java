package com.acme.web.action;

import com.acme.model.Money;
import com.acme.model.Order;
import com.acme.service.OrderService;
import net.sourceforge.stripes.action.DefaultHandler;
import net.sourceforge.stripes.action.ForwardResolution;
import net.sourceforge.stripes.action.HandlesEvent;
import net.sourceforge.stripes.action.Resolution;
import net.sourceforge.stripes.action.UrlBinding;
import net.sourceforge.stripes.integration.spring.SpringBean;
@UrlBinding("/order/Order.action")
public class OrderActionBean extends BaseActionBean {

    @SpringBean("orderService")
    private OrderService orderService;

    private Long orderId;

    private Order order;

    private Money total;

    public Long getOrderId() {
        return orderId;
    }

    public void setOrderId(Long orderId) {
        this.orderId = orderId;
    }

    public Order getOrder() {
        return order;
    }

    public Money getTotal() {
        return total;
    }

    @DefaultHandler
    public Resolution view() {
        order = orderService.find(orderId);
        return new ForwardResolution("/WEB-INF/jsp/order/view.jsp");
    }

    @HandlesEvent("place")
    public Resolution place() {
        order = orderService.find(orderId);
        total = orderService.place(order);
        return new ForwardResolution("/WEB-INF/jsp/order/view.jsp");
    }
}
